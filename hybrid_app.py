#!/usr/bin/env python3
"""
Railway entrypoint for the unified hybrid core.
Runs the democratic_trader_hybrid loop in a background thread and exposes status APIs.
"""

import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from flask import Flask, jsonify, send_file, request
from flask_cors import CORS

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))

import democratic_trader_hybrid as hybrid  # type: ignore
import dashboard_api as dash  # type: ignore

app = Flask(__name__)
CORS(app)

# Ensure dashboard API reads the hybrid DB
dash.DB_PATH = getattr(hybrid, "DB_PATH", dash.DB_PATH)

_worker = None
_stop_evt = threading.Event()
_state = {
    "running": False,
    "cycles": 0,
    "last_cycle_ts": None,
    "last_timeframe": None,
    "last_error": None,
    "cycle_seconds": getattr(hybrid, "CYCLE_SECONDS", 60),
    "cycle_timeframes": getattr(hybrid, "CYCLE_TIMEFRAMES", ["1m"]),
    "invalid_timeframes": [],
}

_allowed_timeframes = set(getattr(hybrid, "TIMEFRAME_SECONDS", {}).keys())


def _loop():
    i = 0
    _state["running"] = True
    while not _stop_evt.is_set():
        timeframes = getattr(hybrid, "CYCLE_TIMEFRAMES", ["1m"]) or ["1m"]
        tf = timeframes[i % len(timeframes)]
        if tf not in hybrid.TIMEFRAME_SECONDS:
            tf = "1m"
        _state["last_timeframe"] = tf
        _state["last_cycle_ts"] = datetime.utcnow().isoformat(timespec="seconds")
        try:
            hybrid.run_cycle(tf)
            _state["last_error"] = None
        except Exception as exc:
            _state["last_error"] = str(exc)
        _state["cycles"] += 1
        i += 1
        _state["cycle_seconds"] = getattr(hybrid, "CYCLE_SECONDS", 60)
        _state["cycle_timeframes"] = list(timeframes)
        time.sleep(_state["cycle_seconds"])
    _state["running"] = False


def _start_worker():
    global _worker
    if _worker and _worker.is_alive():
        return False
    _stop_evt.clear()
    _worker = threading.Thread(target=_loop, daemon=True)
    _worker.start()
    return True


def _stop_worker():
    _stop_evt.set()
    return True


@app.get("/health")
def health():
    return {"ok": True, "running": _state["running"]}


@app.get("/")
def dashboard():
    dashboard_path = ROOT / "dashboard.html"
    if dashboard_path.exists():
        return send_file(str(dashboard_path))
    return jsonify({"ok": True, "message": "Dashboard not found"})


@app.get("/api/stats")
def stats():
    return dash.stats()


@app.get("/api/experts/leaderboard")
def experts():
    return dash.experts()


@app.get("/api/predictions/recent")
def preds():
    return dash.preds()


@app.get("/api/calls/recent")
def calls():
    return dash.calls()


@app.get("/api/trades/recent")
def trades():
    return dash.trades()


@app.get("/api/system/metrics")
def system_metrics():
    return dash.system_metrics()


@app.post("/start")
def start():
    started = _start_worker()
    return jsonify({"started": started, **_state})


@app.post("/stop")
def stop():
    _stop_worker()
    return jsonify({"stopped": True, **_state})


@app.get("/status")
def status():
    return jsonify(_state)


@app.post("/config")
def update_config():
    data = request.json or {}
    if "cycle_seconds" in data:
        try:
            hybrid.CYCLE_SECONDS = max(1, int(data["cycle_seconds"]))
        except Exception:
            pass
    if "timeframes" in data:
        tfs = [t.strip() for t in str(data["timeframes"]).split(",") if t.strip()]
        if tfs:
            invalid = [t for t in tfs if t not in _allowed_timeframes]
            valid = [t for t in tfs if t in _allowed_timeframes]
            if valid:
                hybrid.CYCLE_TIMEFRAMES = valid
            _state["invalid_timeframes"] = invalid
    _state["cycle_seconds"] = getattr(hybrid, "CYCLE_SECONDS", _state["cycle_seconds"])
    _state["cycle_timeframes"] = getattr(hybrid, "CYCLE_TIMEFRAMES", _state["cycle_timeframes"])
    return jsonify({"ok": True, **_state})


if os.getenv("AUTO_START", "1") == "1":
    try:
        hybrid.init_database()
    except Exception:
        pass
    _start_worker()
