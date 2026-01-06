"""
Dashboard API Server (Flask)
- Reads trading_history.db
- Shows stats, leaderboard (expert_weights), recent predictions, recent calls, recent trades
RUN:
  python ./dashboard_api.py
"""
from flask import Flask, jsonify, send_file
from flask_cors import CORS
import sqlite3
from datetime import datetime

DB_PATH = "trading_history.db"

app = Flask(__name__)
CORS(app)

def q(query, args=()):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(query, args)
    rows = cur.fetchall()
    conn.close()
    return rows

@app.route("/")
def index():
    return send_file("dashboard.html")

@app.route("/api/stats")
def stats():
    total_preds = q("SELECT COUNT(*) AS n FROM predictions")[0]["n"]
    eval_preds = q("SELECT COUNT(*) AS n FROM predictions WHERE evaluated=1")[0]["n"]
    avg_score = q("SELECT AVG(correctness_score) AS s FROM predictions WHERE evaluated=1")[0]["s"] or 0
    total_calls = q("SELECT COUNT(*) AS n FROM model_calls")[0]["n"]
    ok_calls = q("SELECT COUNT(*) AS n FROM model_calls WHERE ok=1")[0]["n"]
    total_trades = q("SELECT COUNT(*) AS n FROM trades")[0]["n"]
    return jsonify({
        "total_predictions": total_preds,
        "evaluated_predictions": eval_preds,
        "avg_correctness_score": round(float(avg_score), 4),
        "model_calls": total_calls,
        "model_calls_ok": ok_calls,
        "total_trades": total_trades
    })

@app.route("/api/experts/leaderboard")
def experts():
    rows = q("""SELECT provider, model, thinking_style, timeframe, weight, ema_score, samples, last_updated
                FROM expert_weights
                ORDER BY (ema_score * weight) DESC
                LIMIT 50""")
    out = []
    for r in rows:
        out.append({
            "expert": f"{r['provider']}:{r['model']}:{r['thinking_style']}:{r['timeframe']}",
            "weight": round(float(r["weight"]), 6),
            "ema_score": round(float(r["ema_score"]), 4),
            "samples": int(r["samples"]),
            "last_updated": r["last_updated"]
        })
    return jsonify(out)

@app.route("/api/predictions/recent")
def preds():
    rows = q("""SELECT timestamp, model, thinking_style, asset, asset_type, timeframe, direction,
                       confidence, expected_change_percent, evaluated, correctness_score
                FROM predictions
                ORDER BY timestamp DESC
                LIMIT 100""")
    out = []
    for r in rows:
        out.append({
            "timestamp": r["timestamp"],
            "model": r["model"],
            "thinking_style": r["thinking_style"],
            "asset": r["asset"],
            "asset_type": r["asset_type"],
            "timeframe": r["timeframe"],
            "direction": r["direction"],
            "confidence": round(float(r["confidence"] or 0), 3),
            "expected_change_percent": round(float(r["expected_change_percent"] or 0), 3),
            "evaluated": bool(r["evaluated"]),
            "score": round(float(r["correctness_score"] or 0), 4) if r["evaluated"] else None
        })
    return jsonify(out)

@app.route("/api/calls/recent")
def calls():
    rows = q("""SELECT timestamp, provider, model, thinking_style, timeframe, ok, error, latency_ms
                FROM model_calls
                ORDER BY id DESC
                LIMIT 120""")
    out = []
    for r in rows:
        out.append({
            "timestamp": r["timestamp"],
            "provider": r["provider"],
            "model": r["model"],
            "thinking_style": r["thinking_style"],
            "timeframe": r["timeframe"],
            "ok": bool(r["ok"]),
            "error": r["error"],
            "latency_ms": r["latency_ms"]
        })
    return jsonify(out)

@app.route("/api/trades/recent")
def trades():
    rows = q("""SELECT timestamp, asset, asset_type, action, quantity, price, stop_loss
                FROM trades
                ORDER BY id DESC
                LIMIT 50""")
    out = []
    for r in rows:
        out.append({
            "timestamp": r["timestamp"],
            "asset": r["asset"],
            "asset_type": r["asset_type"],
            "action": r["action"],
            "quantity": r["quantity"],
            "price": round(float(r["price"] or 0), 6),
            "stop_loss": round(float(r["stop_loss"] or 0), 6)
        })
    return jsonify(out)

@app.route("/api/system/metrics")
def system_metrics():
    rows = q("""SELECT timestamp, cycle_ms, assets_count, news_count, picks_stored,
                       evaluated_count, traded, fail_counts, memory_mb, cpu_pct
                FROM system_metrics
                ORDER BY id DESC
                LIMIT 200""")
    out = []
    for r in rows:
        out.append({
            "timestamp": r["timestamp"],
            "cycle_ms": int(r["cycle_ms"] or 0),
            "assets_count": int(r["assets_count"] or 0),
            "news_count": int(r["news_count"] or 0),
            "picks_stored": int(r["picks_stored"] or 0),
            "evaluated_count": int(r["evaluated_count"] or 0),
            "traded": bool(r["traded"]),
            "fail_counts": r["fail_counts"],
            "memory_mb": float(r["memory_mb"] or 0),
            "cpu_pct": float(r["cpu_pct"] or 0),
        })
    return jsonify(out)

if __name__ == "__main__":
    print("Dashboard API: http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, debug=True)
