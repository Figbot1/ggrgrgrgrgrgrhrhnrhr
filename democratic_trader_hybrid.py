r"""
Democratic AI Trading (HYBRID / mostly-free)
- Loads env from D:\.env (or ENV_PATH)
- Uses local Ollama for high-volume free "agents"
- Optional remote APIs are supported but OFF by default (ENABLE_REMOTE=0)
- Logs EVERYTHING to SQLite: prompts, responses, latency, errors, predictions, weights
- Learns via multiplicative weights + EMA score per (provider/model/style/timeframe)
- Attempts ONE trade per cycle (default 60s) on Alpaca paper (if keys provided)
"""

import os, json, time, math, sqlite3, hashlib, re
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Tuple
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
import requests
try:
    import psutil  # optional telemetry
except Exception:
    psutil = None

DB_PATH = "trading_history.db"

THINKING_STYLES = [
    "deductive_reasoning", "inductive_reasoning", "abductive_reasoning",
    "analogy_based_reasoning", "lateral_thinking", "systems_thinking",
    "first_principles", "game_theory", "pattern_recognition",
    "psychological_profiling", "reverse_engineering", "red_teaming",
    "tarot_energy_prophet", "chosen"
]

TIMEFRAME_SECONDS = {
    "5s": 5, "30s": 30,
    "1m": 60, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "3h": 10800, "12h": 43200,
    "1d": 86400, "3d": 259200,
    "1w": 604800, "2w": 1209600,
    "1mo": 2592000, "3mo": 7776000, "6mo": 15552000,
    "1y": 31536000
}

DEFAULT_STOCKS = ["SPY","QQQ","AAPL","MSFT","TSLA","NVDA","AMZN","META","GOOGL","AMD","NFLX","GME","AMC","PLTR","RIVN","LCID","COIN","HOOD","SOFI","NIO","DKNG"]
DEFAULT_CRYPTO = ["BTC","ETH","SOL","DOGE","SHIB","PEPE"]
CRYPTO_ID_MAP = {"BTC":"bitcoin","ETH":"ethereum","SOL":"solana","DOGE":"dogecoin","SHIB":"shiba-inu","PEPE":"pepe"}

def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")

def load_env_file(path: str):
    if not path or not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                os.environ.setdefault(k, v)
    except Exception:
        pass

ENV_PATH = os.getenv("ENV_PATH", r"D:\.env")
load_env_file(ENV_PATH)

CYCLE_SECONDS = int(os.getenv("CYCLE_SECONDS", "60"))
CYCLE_TIMEFRAMES = [x.strip() for x in os.getenv("CYCLE_TIMEFRAMES", "1m,5m,15m").split(",") if x.strip()]
ENABLE_REMOTE = os.getenv("ENABLE_REMOTE", "0") == "1"
FORCE_TRADE = os.getenv("FORCE_TRADE", "1") == "1"
FALLBACK_BTC_TRADE = os.getenv("FALLBACK_BTC_TRADE", "1") == "1"
FALLBACK_BTC_SYMBOL = os.getenv("FALLBACK_BTC_SYMBOL", "BTCUSD").strip().upper()
FALLBACK_BTC_NOTIONAL = float(os.getenv("FALLBACK_BTC_NOTIONAL", "10"))

STOP_LOSS_PERCENT = float(os.getenv("STOP_LOSS_PERCENT", "0.05"))
MAX_POSITION_PCT = float(os.getenv("MAX_POSITION_PCT", "0.50"))      # aggressive cap
PORTFOLIO_CAP = float(os.getenv("PORTFOLIO_CAP", "30"))              # pretend you're trading $30
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "6"))

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434").rstrip("/")
OLLAMA_MODELS = [m.strip() for m in os.getenv("OLLAMA_MODELS", "").split(",") if m.strip()]
OLLAMA_ENABLED = os.getenv("OLLAMA_ENABLED", "0") == "1"
OLLAMA_ALLOW_LOCALHOST = os.getenv("OLLAMA_ALLOW_LOCALHOST", "0") == "1"

APCA_DATA_BASE_URL = os.getenv("APCA_DATA_BASE_URL", "https://data.alpaca.markets").rstrip("/")
ENABLE_ALPHA_HUNTER = os.getenv("ENABLE_ALPHA_HUNTER", "1") == "1"
MAX_EXTRA_ASSETS = int(os.getenv("MAX_EXTRA_ASSETS", "15"))
CONSOLIDATE_ON_START = os.getenv("CONSOLIDATE_ON_START", "0") == "1"
CONSOLIDATE_EVERY_N_CYCLES = int(os.getenv("CONSOLIDATE_EVERY_N_CYCLES", "0"))
ENABLE_AI_MODEL_MANAGER = os.getenv("ENABLE_AI_MODEL_MANAGER", "0") == "1"
AI_MANAGER_DB_PATH = os.getenv("AI_MANAGER_DB_PATH", "ai_manager.db")

DYNAMIC_CRYPTO_IDS: Dict[str, str] = {}

# -----------------------------
# DB
# -----------------------------
def init_database():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    c.execute("PRAGMA temp_store=MEMORY")

    c.execute('''CREATE TABLE IF NOT EXISTS predictions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT,
        model TEXT,
        provider TEXT,
        thinking_style TEXT,
        asset TEXT,
        asset_type TEXT,
        timeframe TEXT,
        direction TEXT,
        confidence REAL,
        expected_change_percent REAL,
        reasoning TEXT,
        current_price REAL,
        target_price REAL,
        evaluated INTEGER DEFAULT 0,
        actual_price REAL,
        actual_change_percent REAL,
        correctness_score REAL,
        evaluation_timestamp TEXT,
        news_context TEXT,
        sentiment_score REAL
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT,
        asset TEXT,
        asset_type TEXT,
        action TEXT,
        quantity REAL,
        price REAL,
        stop_loss REAL,
        alpaca_order_id TEXT,
        meta TEXT
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS model_calls (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT,
        provider TEXT,
        model TEXT,
        thinking_style TEXT,
        timeframe TEXT,
        prompt TEXT,
        response TEXT,
        ok INTEGER,
        error TEXT,
        latency_ms INTEGER,
        key_fingerprint TEXT
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS expert_weights (
        expert_id TEXT PRIMARY KEY,
        provider TEXT,
        model TEXT,
        thinking_style TEXT,
        timeframe TEXT,
        weight REAL,
        ema_score REAL,
        samples INTEGER,
        last_updated TEXT
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS market_data (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT,
        asset TEXT,
        asset_type TEXT,
        price REAL,
        source TEXT,
        meta TEXT
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS system_metrics (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT,
        cycle_ms INTEGER,
        assets_count INTEGER,
        news_count INTEGER,
        picks_stored INTEGER,
        evaluated_count INTEGER,
        traded INTEGER,
        fail_counts TEXT,
        memory_mb REAL,
        cpu_pct REAL
    )''')

    conn.commit()
    conn.close()

def db_exec(query: str, args=()):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(query, args)
    conn.commit()
    conn.close()

def db_fetchall(query: str, args=()) -> List[tuple]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(query, args)
    rows = c.fetchall()
    conn.close()
    return rows

def log_market_snapshot(market_data: Dict[str, Any], source: str):
    if not market_data:
        return
    ts = now_iso()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    for asset, info in market_data.items():
        c.execute('''INSERT INTO market_data (timestamp, asset, asset_type, price, source, meta)
                     VALUES (?, ?, ?, ?, ?, ?)''',
                  (ts, asset, info.get("type"), info.get("price"), source, json.dumps(info)))
    conn.commit()
    conn.close()

def log_system_metrics(cycle_ms: int, assets_count: int, news_count: int, picks_stored: int,
                       evaluated_count: int, traded: bool, fail_counts: Dict[str, int]):
    mem_mb = None
    cpu_pct = None
    if psutil:
        try:
            mem_mb = psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024)
            cpu_pct = psutil.cpu_percent(interval=None)
        except Exception:
            mem_mb = None
            cpu_pct = None
    db_exec('''INSERT INTO system_metrics
               (timestamp, cycle_ms, assets_count, news_count, picks_stored, evaluated_count, traded, fail_counts, memory_mb, cpu_pct)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (now_iso(), cycle_ms, assets_count, news_count, picks_stored,
             evaluated_count, 1 if traded else 0, json.dumps(fail_counts),
             mem_mb, cpu_pct))

def run_data_consolidation():
    if not (CONSOLIDATE_ON_START or CONSOLIDATE_EVERY_N_CYCLES):
        return
    try:
        from data_consolidator import DataConsolidator
        dc = DataConsolidator()
        dc.scan_all_databases()
        dc.scan_all_logs()
        for db_path in dc.found_databases:
            dc.consolidate_database(db_path)
        for log_path in dc.found_logs:
            dc.consolidate_json_logs(log_path)
        dc.analyze_consolidated_data()
    except Exception as e:
        print(f"Data consolidation skipped: {e}")

def ensure_ai_manager_schema(db_path: str):
    conn = sqlite3.connect(db_path)
    conn.execute('''CREATE TABLE IF NOT EXISTS predictions (
        model_id TEXT,
        asset TEXT,
        timeframe TEXT,
        prediction REAL,
        confidence REAL,
        thinking_style TEXT,
        reasoning TEXT,
        timestamp TEXT,
        correct INTEGER DEFAULT 0
    )''')
    conn.commit()
    conn.close()

def collect_ai_manager_predictions(market_data: Dict[str, Any], timeframe: str) -> int:
    if not ENABLE_AI_MODEL_MANAGER:
        return 0
    try:
        import asyncio
        from ai_model_manager import AIModelManager
    except Exception as e:
        print(f"AIModelManager unavailable: {e}")
        return 0

    ensure_ai_manager_schema(AI_MANAGER_DB_PATH)

    async def _run():
        async with AIModelManager(db_path=AI_MANAGER_DB_PATH) as manager:
            preds = await manager.get_all_predictions(market_data)
            if preds:
                manager.store_predictions(preds)
            return preds

    try:
        predictions = asyncio.run(_run())
    except Exception as e:
        print(f"AIModelManager run failed: {e}")
        return 0

    stored = 0
    for pred_data in predictions or []:
        agent_id = pred_data.get("agent_id", "ai_manager")
        style = pred_data.get("thinking_style", "unknown")
        model_full = agent_id
        provider = agent_id.split("_", 1)[0] if "_" in agent_id else "ai_manager"
        for pred in pred_data.get("predictions", []):
            asset = (pred.get("asset") or "").upper()
            if asset not in market_data:
                continue
            exp = float(pred.get("increase_percent") or 0)
            direction = "up" if exp >= 0 else "down"
            pick = [{
                "asset": asset,
                "direction": direction,
                "confidence": float(pred.get("confidence") or 0.5),
                "expected_change_percent": abs(exp),
                "reasoning": pred.get("reasoning") or "",
            }]
            store_predictions(model_full, provider, agent_id, style, timeframe, pick, market_data, [])
            stored += 1
    return stored

def log_model_call(provider, model, thinking_style, timeframe, prompt, response, ok, error, latency_ms, key_fp):
    db_exec('''INSERT INTO model_calls
        (timestamp, provider, model, thinking_style, timeframe, prompt, response, ok, error, latency_ms, key_fingerprint)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        (now_iso(), provider, model, thinking_style, timeframe, prompt, response,
         1 if ok else 0, error, latency_ms, key_fp)
    )

def expert_id(provider, model, style, timeframe) -> str:
    return f"{provider}:{model}:{style}:{timeframe}"

def get_or_init_weight(provider, model, style, timeframe, init_w=1.0) -> Tuple[str, float, float, int]:
    eid = expert_id(provider, model, style, timeframe)
    rows = db_fetchall("SELECT weight, ema_score, samples FROM expert_weights WHERE expert_id=?", (eid,))
    if rows:
        w, ema, n = rows[0]
        return eid, float(w), float(ema), int(n)
    db_exec('''INSERT INTO expert_weights
        (expert_id, provider, model, thinking_style, timeframe, weight, ema_score, samples, last_updated)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        (eid, provider, model, style, timeframe, float(init_w), 0.5, 0, now_iso())
    )
    return eid, float(init_w), 0.5, 0

def update_expert_weight(eid: str, reward: float, eta=0.6, ema_alpha=0.05):
    reward = max(-1.0, min(1.0, reward))
    rows = db_fetchall("SELECT weight, ema_score, samples FROM expert_weights WHERE expert_id=?", (eid,))
    if not rows:
        return
    w, ema, n = float(rows[0][0]), float(rows[0][1]), int(rows[0][2])
    w_new = max(1e-6, w * math.exp(eta * reward))
    score01 = (reward + 1) / 2
    ema_new = (1-ema_alpha) * ema + ema_alpha * score01
    db_exec("""UPDATE expert_weights SET weight=?, ema_score=?, samples=?, last_updated=? WHERE expert_id=?""",
            (w_new, ema_new, n+1, now_iso(), eid))

# -----------------------------
# Market data
# -----------------------------
class AlpacaClient:
    def __init__(self):
        self.api_key = os.getenv("APCA_API_KEY_ID")
        self.api_secret = os.getenv("APCA_API_SECRET_KEY")
        base = os.getenv("APCA_API_BASE_URL", "https://paper-api.alpaca.markets").rstrip("/")
        if base.endswith("/v2"):
            base = base[:-3]
        self.base_url = base
        self.data_base_url = APCA_DATA_BASE_URL
        self.headers = {"APCA-API-KEY-ID": self.api_key or "", "APCA-API-SECRET-KEY": self.api_secret or ""}

    def ok(self) -> bool:
        return bool(self.api_key and self.api_secret)

    def get_account(self) -> Dict[str, Any]:
        if not self.ok():
            return {}
        try:
            r = requests.get(f"{self.base_url}/v2/account", headers=self.headers, timeout=10)
            return r.json() if r.status_code == 200 else {}
        except:
            return {}

    def get_latest_trade(self, symbol: str) -> Optional[float]:
        if not self.ok():
            return None
        try:
            r = requests.get(f"{self.data_base_url}/v2/stocks/{symbol}/trades/latest",
                             headers=self.headers, timeout=10)
            if r.status_code == 200:
                return float(r.json().get("trade", {}).get("p", 0)) or None
        except:
            pass
        try:
            r = requests.get(f"{self.base_url}/v2/stocks/{symbol}/trades/latest", headers=self.headers, timeout=10)
            if r.status_code == 200:
                return float(r.json().get("trade", {}).get("p", 0)) or None
        except:
            pass
        return None

    def place_order(self, symbol: str, qty: int, side: str, order_type="market") -> Dict[str, Any]:
        if not self.ok():
            return {"error": "Alpaca keys missing"}
        data = {"symbol": symbol, "qty": qty, "side": side, "type": order_type, "time_in_force": "day"}
        try:
            r = requests.post(f"{self.base_url}/v2/orders", headers=self.headers, json=data, timeout=10)
            if r.status_code in (200, 201):
                return r.json()
            return {"error": r.text[:300]}
        except Exception as e:
            return {"error": str(e)}

def get_coingecko_prices(symbols: List[str]) -> Dict[str, float]:
    ids = []
    sym_to_id = {}
    for s in symbols:
        cid = CRYPTO_ID_MAP.get(s.upper()) or DYNAMIC_CRYPTO_IDS.get(s.upper())
        if cid:
            ids.append(cid)
            sym_to_id[s.upper()] = cid
    if not ids:
        return {}
    url = "https://api.coingecko.com/api/v3/simple/price"
    try:
        r = requests.get(url, params={"ids": ",".join(ids), "vs_currencies": "usd"}, timeout=10)
        if r.status_code != 200:
            return {}
        data = r.json()
        out = {}
        for sym, cid in sym_to_id.items():
            out[sym] = float(data.get(cid, {}).get("usd", 0)) or 0
        return out
    except:
        return {}

# -----------------------------
# Alpha discovery (lightweight)
# -----------------------------
ALPHA_SUBREDDITS = [
    "wallstreetbets", "stocks", "pennystocks", "smallstreetbets",
    "cryptocurrency", "cryptomoonshots", "satoshistreetbets"
]

_TICKER_EXCLUDE = {
    "THE","AND","FOR","ARE","BUT","NOT","YOU","ALL","CAN","HER","WAS","ONE","OUR","OUT","HAS",
    "HIS","HOW","ITS","MAY","NEW","NOW","OLD","SEE","WAY","WHO","BOY","DID","GET","HIM","LET",
    "PUT","SAY","SHE","TOO","USE","CEO","IPO","ETF","SEC","FDA","USA","NYSE","NASDAQ","WSB",
    "IMO","YOLO","FOMO","HODL","MOON","PUMP","DUMP","BEAR","BULL","AI","USD"
}

def extract_tickers(text: str) -> List[str]:
    if not text:
        return []
    candidates = set(re.findall(r"\$([A-Z]{2,5})\b", text.upper()))
    # also include bare all-caps words
    candidates |= set(re.findall(r"\b([A-Z]{2,5})\b", text.upper()))
    return [t for t in candidates if t not in _TICKER_EXCLUDE]

def coingecko_trending_symbols() -> List[str]:
    url = "https://api.coingecko.com/api/v3/search/trending"
    out = []
    try:
        r = requests.get(url, timeout=10)
        if r.status_code != 200:
            return out
        data = r.json()
        for coin in data.get("coins", [])[:10]:
            item = coin.get("item", {})
            sym = (item.get("symbol") or "").upper()
            cid = item.get("id")
            if sym and cid:
                DYNAMIC_CRYPTO_IDS.setdefault(sym, cid)
                out.append(sym)
    except Exception:
        return out
    return out

def discover_assets(base_assets: List[str], max_extra: int = MAX_EXTRA_ASSETS) -> List[str]:
    if not ENABLE_ALPHA_HUNTER:
        return base_assets
    discovered = set(base_assets)

    # Reddit tickers
    for sub in ALPHA_SUBREDDITS:
        titles = fetch_reddit_hot(sub, limit=10)
        for t in titles:
            for sym in extract_tickers(t):
                discovered.add(sym)

    # RSS tickers
    for t in fetch_rss("https://feeds.finance.yahoo.com/rss/2.0/headline?s=SPY&region=US&lang=en-US"):
        for sym in extract_tickers(t):
            discovered.add(sym)

    # Crypto trending
    for sym in coingecko_trending_symbols():
        discovered.add(sym)

    # Cap extras to avoid exploding the universe
    extra = [s for s in discovered if s not in base_assets]
    if max_extra and len(extra) > max_extra:
        extra = extra[:max_extra]
    return base_assets + extra

# -----------------------------
# News (free)
# -----------------------------
def simple_sentiment(text: str) -> float:
    pos = ["surge","rally","gain","jump","soar","boom","breakout","bull","bullish","upgrade","beats","record"]
    neg = ["crash","plunge","drop","fall","decline","bear","bearish","downgrade","misses","fraud","lawsuit","hack"]
    t = (text or "").lower()
    pc = sum(1 for w in pos if w in t)
    nc = sum(1 for w in neg if w in t)
    tot = pc + nc
    return 0.0 if tot == 0 else (pc - nc) / tot

def fetch_rss(url: str, timeout=10) -> List[str]:
    try:
        r = requests.get(url, timeout=timeout, headers={"User-Agent":"Mozilla/5.0"})
        if r.status_code != 200:
            return []
        xml = r.text
        titles = []
        for chunk in xml.split("<item>")[1:25]:
            if "<title>" in chunk:
                t = chunk.split("<title>",1)[1].split("</title>",1)[0].strip()
                if t:
                    titles.append(t[:220])
        return titles
    except:
        return []

def fetch_reddit_hot(subreddit: str, limit=15) -> List[str]:
    url = f"https://www.reddit.com/r/{subreddit}/hot.json"
    try:
        r = requests.get(url, params={"limit":limit}, timeout=10, headers={"User-Agent":"demo-bot/0.1"})
        if r.status_code != 200:
            return []
        data = r.json()
        out = []
        for c in data.get("data", {}).get("children", [])[:limit]:
            t = c.get("data", {}).get("title", "")
            if t:
                out.append(t[:220])
        return out
    except:
        return []

def collect_news(assets: List[str]) -> List[Dict[str, Any]]:
    rss_sources = [
        ("crypto", "https://cointelegraph.com/rss"),
        ("crypto", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
        ("markets", "https://feeds.finance.yahoo.com/rss/2.0/headline?s=SPY&region=US&lang=en-US"),
    ]
    items: List[Dict[str, Any]] = []
    for tag, url in rss_sources:
        for t in fetch_rss(url):
            items.append({"asset": tag, "title": t, "source": url, "sentiment": simple_sentiment(t)})

    for sub in ["wallstreetbets", "stocks", "cryptocurrency"]:
        for t in fetch_reddit_hot(sub, limit=12):
            items.append({"asset": sub, "title": t, "source": f"reddit:{sub}", "sentiment": simple_sentiment(t)})

    aset = set(a.upper() for a in assets)
    out: List[Dict[str, Any]] = []
    for n in items:
        title_u = n["title"].upper()
        matched = [a for a in aset if a in title_u]
        if matched:
            for a in matched[:2]:
                out.append({**n, "asset": a})
        else:
            out.append(n)
    return out[:80]

# -----------------------------
# Models: Ollama (free)
# -----------------------------
def ollama_chat(model: str, prompt: str, temperature=0.7, timeout=120) -> str:
    url = f"{OLLAMA_URL}/api/chat"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "options": {"temperature": temperature},
        "stream": False,  # important: return ONE JSON object, not JSONL stream
    }
    r = requests.post(url, json=payload, timeout=timeout)
    r.raise_for_status()

    try:
        data = r.json()
    except Exception:
        # If server streamed anyway, parse the last JSON object line
        lines = [ln for ln in (r.text or "").splitlines() if ln.strip()]
        if not lines:
            return ""
        data = json.loads(lines[-1])

    return (data.get("message") or {}).get("content") or ""

# -----------------------------
# Prompt + parsing
# -----------------------------
def get_expert_history(model_full: str, style: str, timeframe: str) -> str:
    rows = db_fetchall('''SELECT correctness_score, evaluated
                          FROM predictions
                          WHERE model=? AND thinking_style=? AND timeframe=?
                          ORDER BY timestamp DESC LIMIT 30''',
                       (model_full, style, timeframe))
    if not rows:
        return "No history yet."
    evald = [r[0] for r in rows if int(r[1]) == 1 and r[0] is not None]
    if not evald:
        return "History exists but not evaluated yet."
    return f"Last {len(evald)} evals avg_score={sum(evald)/len(evald):.3f}"

def get_agent_performance_summary(model_full: str, style: str, timeframe: str) -> str:
    rows = db_fetchall('''SELECT COUNT(*),
                                 SUM(CASE WHEN evaluated=1 AND correctness_score >= 0.7 THEN 1 ELSE 0 END),
                                 AVG(CASE WHEN evaluated=1 THEN correctness_score ELSE NULL END)
                          FROM predictions
                          WHERE model=? AND thinking_style=? AND timeframe=?''',
                       (model_full, style, timeframe))
    total, correct, avg_score = rows[0] if rows else (0, 0, None)
    total = int(total or 0)
    correct = int(correct or 0)
    acc = (correct / total) if total else 0.0
    avg_score = float(avg_score or 0.0)
    return f"total={total} acc={acc:.2f} avg_score={avg_score:.2f}"

def leaderboard_top(limit=10) -> str:
    rows = db_fetchall('''SELECT provider, model, thinking_style, timeframe, weight, ema_score, samples
                          FROM expert_weights
                          ORDER BY (ema_score * weight) DESC
                          LIMIT ?''', (limit,))
    if not rows:
        return "No leaderboard yet."
    lines = []
    for i, r in enumerate(rows, 1):
        lines.append(f"{i}. {r[0]}:{r[1]}:{r[2]}:{r[3]} ema={float(r[5]):.3f} w={float(r[4]):.3f} n={int(r[6])}")
    return "\n".join(lines)

def build_prompt(market_data: Dict[str, Any], news: List[Dict[str,Any]], style: str, timeframe: str, assets: List[str], model_full: str) -> str:
    news_str = "\n".join([f"- {n.get('asset','?')}: {n.get('title','')[:160]} (s={n.get('sentiment',0):.2f})" for n in news[:12]])
    hist = get_expert_history(model_full, style, timeframe)
    perf = get_agent_performance_summary(model_full, style, timeframe)
    lb = leaderboard_top(10)
    compact_market = {k: market_data[k] for k in list(market_data.keys())[:60]}

    return f"""
You are an agent in a democratic AI trading system.

TIMEFRAME: {timeframe}
THINKING STYLE: {style}

ASSETS (choose from this list only):
{", ".join(assets)}

MARKET SNAPSHOT (price only):
{json.dumps(compact_market, indent=2)[:3500]}

RECENT NEWS + SOCIAL:
{news_str}

YOUR HISTORY:
{hist}

YOUR PERFORMANCE SUMMARY:
{perf}

LEADERBOARD (top experts):
{lb}

TASK:
Pick TOP 3 assets most likely to increase over the TIMEFRAME.
Return JSON only (no markdown, no code fences):
{{
  "picks":[
    {{
      "asset":"TSLA",
      "direction":"up",
      "confidence":0.72,
      "expected_change_percent":1.2,
      "reasoning":"concise"
    }}
  ]
}}
""".strip()

def parse_json_object(text: str) -> Optional[dict]:
    if not text:
        return None
    # strip code fences if any
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
    a = t.find("{")
    b = t.rfind("}")
    if a < 0 or b <= a:
        return None
    try:
        return json.loads(t[a:b+1])
    except:
        return None

def parse_picks(text: str) -> List[Dict[str, Any]]:
    obj = parse_json_object(text)
    if not obj:
        return []
    picks = obj.get("picks", [])
    if not isinstance(picks, list):
        return []
    out = []
    for p in picks[:3]:
        if isinstance(p, dict):
            out.append(p)
    return out

def store_predictions(model_full: str, provider: str, model: str, style: str, timeframe: str,
                      picks: List[Dict[str,Any]], market_data: Dict[str,Any], news: List[Dict[str,Any]]):
    ts = now_iso()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    for p in picks:
        asset = (p.get("asset") or "").upper().strip()
        if asset not in market_data:
            continue
        info = market_data[asset]
        asset_type = info.get("type","stock")
        current_price = float(info.get("price") or 0)

        direction = (p.get("direction") or "up").lower().strip()
        confidence = float(p.get("confidence") or 0)
        exp = float(p.get("expected_change_percent") or 0)
        reasoning = (p.get("reasoning") or "")[:700]

        signed = exp if direction == "up" else -abs(exp)
        target_price = current_price * (1 + signed/100)

        asset_news = [n for n in news if n.get("asset") == asset][:12]
        avg_sent = sum(n.get("sentiment",0) for n in asset_news) / len(asset_news) if asset_news else 0

        c.execute('''INSERT INTO predictions
            (timestamp, model, provider, thinking_style, asset, asset_type, timeframe, direction, confidence,
             expected_change_percent, reasoning, current_price, target_price, news_context, sentiment_score)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (ts, model_full, provider, style, asset, asset_type, timeframe, direction, confidence,
             abs(exp), reasoning, current_price, target_price, json.dumps(asset_news), float(avg_sent))
        )

    conn.commit()
    conn.close()

# -----------------------------
# Scoring + Evaluation + Learning
# -----------------------------
def correctness_score(predicted_change, actual_change, confidence):
    if predicted_change == 0 or actual_change == 0:
        return 0.5
    same_dir = (predicted_change * actual_change) > 0
    if not same_dir:
        magnitude_penalty = min(abs(actual_change), 5) / 5
        return max(0, 0.3 - (magnitude_penalty * 0.3))
    magnitude_error = abs(predicted_change - actual_change) / max(abs(actual_change), 0.1)
    magnitude_score = max(0, 1 - (magnitude_error / 2))
    confidence_boost = (confidence - 0.5) * 0.2 if confidence > 0.5 else 0
    return min(1.0, magnitude_score + confidence_boost)


# -----------------------------
# Market-data helpers (no external providers)
# -----------------------------
def _tf_to_alpaca(tf: str) -> str:
    """Map Figbot shorthand to Alpaca timeframe strings."""
    tf = (tf or "").strip().lower()
    if tf.endswith("s"):  # Alpaca REST bars don't support seconds; fall back to 1Min
        return "1Min"
    if tf.endswith("m"):
        n = int(tf[:-1] or "1")
        return f"{n}Min"
    if tf.endswith("h"):
        n = int(tf[:-1] or "1")
        return f"{n}Hour"
    if tf in ("1d","d","day"):
        return "1Day"
    return "1Min"

def _alpaca_get_json(url: str, headers: Dict[str,str], params: Optional[Dict[str,Any]]=None, timeout=10) -> Optional[Dict[str,Any]]:
    try:
        r = requests.get(url, headers=headers, params=params, timeout=timeout)
        if r.status_code >= 400:
            return None
        return r.json()
    except Exception:
        return None

def _alpaca_crypto_mid_from_orderbook(alpaca: "AlpacaClient", symbol: str) -> Optional[float]:
    """Use Alpaca crypto orderbook (best bid/ask mid) for robust pricing."""
    url = f"{alpaca.data_base_url}/v1beta3/crypto/us/latest/orderbooks"
    j = _alpaca_get_json(url, headers=alpaca.headers(), params={"symbols": symbol})
    if not j:
        return None
    ob = (j.get("orderbooks") or {}).get(symbol)
    if not ob:
        return None
    bids = ob.get("b") or []
    asks = ob.get("a") or []
    if not bids or not asks:
        return None
    bid = float(bids[0].get("p") or 0)
    ask = float(asks[0].get("p") or 0)
    if bid <= 0 or ask <= 0:
        return None
    return (bid + ask) / 2.0

def _alpaca_stock_latest_price(alpaca: "AlpacaClient", symbol: str) -> Optional[float]:
    try:
        t = alpaca.get_latest_trade(symbol)
        return float(t.get("price")) if t and t.get("price") is not None else None
    except Exception:
        return None

def _alpaca_crypto_bars(alpaca: "AlpacaClient", symbol: str, tf: str, limit: int = 3) -> Optional[List[Dict[str,Any]]]:
    url = f"{alpaca.data_base_url}/v1beta3/crypto/us/bars"
    j = _alpaca_get_json(url, headers=alpaca.headers(), params={"symbols": symbol, "timeframe": _tf_to_alpaca(tf), "limit": str(limit), "sort": "desc"})
    if not j:
        return None
    bars = (j.get("bars") or {}).get(symbol)
    return bars if isinstance(bars, list) else None

def _alpaca_stock_bars(alpaca: "AlpacaClient", symbol: str, tf: str, limit: int = 3) -> Optional[List[Dict[str,Any]]]:
    url = f"{alpaca.data_base_url}/v2/stocks/{symbol}/bars"
    j = _alpaca_get_json(url, headers=alpaca.headers(), params={"timeframe": _tf_to_alpaca(tf), "limit": str(limit), "sort": "desc"})
    if not j:
        return None
    bars = j.get("bars")
    return bars if isinstance(bars, list) else None

def _recent_return_from_bars(bars: Optional[List[Dict[str,Any]]]) -> Optional[float]:
    if not bars or len(bars) < 2:
        return None
    def close(bar):
        return float(bar.get("c") or 0)
    c0 = close(bars[0])
    c1 = close(bars[1])
    if c0 <= 0 or c1 <= 0:
        return None
    return (c0 / c1) - 1.0

def baseline_momentum_picks(alpaca: "AlpacaClient", tf: str, assets: List[str], asset_types: Dict[str,str], max_picks: int = 8) -> List[Dict[str,Any]]:
    """Rule-based fallback when no LLM is configured: short-horizon momentum."""
    picks: List[Dict[str,Any]] = []
    for a in assets:
        at = (asset_types.get(a) or ("crypto" if "/" in a else "stock")).lower()
        bars = _alpaca_crypto_bars(alpaca, a, tf) if at == "crypto" else _alpaca_stock_bars(alpaca, a, tf)
        r = _recent_return_from_bars(bars)
        if r is None:
            continue
        if abs(r) < 0.0002:
            continue
        direction = "up" if r >= 0 else "down"
        conf = min(0.99, max(0.05, abs(r) * 150.0))
        picks.append({
            "asset": a,
            "direction": direction,
            "confidence": conf,
            "expected_change_percent": r * 100.0,
            "reasoning": f"Baseline momentum: last bar return={r*100.0:.3f}%"
        })
    picks.sort(key=lambda x: float(x.get("confidence") or 0), reverse=True)
    return picks[:max_picks]

def get_price(alpaca: AlpacaClient, asset: str, asset_type: str) -> Optional[float]:
    asset = (asset or "").upper().strip()
    asset_type = (asset_type or ("crypto" if "/" in asset else "stock")).lower().strip()

    # Prefer Alpaca market data (works in Railway without extra providers).
    if asset_type == "crypto":
        p = _alpaca_crypto_mid_from_orderbook(alpaca, asset)
        if p:
            return float(p)
        # last resort: coingecko
        try:
            return get_coingecko_prices([asset]).get(asset.upper()) or None
        except Exception:
            return None

    # stock
    p = _alpaca_stock_latest_price(alpaca, asset)
    if p:
        return float(p)
    return None

def evaluate_predictions(alpaca: AlpacaClient) -> int:
    rows = db_fetchall('''SELECT id, timestamp, model, provider, thinking_style, asset, asset_type, timeframe,
                                 direction, expected_change_percent, confidence, current_price
                          FROM predictions
                          WHERE evaluated=0
                          ORDER BY timestamp ASC
                          LIMIT 2500''')
    done = 0
    now = datetime.now()

    for row in rows:
        (pid, ts, model_full, provider, style, asset, asset_type, tf, direction, exp_abs, conf, start_price) = row
        seconds = TIMEFRAME_SECONDS.get(tf, 60)
        try:
            t0 = datetime.fromisoformat(ts)
        except:
            continue
        if (now - t0).total_seconds() < seconds:
            continue

        cur = get_price(alpaca, asset, asset_type)
        if not cur or not start_price:
            continue

        actual_change = ((float(cur) - float(start_price)) / float(start_price)) * 100.0
        predicted_change = float(exp_abs) if direction == "up" else -float(exp_abs)
        score = correctness_score(predicted_change, actual_change, float(conf))

        db_exec('''UPDATE predictions
                   SET evaluated=1, actual_price=?, actual_change_percent=?, correctness_score=?, evaluation_timestamp=?
                   WHERE id=?''',
                (float(cur), float(actual_change), float(score), now_iso(), pid))
        done += 1

        # learning update
        model_name = model_full.split(":",1)[1] if ":" in model_full else model_full
        eid, _, _, _ = get_or_init_weight(provider, model_name, style, tf)
        reward = (score * 2.0) - 1.0  # map 0..1 => -1..1
        update_expert_weight(eid, reward)

    return done

# -----------------------------
# Democracy -> trade selection
# -----------------------------
def position_qty(account_value: float, price: float) -> int:
    max_notional = max(1.0, account_value * MAX_POSITION_PCT)
    return max(1, int(max_notional / max(price, 1e-9)))

def choose_trade(alpaca: AlpacaClient, timeframe_focus: str, lookback_seconds=180) -> Optional[Dict[str,Any]]:
    cutoff = (datetime.now() - timedelta(seconds=lookback_seconds)).isoformat(timespec="seconds")
    rows = db_fetchall('''SELECT model, provider, thinking_style, asset, asset_type, timeframe, direction,
                                 confidence, expected_change_percent
                          FROM predictions
                          WHERE timestamp >= ? AND timeframe = ?
                          ORDER BY timestamp DESC''', (cutoff, timeframe_focus))
    if not rows:
        return None

    bucket: Dict[tuple, Dict[str, float]] = {}
    for (model_full, provider, style, asset, asset_type, tf, direction, conf, exp_abs) in rows:
        model_name = model_full.split(":",1)[1] if ":" in model_full else model_full
        eid, w, ema, _ = get_or_init_weight(provider, model_name, style, tf)

        signed = float(exp_abs) if direction == "up" else -float(exp_abs)
        vote_w = max(1e-6, w) * max(0.05, ema) * max(0.05, float(conf))
        k = (asset, asset_type, tf)
        if k not in bucket:
            bucket[k] = {"sum_w":0.0, "sum_signed":0.0, "sum_abs":0.0, "count":0.0}
        bucket[k]["sum_w"] += vote_w
        bucket[k]["sum_signed"] += vote_w * signed
        bucket[k]["sum_abs"] += vote_w * abs(signed)
        bucket[k]["count"] += 1.0

    cands = []
    for (asset, asset_type, tf), b in bucket.items():
        if b["sum_w"] <= 0:
            continue
        weighted_change = b["sum_signed"] / b["sum_w"]
        consensus = abs(b["sum_signed"]) / max(1e-9, b["sum_abs"])
        cands.append((asset, asset_type, tf, weighted_change, consensus, int(b["count"])))

    cands.sort(key=lambda x: (x[3]*x[4], x[4], x[5]), reverse=True)
    if not cands:
        return None

    asset, asset_type, tf, weighted_change, consensus, votes = cands[0]

    acct = alpaca.get_account() if alpaca.ok() else {}
    pv = float(acct.get("portfolio_value") or PORTFOLIO_CAP)
    pv = min(pv, PORTFOLIO_CAP)

    price = get_price(alpaca, asset, asset_type)
    if not price:
        return None

    side = "buy" if weighted_change >= 0 else "sell"
    qty = position_qty(pv, float(price))
    stop_loss = float(price) * (1 - STOP_LOSS_PERCENT if side == "buy" else 1 + STOP_LOSS_PERCENT)

    return {
        "asset": asset, "asset_type": asset_type, "timeframe": tf,
        "expected_change": weighted_change, "consensus": consensus, "votes": votes,
        "side": side, "qty": qty, "price": float(price), "stop_loss": stop_loss
    }

def record_trade(tr: Dict[str,Any], alpaca_order_id: Optional[str]):
    db_exec('''INSERT INTO trades (timestamp, asset, asset_type, action, quantity, price, stop_loss, alpaca_order_id, meta)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (now_iso(), tr["asset"], tr["asset_type"], tr["side"], tr["qty"], tr["price"], tr["stop_loss"],
             alpaca_order_id, json.dumps(tr)))

# -----------------------------
# Cycle
# -----------------------------
def run_cycle(tf: str):
    alpaca = AlpacaClient()

    stocks = [x.strip().upper() for x in os.getenv("STOCK_ASSETS", ",".join(DEFAULT_STOCKS)).split(",") if x.strip()]
    crypto = [x.strip().upper() for x in os.getenv("CRYPTO_ASSETS", ",".join(DEFAULT_CRYPTO)).split(",") if x.strip()]
    assets = discover_assets(list(dict.fromkeys(stocks + crypto)))
    stocks = [a for a in assets if a not in CRYPTO_ID_MAP and a not in DYNAMIC_CRYPTO_IDS]
    crypto = [a for a in assets if a in CRYPTO_ID_MAP or a in DYNAMIC_CRYPTO_IDS]

    # prices
    market_data: Dict[str,Any] = {}
    if alpaca.ok():
        for s in stocks:
            px = alpaca.get_latest_trade(s)
            if px:
                market_data[s] = {"type":"stock", "price": float(px)}
    cpx = get_coingecko_prices(crypto)
    for sym, px in cpx.items():
        if px:
            market_data[sym] = {"type":"crypto", "price": float(px)}

    log_market_snapshot(market_data, "alpaca+coingecko")
    news = collect_news(list(market_data.keys()))
    # If no LLM was used (common on Railway) we still want a live, functional bot.
    # Generate rule-based momentum picks from Alpaca bars and store them as a "baseline" model.
    if stored == 0:
        asset_types = {a: ("crypto" if "/" in a else "stock") for a in ASSETS}
        picks = baseline_momentum_picks(alpaca, tf, ASSETS, asset_types, max_picks=8)
        if picks:
            try:
                store_predictions("baseline:momentum", "internal", "momentum", "rule", tf, picks, market_data, news)
                stored = len(picks)
            except Exception:
                pass

    evaluated = evaluate_predictions(alpaca)

    # model tasks (ollama only unless ENABLE_REMOTE=1 later)
    tasks = []
    ollama_localhost = OLLAMA_URL.startswith("http://localhost") or OLLAMA_URL.startswith("http://127.")
    ollama_ready = OLLAMA_ENABLED and OLLAMA_MODELS and (OLLAMA_ALLOW_LOCALHOST or not ollama_localhost)
    if ollama_ready:
        for m in OLLAMA_MODELS:
            for style in THINKING_STYLES:
                model_full = f"ollama:{m}"
                prompt = build_prompt(market_data, news, style, tf, list(market_data.keys()), model_full)
                tasks.append((m, style, prompt, model_full))

    fail_counts = {"no_text":0, "parse_fail":0, "exceptions":0}
    stored = 0
    t_start = time.time()

    def worker(model_name: str, style: str, timeframe: str, prompt: str) -> Tuple[bool, str, str]:
        # returns (ok, response_text, error)
        t0 = time.time()
        try:
            resp = ollama_chat(model_name, prompt, temperature=0.7, timeout=180)
            latency = int((time.time()-t0)*1000)
            log_model_call("ollama", model_name, style, timeframe, prompt, resp, True, None, latency, None)
            return True, resp, ""
        except Exception as e:
            latency = int((time.time()-t0)*1000)
            log_model_call("ollama", model_name, style, timeframe, prompt, "", False, str(e)[:220], latency, None)
            return False, "", str(e)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = []
        for (m, style, prompt, model_full) in tasks:
            futures.append((m, style, prompt, model_full, ex.submit(worker, m, style, tf, prompt)))

        for (m, style, prompt, model_full, fut) in futures:
            try:
                ok, text, err = fut.result()
            except Exception:
                fail_counts["exceptions"] += 1
                continue

            if not ok or not text:
                fail_counts["no_text"] += 1
                continue

            picks = parse_picks(text)
            if not picks:
                fail_counts["parse_fail"] += 1
                continue

            get_or_init_weight("ollama", m, style, tf)
            store_predictions(model_full, "ollama", m, style, tf, picks, market_data, news)
            stored += len(picks)

    stored += collect_ai_manager_predictions(market_data, tf)

    trade = choose_trade(alpaca, tf, lookback_seconds=max(180, CYCLE_SECONDS*3))
    did_trade = False
    alpaca_order_id = None

    if trade:
        print(f"💰 ENSEMBLE: {trade['side'].upper()} {trade['qty']} {trade['asset']} @ {trade['price']:.6f} "
              f"tf={trade['timeframe']} exp={trade['expected_change']:.2f}% cons={trade['consensus']:.2f} votes={trade['votes']}")
        if alpaca.ok():
            res = alpaca.place_order(trade["asset"], trade["qty"], trade["side"])
            if "error" in res:
                print(f"   ❌ Alpaca order failed: {res['error']}")
            else:
                alpaca_order_id = res.get("id")
                did_trade = True
                print(f"   ✅ Alpaca order ok: {alpaca_order_id or 'ok'}")
        else:
            if FORCE_TRADE:
                print("   ⚠️ Alpaca keys missing; cannot place trade.")
        if did_trade:
            record_trade(trade, alpaca_order_id)
    else:
        if stored == 0 and FALLBACK_BTC_TRADE and alpaca.ok():
            price = get_price(alpaca, FALLBACK_BTC_SYMBOL, "crypto")
            if price:
                qty = max(0.00000001, FALLBACK_BTC_NOTIONAL / float(price))
                buy_trade = {
                    "asset": FALLBACK_BTC_SYMBOL,
                    "asset_type": "crypto",
                    "timeframe": tf,
                    "expected_change": 0.0,
                    "consensus": 0.0,
                    "votes": 0,
                    "side": "buy",
                    "qty": qty,
                    "price": float(price),
                    "stop_loss": float(price) * (1 - STOP_LOSS_PERCENT),
                }
                res_buy = alpaca.place_order(buy_trade["asset"], buy_trade["qty"], "buy")
                if "error" in res_buy:
                    print(f"   ❌ Fallback BUY failed: {res_buy['error']}")
                else:
                    record_trade(buy_trade, res_buy.get("id"))
                    did_trade = True
                    res_sell = alpaca.place_order(buy_trade["asset"], buy_trade["qty"], "sell")
                    if "error" in res_sell:
                        print(f"   ❌ Fallback SELL failed: {res_sell['error']}")
                    else:
                        sell_trade = dict(buy_trade)
                        sell_trade["side"] = "sell"
                        record_trade(sell_trade, res_sell.get("id"))
                        did_trade = True
                        print(f"⚠️ Fallback round-trip {FALLBACK_BTC_SYMBOL} qty={qty:.8f}")
            else:
                print("⚠️ Fallback BTC trade skipped (price unavailable).")
        elif FORCE_TRADE:
            print("⚠️  FORCE_TRADE=1 but no trade signal (likely no predictions stored).")
        else:
            print("💤 No trade this cycle (FORCE_TRADE=0).")

    dur = int((time.time() - t_start)*1000)
    print(f"🧠 Cycle tf={tf} | assets={len(market_data)} | news={len(news)} | stored_picks={stored} | evaluated={evaluated} | duration={dur}ms | traded={did_trade} | fails={fail_counts}")
    log_system_metrics(dur, len(market_data), len(news), stored, evaluated, did_trade, fail_counts)

def main():
    init_database()
    print("="*88)
    print("DEMOCRATIC AI TRADING (HYBRID) - START")
    print(f"DB: {DB_PATH}")
    print(f"ENV: {ENV_PATH}")
    print(f"CYCLE_SECONDS={CYCLE_SECONDS}  ENABLE_REMOTE={int(ENABLE_REMOTE)}  FORCE_TRADE={int(FORCE_TRADE)}")
    print(f"OLLAMA_URL={OLLAMA_URL}  MODELS={','.join(OLLAMA_MODELS)}  OLLAMA_ENABLED={int(OLLAMA_ENABLED)}  MAX_WORKERS={MAX_WORKERS}")
    print("="*88)

    if CONSOLIDATE_ON_START:
        run_data_consolidation()

    i = 0
    while True:
        tf = CYCLE_TIMEFRAMES[i % len(CYCLE_TIMEFRAMES)] if CYCLE_TIMEFRAMES else "1m"
        if tf not in TIMEFRAME_SECONDS:
            tf = "1m"
        print("\n" + "-"*88)
        print(f"🔄 {now_iso()}  cycle={i+1}  timeframe={tf}")
        print("-"*88)
        try:
            run_cycle(tf)
        except KeyboardInterrupt:
            print("\n🛑 Shutdown")
            break
        except Exception as e:
            print(f"❌ Cycle error: {e}")
        i += 1
        if CONSOLIDATE_EVERY_N_CYCLES > 0 and (i % CONSOLIDATE_EVERY_N_CYCLES) == 0:
            run_data_consolidation()
        print(f"⏳ Sleeping {CYCLE_SECONDS}s...\n")
        time.sleep(CYCLE_SECONDS)

if __name__ == "__main__":
    main()
