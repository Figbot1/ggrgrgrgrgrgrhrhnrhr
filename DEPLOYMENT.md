Deployment Steps

1) Commit your changes locally.
2) Push to GitHub (PAT or SSH). If you hit auth errors, use a GitHub PAT as the password.
3) Redeploy on Railway.

Railway start command:
web: gunicorn clean_deploy.hybrid_app:app --bind 0.0.0.0:$PORT --workers 1 --timeout 120

Verify deployment:
- GET /api/version (shows build stamp)
- GET /health

Common env vars:
- APCA_API_KEY_ID
- APCA_API_SECRET_KEY
- APCA_API_BASE_URL=https://paper-api.alpaca.markets
- FALLBACK_BTC_TRADE=1
- FALLBACK_BTC_NOTIONAL=10
- FALLBACK_BTC_ASSET=BTC
- FALLBACK_BTC_ORDER_SYMBOL=BTC/USD
