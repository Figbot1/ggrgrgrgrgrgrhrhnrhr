Deployment Environment (Railway)

Required:
- APCA_API_KEY_ID
- APCA_API_SECRET_KEY
- APCA_API_BASE_URL=https://paper-api.alpaca.markets

Optional model provider:
- ENABLE_REMOTE=1
- REMOTE_MODEL_URL=https://your-model-endpoint
- REMOTE_MODEL_KEY=your-token (optional)
- REMOTE_MODEL_MODELS=model-a,model-b
- REMOTE_MODEL_TIMEOUT=45

Ollama (disabled by default):
- OLLAMA_ENABLED=1
- OLLAMA_MODELS=llama3.1:8b
- OLLAMA_URL=http://your-ollama-host
- OLLAMA_ALLOW_LOCALHOST=0

Fallback BTC trades (when no model signals):
- FALLBACK_BTC_TRADE=1
- FALLBACK_BTC_NOTIONAL=10
- FALLBACK_BTC_ASSET=BTC
- FALLBACK_BTC_ORDER_SYMBOL=BTC/USD

Cycle tuning:
- CYCLE_SECONDS=60
- CYCLE_TIMEFRAMES=5s,30s,1m,5m,15m
