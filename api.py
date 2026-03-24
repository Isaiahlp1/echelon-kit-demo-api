"""
Echelon Kit Demo API — FastAPI server for the Try-It-Here feature.

Endpoints:
  POST /api/demo   — Analyze a business idea (rate-limited: 3/hr per IP)
  GET  /health     — Health check
"""

import sys
import time
import logging
from collections import defaultdict
from typing import Any

from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

# Import the Echelon analysis engine
# The source file uses hyphens (echelon-demo.py), so we use importlib
import importlib.util
from pathlib import Path

# Resolve the path: works locally (/home/user/workspace/sonar-tools)
# and in Docker (/app/sonar-tools)
_search_paths = [
    Path(__file__).parent / "sonar-tools" / "echelon-demo.py",
    Path("/home/user/workspace/sonar-tools/echelon-demo.py"),
    Path("/app/sonar-tools/echelon-demo.py"),
]
_demo_path = None
for _p in _search_paths:
    if _p.exists():
        _demo_path = _p
        break

if _demo_path is None:
    raise ImportError(
        f"Cannot find echelon-demo.py. Searched: {[str(p) for p in _search_paths]}"
    )

# Add the parent directory to sys.path so sonar_client can be imported
sys.path.insert(0, str(_demo_path.parent))

_spec = importlib.util.spec_from_file_location("echelon_demo", str(_demo_path))
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

generate_demo_analysis = _mod.generate_demo_analysis
to_api_response = _mod.to_api_response
InputValidationError = _mod.InputValidationError

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("echelon_api")

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Echelon Kit Demo API",
    description="Backend for the Echelon Kit Try-It-Here demo feature.",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://echelonkit.com",
        "https://www.echelonkit.com",
        "http://localhost:3000",
        "http://localhost:5500",
        "http://localhost:8000",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:5500",
        "http://127.0.0.1:8000",
        # Allow any S3 preview URLs during development
        "https://*.s3.amazonaws.com",
    ],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type"],
    allow_origin_regex=r"https://.*\.s3\.amazonaws\.com",
)

# ---------------------------------------------------------------------------
# Rate Limiting (in-memory, 3 requests per IP per hour)
# ---------------------------------------------------------------------------
RATE_LIMIT = 3
RATE_WINDOW = 3600  # seconds (1 hour)

# Dict of IP -> list of timestamps
_rate_store: dict[str, list[float]] = defaultdict(list)


def _check_rate_limit(ip: str) -> bool:
    """Return True if the request is allowed, False if rate-limited."""
    now = time.time()
    cutoff = now - RATE_WINDOW

    # Prune old entries
    _rate_store[ip] = [t for t in _rate_store[ip] if t > cutoff]

    if len(_rate_store[ip]) >= RATE_LIMIT:
        return False

    _rate_store[ip].append(now)
    return True


def _get_client_ip(request: Request) -> str:
    """Extract client IP, respecting X-Forwarded-For for proxied deployments."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------
class DemoRequest(BaseModel):
    business_idea: str = Field(
        ...,
        min_length=1,
        max_length=150,
        description="The business idea to analyze (e.g., 'mobile dog grooming').",
        json_schema_extra={"example": "mobile dog grooming"},
    )


class HealthResponse(BaseModel):
    status: str = "ok"
    service: str = "echelon-kit-demo-api"
    version: str = "1.0.0"


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.get("/health", response_model=HealthResponse)
async def health_check():
    """Health check endpoint for monitoring and Render deploy checks."""
    return HealthResponse()


@app.post("/api/demo")
async def demo_analysis(body: DemoRequest, request: Request) -> dict[str, Any]:
    """
    Analyze a business idea and return a preview of what Echelon Kit delivers.

    Rate-limited to 3 requests per IP per hour.
    """
    client_ip = _get_client_ip(request)

    # Rate limit check
    if not _check_rate_limit(client_ip):
        logger.warning("Rate limit exceeded for IP: %s", client_ip)
        raise HTTPException(
            status_code=429,
            detail={
                "error": "Rate limit exceeded. You can analyze up to 3 business ideas per hour.",
                "retry_after_seconds": RATE_WINDOW,
            },
        )

    logger.info("Demo request from %s: %r", client_ip, body.business_idea)

    try:
        # Run the analysis engine
        raw_result = generate_demo_analysis(body.business_idea)

        # Strip internal metadata before sending to frontend
        api_result = to_api_response(raw_result)

        if not api_result.get("success"):
            raise HTTPException(
                status_code=400,
                detail={"error": api_result.get("error", "Analysis failed.")},
            )

        return api_result

    except InputValidationError as exc:
        raise HTTPException(status_code=400, detail={"error": str(exc)})

    except HTTPException:
        raise  # Re-raise HTTP exceptions as-is

    except Exception as exc:
        logger.exception("Unexpected error during demo analysis")
        raise HTTPException(
            status_code=500,
            detail={"error": "Something went wrong. Please try again in a moment."},
        )


# ---------------------------------------------------------------------------
# Error handlers
# ---------------------------------------------------------------------------
@app.exception_handler(422)
async def validation_error_handler(request: Request, exc):
    return JSONResponse(
        status_code=422,
        content={
            "success": False,
            "error": "Invalid request. Please provide a valid business idea.",
        },
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
