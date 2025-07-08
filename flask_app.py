from flask_cors import CORS
from flask import Flask, request, jsonify
import joblib
import numpy as np
import os
import pandas as pd
from datetime import datetime
from prophet import Prophet

app = Flask(__name__)
CORS(app)

# Load Models
model = joblib.load("waste_predictor_model.pkl")
model_columns = joblib.load("model_columns.pkl")
demand_model = joblib.load("demand_model.pkl")
ai_model = joblib.load("ai_score_model.pkl")

@app.route('/')
def home():
    return "Walmart Waste Predictor API is running!"

# === SMART BULK RECOMMENDATIONS ===
@app.route('/upload_csv', methods=['POST'])
def upload_csv():
    try:
        file = request.files['file']
        df = pd.read_csv(file)

        required_columns = {"product_id", "name", "category", "stock", "expiry_date", "store_location", "freshness_score"}
        if not required_columns.issubset(df.columns):
            return jsonify({"success": False, "error": "Missing required columns."})

        today = datetime.today().date()
        df["expiry_date"] = pd.to_datetime(df["expiry_date"], errors='coerce')
        df["days_to_expiry"] = df["expiry_date"].apply(lambda x: (x.date() - today).days if pd.notna(x) else pd.NA)

        def check_expiry_risk(row):
            if pd.isna(row["days_to_expiry"]) or pd.isna(row["freshness_score"]):
                return 0
            return 1 if row["days_to_expiry"] <= 2 and row["freshness_score"] < 0.7 else 0

        df["expiry_risk"] = df.apply(check_expiry_risk, axis=1)

        def label_expiry_status(days):
            if pd.isna(days):
                return "Invalid Date"
            elif days < 0:
                return "Already Expired"
            else:
                return f"{days} day(s) left"

        df["expiry_status"] = df["days_to_expiry"].apply(label_expiry_status)

        if 'previous_sales' not in df.columns:
            df['previous_sales'] = df['stock'].apply(lambda x: max(1, int(x * 0.7)))
        if 'temperature_C' not in df.columns:
            df['temperature_C'] = 25

        df["daily_demand"] = df.apply(
            lambda row: round(demand_model.predict([[row["previous_sales"], row["stock"], row["temperature_C"]]])[0], 2),
            axis=1
        )

        df["recommendation"] = df["expiry_risk"].apply(lambda r: "Donate" if r else "Keep in Stock")

        recommendations = df[[
            "product_id", "name", "store_location", "category", "stock", "freshness_score",
            "expiry_status", "daily_demand", "expiry_risk", "recommendation"
        ]].to_dict(orient="records")

        alerts_df = df[
            (df["days_to_expiry"] < 1) |
            ((df["stock"] > 50) & (df["daily_demand"] < 10)) |
            ((df["daily_demand"] > 80) & (df["stock"] < 20) & (df["days_to_expiry"] > 1))
        ].copy()

        def detect_reason(row):
            if row["days_to_expiry"] < 1:
                return "❗ Expiring Today"
            elif row["stock"] > 50 and row["daily_demand"] < 10:
                return "📉 Overstocked, Low Demand"
            elif row["daily_demand"] > 80 and row["stock"] < 20:
                return "⚡ Demand Surge, Low Stock"
            return "Unknown"

        alerts_df["alert_reason"] = alerts_df.apply(detect_reason, axis=1)
        alerts_df["expiry_date"] = alerts_df["expiry_date"].dt.strftime("%d-%m-%Y")

        alerts = alerts_df[[
            "product_id", "name", "store_location", "category", "stock",
            "expiry_date", "daily_demand", "days_to_expiry", "alert_reason"
        ]].to_dict(orient="records")

        return jsonify({"success": True, "recommendations": recommendations, "alerts": alerts})

    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

# === DEMAND, RISK, RECOMMEND ===
@app.route('/predict', methods=['POST'])
def predict():
    try:
        json_ = request.json
        query = [json_[col] for col in model_columns]
        prediction = model.predict([query])[0]
        return jsonify({'prediction': "High Risk" if prediction else "Low Risk"})
    except Exception as e:
        return jsonify({'error': str(e)})

@app.route('/forecast_demand', methods=['POST'])
def forecast_demand():
    try:
        json_ = request.json
        features = [json_['previous_sales'], json_['stock'], json_['temperature_C']]
        prediction = demand_model.predict([features])[0]
        return jsonify({'forecasted_demand': round(prediction, 2)})
    except Exception as e:
        return jsonify({'error': str(e)})

@app.route('/recommend_action', methods=['POST'])
def recommend_action():
    try:
        data = request.json
        expiry_features = [data[col] for col in model_columns]
        expiry_risk = model.predict([expiry_features])[0]
        demand_features = [data['previous_sales'], data['stock'], data['temperature_C']]
        demand = demand_model.predict([demand_features])[0]
        action = "Donate" if expiry_risk == 1 and demand < 20 else \
                 "Transfer or Apply Discount" if expiry_risk == 1 else \
                 "Keep in Stock"
        return jsonify({
            'expiry_risk': int(expiry_risk),
            'forecasted_demand': round(demand, 2),
            'recommendation': action
        })
    except Exception as e:
        return jsonify({'error': str(e)})

# === WASTE FORECAST ===
@app.route("/forecast_waste", methods=["POST"])
def forecast_waste():
    try:
        file = request.files.get('file')
        if not file:
            return jsonify({"error": "No file uploaded"}), 400

        df = pd.read_csv(file)
        expected_columns = {"store_location", "item_name", "date", "waste_kg"}
        if not expected_columns.issubset(df.columns):
            return jsonify({"error": f"Missing required columns: {expected_columns}"}), 400

        df["date"] = pd.to_datetime(df["date"], format="%d-%m-%Y", errors="coerce")
        df = df.dropna(subset=["date", "waste_kg"])
        df["waste_kg"] = pd.to_numeric(df["waste_kg"], errors="coerce")

        results = []
        grouped = df.groupby(["store_location", "item_name"])
        for (store, item), group_df in grouped:
            group_df = group_df.rename(columns={"date": "ds", "waste_kg": "y"}).sort_values("ds")
            if len(group_df) < 2:
                continue
            model = Prophet()
            model.fit(group_df[["ds", "y"]])
            forecast = model.predict(model.make_future_dataframe(periods=7))
            forecast_result = forecast[["ds", "yhat"]].tail(7)
            forecast_result["ds"] = forecast_result["ds"].dt.strftime("%d-%m-%Y")
            for _, row in forecast_result.iterrows():
                results.append({
                    "store_location": store,
                    "item_name": item,
                    "date": row["ds"],
                    "predicted_waste_kg": round(row["yhat"], 2)
                })

        return jsonify(results)

    except Exception as e:
        return jsonify({"error": str(e)})

# === AI LEADERBOARD ===
LEADERBOARD_FILE = "data/ai_leaderboard.csv"
if not os.path.exists(LEADERBOARD_FILE):
    pd.DataFrame(columns=["store_location", "waste_donated_kg", "waste_reduced_kg", "waste_generated_kg", "date", "ai_score"]).to_csv(LEADERBOARD_FILE, index=False)

@app.route("/upload_waste_ai", methods=["POST"])
def upload_waste_ai():
    try:
        file = request.files['file']
        df = pd.read_csv(file)
        required = {"store_location", "waste_donated_kg", "waste_reduced_kg", "waste_generated_kg", "date"}
        if not required.issubset(df.columns):
            return jsonify({"success": False, "error": "Missing required columns."})
        df["date"] = pd.to_datetime(df["date"], format="%d-%m-%Y", errors='coerce').dt.date
        df["ai_score"] = ai_model.predict(df[["waste_donated_kg", "waste_reduced_kg", "waste_generated_kg"]]).round(2)
        df.to_csv(LEADERBOARD_FILE, mode="a", index=False, header=False)
        return jsonify({"success": True, "message": "Report uploaded with AI scores!"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route("/ai_daily_leaderboard", methods=["GET"])
def ai_daily_leaderboard():
    try:
        df = pd.read_csv(LEADERBOARD_FILE)
        df["date"] = pd.to_datetime(df["date"], errors='coerce').dt.date
        today = datetime.today().date()
        daily = df[df["date"] == today].drop_duplicates("store_location")
        daily = daily.sort_values("ai_score", ascending=False).reset_index(drop=True)
        daily["badge"] = ["🥇", "🥈", "🥉"] + [""] * (len(daily) - 3)
        return jsonify(daily.to_dict(orient="records"))
    except Exception as e:
        return jsonify({"error": str(e)})

@app.route("/ai_monthly_leaderboard", methods=["GET"])
def ai_monthly_leaderboard():
    try:
        df = pd.read_csv(LEADERBOARD_FILE)
        df["date"] = pd.to_datetime(df["date"], errors='coerce').dt.date
        today = datetime.today()
        df = df[df["date"].apply(lambda d: d.month == today.month and d.year == today.year)]
        monthly = df.groupby("store_location").agg({
            "waste_donated_kg": "sum",
            "waste_reduced_kg": "sum",
            "waste_generated_kg": "sum",
            "ai_score": "mean"
        }).reset_index().sort_values("ai_score", ascending=False)
        monthly["badge"] = ["🏆"] + [""] * (len(monthly) - 1)
        return jsonify(monthly.to_dict(orient="records"))
    except Exception as e:
        return jsonify({"error": str(e)})

@app.route("/ai_leaderboard_by_date", methods=["GET"])
def ai_leaderboard_by_date():
    try:
        date_str = request.args.get("date")
        if not date_str:
            return jsonify({"error": "Date parameter missing"}), 400
        date_obj = datetime.strptime(date_str, "%d-%m-%Y").date()
        df = pd.read_csv(LEADERBOARD_FILE)
        df["date"] = pd.to_datetime(df["date"], errors='coerce').dt.date
        result = df[df["date"] == date_obj].drop_duplicates("store_location")
        result = result.sort_values("ai_score", ascending=False).reset_index(drop=True)
        result["badge"] = ["🥇", "🥈", "🥉"] + [""] * (len(result) - 3)
        return jsonify(result.to_dict(orient="records"))
    except Exception as e:
        return jsonify({"error": str(e)})

# === NEW: RETAIL SUSTAINABILITY SIMULATOR (Page 6) ===
@app.route('/simulate_day', methods=['POST'])
def simulate_day():
    try:
        data = request.json
        stock = int(data.get("stock", 0))
        temperature = float(data.get("temperature", 25))
        action = data.get("action", "redistribute")

        waste = max(0, int(stock * (temperature - 25) * 0.02)) if temperature > 25 else 0
        donation = int(stock * 0.1) if action == "redistribute" else 0
        loss = waste * 15

        sustainability_score = 100 - waste - loss / 10 + donation * 2
        mood = "🌎 Excellent" if sustainability_score >= 90 else \
               "⚠️ Moderate" if sustainability_score >= 70 else \
               "🚨 Poor"

        return jsonify({
            "stock": stock,
            "temperature": temperature,
            "action": action,
            "waste": waste,
            "donation": donation,
            "loss": loss,
            "sustainability_score": round(sustainability_score, 2),
            "mood": mood,
            "timestamp": datetime.now().strftime("%d-%m-%Y %H:%M")
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    print("✅ Starting Flask...")
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=True)
