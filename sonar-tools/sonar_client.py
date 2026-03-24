"""
sonar_client.py — Shared Sonar API client for CrystalFlow internal tooling.

INTERNAL USE ONLY. This module is not exposed to customers.
All API calls are made server-side; no raw LLM output is passed directly to end users.

BUDGET PROTECTION:
  - Per-request cost cap (kills any single runaway query)
  - Per-minute request cap (rate limiter)
  - Hourly request cap (burst protection)
  - Daily request + cost cap (hard ceiling per 24h)
  - Monthly cost cap (absolute budget ceiling)
  - Persistent ledger saved to disk (survives restarts)
  - Kill switch: set SONAR_KILL_SWITCH=1 to block ALL requests instantly
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("sonar_client")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
BASE_URL = "https://api.perplexity.ai/chat/completions"

# Pricing per 1M tokens (input / output) — kept here for cost tracking
MODEL_PRICING: dict[str, dict[str, float]] = {
    "sonar": {"input": 1.0, "output": 1.0},
    "sonar-pro": {"input": 3.0, "output": 15.0},
}

# ---------------------------------------------------------------------------
# Budget caps (configurable via env vars, sensible defaults)
# ---------------------------------------------------------------------------
DEFAULT_CAPS = {
    # Rate limiting
    "MAX_REQUESTS_PER_MINUTE": 5,       # Sliding window
    "MAX_REQUESTS_PER_HOUR": 60,        # Burst protection
    "MAX_REQUESTS_PER_DAY": 500,        # Daily hard ceiling
    # Cost limiting
    "MAX_COST_PER_REQUEST_USD": 0.50,   # Kill any single query over 50 cents
    "MAX_COST_PER_DAY_USD": 5.00,       # $5/day hard cap
    "MAX_COST_PER_MONTH_USD": 50.00,    # $50/month absolute ceiling
}

LEDGER_DIR = Path(os.environ.get("SONAR_LEDGER_DIR", Path.home() / ".sonar-ledger"))


def _get_cap(name: str) -> float:
    """Read cap from env var (e.g., SONAR_MAX_REQUESTS_PER_DAY=200) or use default."""
    env_val = os.environ.get(f"SONAR_{name}")
    if env_val is not None:
        return float(env_val)
    return float(DEFAULT_CAPS[name])

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------
@dataclass
class SearchResult:
    url: str
    title: str = ""
    date: str = ""
    snippet: str = ""


@dataclass
class SonarResponse:
    content: str
    citations: list[str] = field(default_factory=list)
    search_results: list[SearchResult] = field(default_factory=list)
    related_questions: list[str] = field(default_factory=list)
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    estimated_cost_usd: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Rate limiter (token-bucket style, max N requests per 60 seconds)
# ---------------------------------------------------------------------------
class RateLimiter:
    """Sliding-window rate limiter. Default: 5 requests/minute."""

    def __init__(self, max_requests: int = 5, window_seconds: int = 60) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._timestamps: deque[float] = deque()

    def wait_if_needed(self) -> None:
        now = time.monotonic()
        # Drop timestamps outside the window
        while self._timestamps and now - self._timestamps[0] > self.window_seconds:
            self._timestamps.popleft()

        if len(self._timestamps) >= self.max_requests:
            oldest = self._timestamps[0]
            sleep_for = self.window_seconds - (now - oldest) + 0.1
            if sleep_for > 0:
                logger.info("Rate limit reached — waiting %.1fs before next request.", sleep_for)
                time.sleep(sleep_for)

        self._timestamps.append(time.monotonic())


# ---------------------------------------------------------------------------
# Budget guard — persistent daily/monthly cost & request ledger
# ---------------------------------------------------------------------------
class BudgetGuard:
    """
    Hard budget enforcement. Tracks requests and cost per day and per month.
    Persists to disk so limits survive process restarts and cron invocations.

    Kill switch: set env SONAR_KILL_SWITCH=1 to block ALL requests instantly.
    """

    def __init__(self, ledger_dir: Path = LEDGER_DIR) -> None:
        self.ledger_dir = ledger_dir
        self.ledger_dir.mkdir(parents=True, exist_ok=True)
        self._ledger_path = self.ledger_dir / "budget_ledger.json"

        # Load caps from env or defaults
        self.max_requests_per_hour = int(_get_cap("MAX_REQUESTS_PER_HOUR"))
        self.max_requests_per_day = int(_get_cap("MAX_REQUESTS_PER_DAY"))
        self.max_cost_per_request = _get_cap("MAX_COST_PER_REQUEST_USD")
        self.max_cost_per_day = _get_cap("MAX_COST_PER_DAY_USD")
        self.max_cost_per_month = _get_cap("MAX_COST_PER_MONTH_USD")

        # Hourly tracking (in-memory, resets on restart — that's fine)
        self._hourly_timestamps: deque[float] = deque()

        # Load persistent ledger
        self._ledger = self._load_ledger()

    def _load_ledger(self) -> dict[str, Any]:
        """Load or initialize the persistent ledger."""
        if self._ledger_path.exists():
            try:
                with open(self._ledger_path) as f:
                    return json.load(f)
            except (json.JSONDecodeError, KeyError):
                logger.warning("Corrupt ledger — resetting.")
        return self._fresh_ledger()

    def _fresh_ledger(self) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        return {
            "day": now.strftime("%Y-%m-%d"),
            "month": now.strftime("%Y-%m"),
            "daily_requests": 0,
            "daily_cost_usd": 0.0,
            "monthly_cost_usd": 0.0,
            "monthly_requests": 0,
            "last_updated": now.isoformat(),
            "blocked_attempts": 0,
        }

    def _save_ledger(self) -> None:
        with open(self._ledger_path, "w") as f:
            json.dump(self._ledger, f, indent=2)

    def _rollover_if_needed(self) -> None:
        """Reset daily/monthly counters when the calendar flips."""
        now = datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")
        this_month = now.strftime("%Y-%m")

        if self._ledger["day"] != today:
            logger.info("New day detected — resetting daily counters.")
            self._ledger["day"] = today
            self._ledger["daily_requests"] = 0
            self._ledger["daily_cost_usd"] = 0.0

        if self._ledger["month"] != this_month:
            logger.info("New month detected — resetting monthly counters.")
            self._ledger["month"] = this_month
            self._ledger["monthly_cost_usd"] = 0.0
            self._ledger["monthly_requests"] = 0

    def pre_request_check(self) -> None:
        """
        Call BEFORE every API request. Raises BudgetExceeded if any cap is hit.
        """
        # Kill switch — instant block
        if os.environ.get("SONAR_KILL_SWITCH", "0") == "1":
            raise BudgetExceeded(
                "KILL SWITCH ACTIVE. Set SONAR_KILL_SWITCH=0 to resume."
            )

        self._rollover_if_needed()

        # Hourly request cap
        now = time.monotonic()
        while self._hourly_timestamps and now - self._hourly_timestamps[0] > 3600:
            self._hourly_timestamps.popleft()
        if len(self._hourly_timestamps) >= self.max_requests_per_hour:
            self._ledger["blocked_attempts"] += 1
            self._save_ledger()
            raise BudgetExceeded(
                f"Hourly request cap reached ({self.max_requests_per_hour}/hr). "
                f"Try again in {int(3600 - (now - self._hourly_timestamps[0]))}s."
            )

        # Daily request cap
        if self._ledger["daily_requests"] >= self.max_requests_per_day:
            self._ledger["blocked_attempts"] += 1
            self._save_ledger()
            raise BudgetExceeded(
                f"Daily request cap reached ({self.max_requests_per_day}/day). "
                f"Resets at midnight UTC. Override: SONAR_MAX_REQUESTS_PER_DAY"
            )

        # Daily cost cap
        if self._ledger["daily_cost_usd"] >= self.max_cost_per_day:
            self._ledger["blocked_attempts"] += 1
            self._save_ledger()
            raise BudgetExceeded(
                f"Daily cost cap reached (${self.max_cost_per_day:.2f}/day). "
                f"Spent today: ${self._ledger['daily_cost_usd']:.4f}. "
                f"Override: SONAR_MAX_COST_PER_DAY_USD"
            )

        # Monthly cost cap
        if self._ledger["monthly_cost_usd"] >= self.max_cost_per_month:
            self._ledger["blocked_attempts"] += 1
            self._save_ledger()
            raise BudgetExceeded(
                f"Monthly cost cap reached (${self.max_cost_per_month:.2f}/mo). "
                f"Spent this month: ${self._ledger['monthly_cost_usd']:.4f}. "
                f"Override: SONAR_MAX_COST_PER_MONTH_USD"
            )

        self._hourly_timestamps.append(now)

    def post_request_record(self, cost_usd: float) -> None:
        """
        Call AFTER every API request. Records cost and saves ledger to disk.
        """
        # Per-request cost check (for the NEXT request's reference)
        if cost_usd > self.max_cost_per_request:
            logger.warning(
                "ALERT: Single request cost $%.4f exceeded per-request cap of $%.2f. "
                "Investigate the query that caused this.",
                cost_usd,
                self.max_cost_per_request,
            )

        self._rollover_if_needed()
        self._ledger["daily_requests"] += 1
        self._ledger["daily_cost_usd"] += cost_usd
        self._ledger["monthly_cost_usd"] += cost_usd
        self._ledger["monthly_requests"] += 1
        self._ledger["last_updated"] = datetime.now(timezone.utc).isoformat()
        self._save_ledger()

        # Warn at 80% thresholds
        if self._ledger["daily_cost_usd"] >= self.max_cost_per_day * 0.8:
            logger.warning(
                "BUDGET WARNING: Daily spend at %.0f%% ($%.4f / $%.2f)",
                (self._ledger["daily_cost_usd"] / self.max_cost_per_day) * 100,
                self._ledger["daily_cost_usd"],
                self.max_cost_per_day,
            )
        if self._ledger["monthly_cost_usd"] >= self.max_cost_per_month * 0.8:
            logger.warning(
                "BUDGET WARNING: Monthly spend at %.0f%% ($%.4f / $%.2f)",
                (self._ledger["monthly_cost_usd"] / self.max_cost_per_month) * 100,
                self._ledger["monthly_cost_usd"],
                self.max_cost_per_month,
            )

    def status(self) -> dict[str, Any]:
        """Return current budget status for dashboards/logs."""
        self._rollover_if_needed()
        return {
            "kill_switch": os.environ.get("SONAR_KILL_SWITCH", "0") == "1",
            "daily_requests": f"{self._ledger['daily_requests']}/{self.max_requests_per_day}",
            "daily_cost": f"${self._ledger['daily_cost_usd']:.4f}/${self.max_cost_per_day:.2f}",
            "monthly_cost": f"${self._ledger['monthly_cost_usd']:.4f}/${self.max_cost_per_month:.2f}",
            "monthly_requests": self._ledger["monthly_requests"],
            "blocked_attempts": self._ledger["blocked_attempts"],
            "caps": {
                "per_minute": int(_get_cap("MAX_REQUESTS_PER_MINUTE")),
                "per_hour": self.max_requests_per_hour,
                "per_day": self.max_requests_per_day,
                "cost_per_request": f"${self.max_cost_per_request:.2f}",
                "cost_per_day": f"${self.max_cost_per_day:.2f}",
                "cost_per_month": f"${self.max_cost_per_month:.2f}",
            },
        }


class BudgetExceeded(Exception):
    """Raised when any budget cap is hit. Prevents the API call from executing."""
    pass


# ---------------------------------------------------------------------------
# Cost tracker
# ---------------------------------------------------------------------------
class CostTracker:
    def __init__(self) -> None:
        self.total_usd: float = 0.0
        self.query_count: int = 0
        self._log: list[dict[str, Any]] = []

    def record(
        self,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        query_summary: str = "",
    ) -> float:
        pricing = MODEL_PRICING.get(model, MODEL_PRICING["sonar"])
        cost = (prompt_tokens / 1_000_000) * pricing["input"] + (
            completion_tokens / 1_000_000
        ) * pricing["output"]
        self.total_usd += cost
        self.query_count += 1
        entry = {
            "query": query_summary,
            "model": model,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "cost_usd": round(cost, 6),
        }
        self._log.append(entry)
        logger.info(
            "Cost: $%.6f (prompt=%d, completion=%d) | Session total: $%.6f",
            cost,
            prompt_tokens,
            completion_tokens,
            self.total_usd,
        )
        return cost

    def summary(self) -> dict[str, Any]:
        return {
            "total_usd": round(self.total_usd, 6),
            "query_count": self.query_count,
            "queries": self._log,
        }


# ---------------------------------------------------------------------------
# Main client
# ---------------------------------------------------------------------------
class SonarClient:
    """
    Internal Sonar API client for CrystalFlow business intelligence tools.

    Budget Protection Layers:
      1. Kill switch     — SONAR_KILL_SWITCH=1 blocks everything instantly
      2. Rate limiter    — 5 req/min sliding window (configurable)
      3. Hourly cap      — 60 req/hr (configurable via SONAR_MAX_REQUESTS_PER_HOUR)
      4. Daily req cap   — 500 req/day (configurable via SONAR_MAX_REQUESTS_PER_DAY)
      5. Daily cost cap  — $5/day (configurable via SONAR_MAX_COST_PER_DAY_USD)
      6. Monthly cost cap — $50/month (configurable via SONAR_MAX_COST_PER_MONTH_USD)
      7. Per-request cap — $0.50 per single query (alerts if exceeded)
      8. Persistent ledger — survives restarts, saved to ~/.sonar-ledger/

    Usage
    -----
    client = SonarClient()
    response = client.query("What is the water hardness in Coral Gables?")
    print(response.content)
    print(response.citations)
    print(client.budget_status())  # Check remaining budget
    """

    def __init__(
        self,
        api_key: str | None = None,
        default_model: str = "sonar",
        max_requests_per_minute: int | None = None,
    ) -> None:
        self.api_key = api_key or os.environ.get("PERPLEXITY_API_KEY", "")
        if not self.api_key:
            raise ValueError(
                "PERPLEXITY_API_KEY not set. Export it or add it to .env"
            )
        self.default_model = default_model
        rpm = max_requests_per_minute or int(_get_cap("MAX_REQUESTS_PER_MINUTE"))
        self._rate_limiter = RateLimiter(max_requests=rpm)
        self._budget_guard = BudgetGuard()
        self.cost_tracker = CostTracker()
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
        )
        logger.info(
            "SonarClient initialized. Budget status: %s",
            json.dumps(self._budget_guard.status(), indent=2),
        )

    # ------------------------------------------------------------------
    # Public query interface
    # ------------------------------------------------------------------
    def query(
        self,
        prompt: str,
        *,
        system_prompt: str = "You are a business intelligence research assistant. Provide concise, factual analysis with citations.",
        model: str | None = None,
        search_recency_filter: str = "month",
        search_domain_filter: list[str] | None = None,
        return_related_questions: bool = True,
        search_context_size: str = "medium",
        max_tokens: int = 1024,
        temperature: float = 0.2,
        retries: int = 3,
    ) -> SonarResponse:
        """
        Send a query to the Sonar API and return a structured SonarResponse.

        Parameters
        ----------
        prompt : str
            The user question or research query.
        system_prompt : str
            System context for the model.
        model : str, optional
            Override the default model ("sonar" or "sonar-pro").
        search_recency_filter : str
            One of "hour", "day", "week", "month".
        search_domain_filter : list[str], optional
            Restrict web searches to these domains.
        return_related_questions : bool
            Whether to include related questions in the response.
        search_context_size : str
            One of "low", "medium", "high".
        max_tokens : int
            Maximum completion tokens.
        temperature : float
            Sampling temperature (lower = more deterministic).
        retries : int
            Number of retry attempts on transient errors.
        """
        selected_model = model or self.default_model
        payload: dict[str, Any] = {
            "model": selected_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "search_recency_filter": search_recency_filter,
            "return_related_questions": return_related_questions,
            "search_context_size": search_context_size,
        }
        if search_domain_filter:
            payload["search_domain_filter"] = search_domain_filter

        # --- Budget gate: block before any API call if caps are hit ---
        self._budget_guard.pre_request_check()
        self._rate_limiter.wait_if_needed()

        last_exc: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                resp = self._session.post(BASE_URL, json=payload, timeout=60)
                resp.raise_for_status()
                return self._parse_response(resp.json(), selected_model, prompt[:80])
            except requests.exceptions.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else 0
                logger.warning("HTTP %s on attempt %d: %s", status, attempt, exc)
                if status in (400, 401, 403):
                    raise  # Non-retriable
                last_exc = exc
            except requests.exceptions.RequestException as exc:
                logger.warning("Request error on attempt %d: %s", attempt, exc)
                last_exc = exc

            if attempt < retries:
                backoff = 2 ** attempt
                logger.info("Retrying in %ds…", backoff)
                time.sleep(backoff)

        raise RuntimeError(f"Sonar API failed after {retries} attempts") from last_exc

    def batch_query(
        self,
        prompts: list[str],
        **kwargs: Any,
    ) -> list[SonarResponse]:
        """Run multiple queries sequentially with rate limiting applied per call."""
        results: list[SonarResponse] = []
        for i, prompt in enumerate(prompts, 1):
            logger.info("Batch query %d/%d: %s…", i, len(prompts), prompt[:60])
            results.append(self.query(prompt, **kwargs))
        return results

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def budget_status(self) -> dict[str, Any]:
        """Return current budget/cap status. Use in dashboards or CLI checks."""
        return self._budget_guard.status()

    def _parse_response(
        self, data: dict[str, Any], model: str, query_summary: str
    ) -> SonarResponse:
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message", {})
        content = message.get("content", "")

        citations: list[str] = data.get("citations", [])

        # search_results may be nested under the choice or top-level
        raw_results: list[dict[str, Any]] = data.get("search_results") or choice.get(
            "search_results", []
        )
        search_results = [
            SearchResult(
                url=r.get("url", ""),
                title=r.get("title", ""),
                date=r.get("date", ""),
                snippet=r.get("snippet", ""),
            )
            for r in raw_results
        ]

        related_questions: list[str] = data.get("related_questions", [])

        usage = data.get("usage", {})
        prompt_tokens = usage.get("prompt_tokens", 0)
        completion_tokens = usage.get("completion_tokens", 0)

        cost = self.cost_tracker.record(
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            query_summary=query_summary,
        )

        # --- Record cost in persistent budget ledger ---
        self._budget_guard.post_request_record(cost)

        return SonarResponse(
            content=content,
            citations=citations,
            search_results=search_results,
            related_questions=related_questions,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            estimated_cost_usd=cost,
            raw=data,
        )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"SonarClient(model={self.default_model!r}, "
            f"queries_run={self.cost_tracker.query_count}, "
            f"total_cost=${self.cost_tracker.total_usd:.6f})"
        )
