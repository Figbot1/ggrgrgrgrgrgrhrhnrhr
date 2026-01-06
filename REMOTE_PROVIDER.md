Remote Model Provider Contract

Configure:
- ENABLE_REMOTE=1
- REMOTE_MODEL_URL=https://your-endpoint
- REMOTE_MODEL_KEY=your-token (optional)
- REMOTE_MODEL_MODELS=model-a,model-b
- REMOTE_MODEL_TIMEOUT=45

Request (POST JSON):
{
  "model": "model-a",
  "prompt": "full prompt text",
  "temperature": 0.7,
  "timeout": 45
}

Response (one of):
{
  "text": "model response"
}
or
{
  "response": "model response"
}
or
{
  "message": "model response"
}
or OpenAI-style:
{
  "choices": [
    {"message": {"content": "model response"}}
  ]
}

Notes:
- If you return a different shape, update `remote_chat()` in `clean_deploy/democratic_trader_hybrid.py`.
