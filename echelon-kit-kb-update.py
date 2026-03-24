"""
echelon-kit-kb-update.py — Upload Echelon Kit product knowledge to ElevenLabs KB.

This creates a knowledge base document with Echelon Kit product info, guardrails,
and sales enablement content, then attaches it to the CrystalFlow agent.

The ElevenLabs chatbot can then reference Echelon Kit when relevant conversations
come up (e.g., customers asking about business consulting, startup help, etc.).

Usage:
    python echelon-kit-kb-update.py                    # Upload with confirmation
    python echelon-kit-kb-update.py --preview          # Preview only
    python echelon-kit-kb-update.py --auto             # Skip confirmation
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("echelon_kb_update")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
ELEVENLABS_API_BASE = "https://api.elevenlabs.io/v1"
AGENT_ID = "agent_1201km4bnftaf8m8r341bsqx36sj"
PRIMARY_KB_DOC_ID = "NuBCwt6ISP8uX7mBGYVj"  # NEVER delete

STATE_DIR = Path(os.environ.get("SONAR_LEDGER_DIR", Path.home() / ".sonar-ledger"))
STATE_FILE = STATE_DIR / "echelon_kit_kb_doc.json"


# ---------------------------------------------------------------------------
# Echelon Kit Knowledge Document
# ---------------------------------------------------------------------------
def build_echelon_kb_document() -> str:
    """Build the Echelon Kit knowledge base document for the ElevenLabs agent."""
    now = datetime.now(timezone.utc).strftime("%B %d, %Y")
    
    return f"""Echelon Kit — Business Launch Intelligence Platform
{"=" * 60}
Last updated: {now}

PRODUCT OVERVIEW
{"-" * 40}
Echelon Kit is a premium business launch toolkit created by Isaiah Pacheco,
the founder of CrystalFlow Miami. It provides aspiring entrepreneurs with
AI-powered market intelligence, competitor analysis, and step-by-step
launch frameworks to start and scale a service business.

Website: echelonkit.com
Purchase: pacheco404.gumroad.com/l/echelon-kit

PRICING TIERS
{"-" * 40}
1. Blueprint — $250
   - Digital-only business launch blueprint
   - Market analysis framework templates
   - Competitor research methodology
   - Step-by-step launch checklist
   - Best for: Self-starters who want a proven framework

2. Starter Kit — $700
   - Everything in Blueprint PLUS:
   - AI-powered market analysis for your specific business idea
   - Personalized competitor intelligence report
   - Customer pain point identification
   - Recommended technology stack with cost estimates
   - Startup cost breakdown (low/mid/high scenarios)
   - Best for: Entrepreneurs ready to validate their idea with real data

3. Done-For-You — $4,000
   - Everything in Starter Kit PLUS:
   - Complete business launch package
   - Custom branding and identity
   - Website design and deployment
   - CRM setup and configuration
   - Marketing automation setup
   - 30-day post-launch support
   - Best for: Entrepreneurs who want a turnkey solution

WHAT MAKES ECHELON KIT DIFFERENT
{"-" * 40}
- Built by a real business owner who launched CrystalFlow Miami from scratch
- Uses the same AI intelligence tools that power CrystalFlow's operations
- Real-time market data, not generic templates
- Covers the complete journey: idea validation → launch → first customers
- Practical, no-fluff approach based on real experience

THE "TRY IT HERE" DEMO
{"-" * 40}
The Echelon Kit website features a free demo where visitors can enter any
business idea and receive a preview of the type of analysis included in the
Starter Kit and Done-For-You tiers. The demo provides:
- Market size and growth trends
- Top competitors in the space
- Common customer pain points
- Recommended tech stack
- Estimated startup costs

This demo is powered by AI market intelligence and gives a taste of the
full analysis. The complete Starter Kit and Done-For-You packages go much
deeper with personalized recommendations and actionable next steps.

CONVERSATION GUIDELINES
{"-" * 40}
When discussing Echelon Kit with customers:
- Position it as a premium product built from real entrepreneurial experience
- Emphasize that Isaiah used these exact methods to build CrystalFlow Miami
- The $700 Starter Kit is the best value for someone serious about launching
- The $4,000 Done-For-You is for people who want everything handled
- Direct interested customers to echelonkit.com
- Mention the free "Try It Here" demo as a low-commitment way to see the value
- Never pressure — educate and let the product sell itself

GUARDRAILS
{"-" * 40}
- Never share internal pricing strategies or cost structures
- Never make income guarantees or promises about business success
- Never disparage competitors by name
- Never share Isaiah's personal information beyond his role as founder
- Keep all discussions professional and business-focused
- If someone asks about topics unrelated to business or water treatment,
  politely redirect to relevant services
"""


# ---------------------------------------------------------------------------
# ElevenLabs API
# ---------------------------------------------------------------------------
def _get_api_key() -> str:
    key = os.environ.get("ELEVENLABS_API_KEY", "")
    if not key:
        raise ValueError("ELEVENLABS_API_KEY not set.")
    return key


def _headers() -> dict[str, str]:
    return {"xi-api-key": _get_api_key()}


def get_agent_config() -> dict:
    resp = requests.get(
        f"{ELEVENLABS_API_BASE}/convai/agents/{AGENT_ID}",
        headers=_headers(),
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def get_current_kb_ids(agent_config: dict) -> list[str]:
    try:
        kb_array = (
            agent_config.get("conversation_config", {})
            .get("agent", {})
            .get("prompt", {})
            .get("knowledge_base", [])
        )
        ids = []
        for item in kb_array:
            if isinstance(item, dict):
                ids.append(item.get("id", item.get("file_id", "")))
            elif isinstance(item, str):
                ids.append(item)
        return [i for i in ids if i]
    except Exception as exc:
        logger.error("Failed to extract KB IDs: %s", exc)
        return []


def upload_document(file_path: Path, doc_name: str) -> str:
    with open(file_path, "rb") as f:
        resp = requests.post(
            f"{ELEVENLABS_API_BASE}/convai/knowledge-base/file",
            headers=_headers(),
            files={"file": (file_path.name, f, "text/plain")},
            data={"name": doc_name},
            timeout=60,
        )
    resp.raise_for_status()
    data = resp.json()
    doc_id = data.get("id", "")
    logger.info("Uploaded: %s (ID: %s)", doc_name, doc_id)
    return doc_id


def patch_agent_kb(kb_ids: list[str]) -> None:
    if PRIMARY_KB_DOC_ID not in kb_ids:
        kb_ids.insert(0, PRIMARY_KB_DOC_ID)

    payload = {
        "conversation_config": {
            "agent": {
                "prompt": {
                    "knowledge_base": [
                        {"type": "file", "id": kid, "name": ""} for kid in kb_ids
                    ]
                }
            }
        }
    }
    resp = requests.patch(
        f"{ELEVENLABS_API_BASE}/convai/agents/{AGENT_ID}",
        headers={**_headers(), "Content-Type": "application/json"},
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    logger.info("Agent KB patched with %d documents.", len(kb_ids))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main(preview: bool = False, auto: bool = False) -> dict:
    content = build_echelon_kb_document()
    doc_name = f"Echelon Kit Product Knowledge {datetime.now(timezone.utc).strftime('%Y-%m-%d')}"

    print(f"\n{'='*60}")
    print(f"  Echelon Kit KB Document Preview")
    print(f"  Name: {doc_name}")
    print(f"  Length: {len(content)} characters")
    print(f"{'='*60}\n")

    if preview:
        print(content)
        return {"success": True, "action": "preview"}

    if not auto:
        print(content[:800])
        print(f"\n... ({len(content) - 800} more characters)\n")
        confirm = input("Upload to ElevenLabs? [y/N] ").strip().lower()
        if confirm not in ("y", "yes"):
            print("Aborted.")
            return {"success": False, "action": "aborted"}

    # Write temp file
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_DIR / "echelon_kit_kb.txt"
    tmp.write_text(content, encoding="utf-8")

    # Load state
    state = {}
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text())
        except Exception:
            state = {}

    # Delete old echelon doc if exists
    old_id = state.get("current_doc_id")
    if old_id and old_id != PRIMARY_KB_DOC_ID:
        try:
            resp = requests.delete(
                f"{ELEVENLABS_API_BASE}/convai/knowledge-base/{old_id}",
                headers=_headers(),
                timeout=30,
            )
            if resp.status_code != 404:
                resp.raise_for_status()
            logger.info("Deleted old echelon doc: %s", old_id)
        except Exception as exc:
            logger.warning("Failed to delete old doc %s: %s", old_id, exc)

    # Upload new
    new_id = upload_document(tmp, doc_name)

    # Patch agent
    agent_config = get_agent_config()
    kb_ids = get_current_kb_ids(agent_config)
    if old_id and old_id in kb_ids:
        kb_ids.remove(old_id)
    if new_id not in kb_ids:
        kb_ids.append(new_id)
    patch_agent_kb(kb_ids)

    # Save state
    state["current_doc_id"] = new_id
    state["doc_name"] = doc_name
    state["uploaded_at"] = datetime.now(timezone.utc).isoformat()
    STATE_FILE.write_text(json.dumps(state, indent=2))

    tmp.unlink(missing_ok=True)

    print(f"\n✓ Echelon Kit knowledge uploaded to ElevenLabs.")
    print(f"  Doc ID: {new_id}")
    print(f"  Agent: {AGENT_ID}")
    print(f"  Total KB docs: {len(kb_ids)}")

    return {"success": True, "doc_id": new_id, "total_kb_docs": len(kb_ids)}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--preview", action="store_true")
    parser.add_argument("--auto", action="store_true")
    args = parser.parse_args()
    result = main(preview=args.preview, auto=args.auto)
    if not result.get("success"):
        sys.exit(1)
