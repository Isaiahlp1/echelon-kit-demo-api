# Echelon Kit Demo API

FastAPI backend powering the **Try It Here** feature on [echelonkit.com](https://echelonkit.com).

Visitors enter a business idea → the backend queries Perplexity Sonar for real-time market analysis → returns structured data to the frontend.

## Architecture

```
echelonkit.com (Netlify)  →  fetch("/api/demo")  →  This API (Render)  →  Perplexity Sonar
```

The frontend is a static site on Netlify. This backend runs as a separate web service on Render. CORS is locked to `echelonkit.com`.

## Security

- **LLM Guardrails**: 3-layer protection (input sanitization, system prompt guardrails, output sanitization)
- **Rate Limiting**: 3 requests per IP per hour
- **No /docs or /redoc**: API documentation endpoints disabled in production
- **CORS Locked**: Only `echelonkit.com` origins accepted
- **Sonar API Key**: Server-side only, never exposed to browser

## Deployment (Render)

1. Connect this repo to [Render](https://render.com)
2. It auto-detects `render.yaml` and creates a Docker web service
3. Add environment variable: `PERPLEXITY_API_KEY`
4. Deploy

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `PERPLEXITY_API_KEY` | Yes | Perplexity Sonar API key |
| `PORT` | No | Server port (default: 8000, Render sets this) |

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/demo` | Analyze a business idea (rate-limited) |
| GET | `/health` | Health check |

## Local Development

```bash
pip install -r requirements.txt
PERPLEXITY_API_KEY=your-key python api.py
```

## Files

- `api.py` — FastAPI server with CORS, rate limiting, error handling
- `Dockerfile` — Production container
- `render.yaml` — Render Blueprint for one-click deploy
- `sonar-tools/echelon-demo.py` — Analysis engine with LLM guardrails
- `sonar-tools/sonar_client.py` — Sonar API client with budget protection
