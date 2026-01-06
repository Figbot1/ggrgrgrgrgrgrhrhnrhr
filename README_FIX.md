# Figbot Fix Pack (Functional Trading + Live UI)

This pack modifies the bot so it **still produces predictions and trades** even when no local LLM (Ollama) is available on Railway.

## What changed
- Crypto pricing now uses Alpaca **latest orderbook mid** (instead of CoinGecko), so `BTC/USD` price resolves on Railway.
- If no AI-model predictions are stored, Figbot generates **baseline momentum** picks from Alpaca bars and stores them so:
  - the dashboard fills with Predictions + Calls,
  - `choose_trade()` can act,
  - evaluation/learning tables update.
- Default timeframes no longer include second-based bars (Alpaca REST bars don’t support seconds).

## Deploy
Replace your repo contents with this zip (or copy the modified files) and redeploy on Railway.
Make sure your Railway variables include:
- `APCA_API_KEY_ID`
- `APCA_API_SECRET_KEY`
- `APCA_API_BASE_URL` (paper example: `https://paper-api.alpaca.markets`)
- `APCA_DATA_BASE_URL` (default: `https://data.alpaca.markets`)
