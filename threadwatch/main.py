"""
ThreadWatch — Governed Agents hackathon project.

A FastAPI service that ingests signals from the other four governance tools
and computes an overall pipeline health score from 0.0 to 1.0.

Each tool POSTs a signal after it does its job. ThreadWatch looks at the
last 20 signals per tool and scores a "good outcome" ratio:

  iron-thread  -> ratio of signals where payload.status == "passed"
  agentid      -> ratio of signals where payload.recommendation == "ALLOW"
  chainthread  -> ratio of signals where payload.status == "delivered"
  policythread -> ratio of signals where payload.passed == true

If a tool has zero signals, it scores 1.0 (no data = not degraded).
health_score = average of the four tool scores.

Run locally:
    python -m uvicorn main:app --reload

Deploy: Render (see render.yaml). GitHub push triggers auto-deploy.
"""

import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

load_dotenv()

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

if not SUPABASE_URL or not SUPABASE_KEY:
    print("WARNING: SUPABASE_URL / SUPABASE_KEY are not set. "
          "Set them in a .env file (local) or in Render env vars (deployed).")

SUPABASE_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
}

VALID_TOOLS = ["iron-thread", "agentid", "chainthread", "policythread"]

app = FastAPI(title="ThreadWatch", version="1.0.0")

# CORS must be added before any routes are registered.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Supabase REST helpers (httpx only — never supabase-py)
# ---------------------------------------------------------------------------

def _sb_url(table: str) -> str:
    return f"{SUPABASE_URL}/rest/v1/{table}"


def sb_select(table: str, params: Dict[str, str]) -> List[Dict[str, Any]]:
    with httpx.Client(timeout=15) as client:
        resp = client.get(_sb_url(table), headers=SUPABASE_HEADERS, params=params)
    if resp.status_code >= 300:
        raise HTTPException(status_code=502, detail=f"Supabase error ({table}): {resp.text}")
    return resp.json()


def sb_insert(table: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    headers = {**SUPABASE_HEADERS, "Prefer": "return=representation"}
    with httpx.Client(timeout=15) as client:
        resp = client.post(_sb_url(table), headers=headers, json=payload)
    if resp.status_code >= 300:
        raise HTTPException(status_code=502, detail=f"Supabase error ({table}): {resp.text}")
    data = resp.json()
    if isinstance(data, list):
        if not data:
            raise HTTPException(status_code=502, detail=f"Supabase insert into {table} returned no rows")
        return data[0]
    return data


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class SignalCreate(BaseModel):
    signal_type: str
    payload: Dict[str, Any]
    chain_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Scoring logic
#
# Each tool's score is the fraction of its last-20 signals representing a
# "good" outcome, judged from the signal's payload:
#
#   iron-thread  : payload["status"]         == "passed"
#   agentid      : payload["recommendation"] == "ALLOW"
#   chainthread  : payload["status"]         == "delivered"
#   policythread : payload["passed"]         == true
#
# A signal that's missing the relevant key counts as NOT good (conservative
# — an agent that forgot to report status shouldn't silently inflate health).
# ---------------------------------------------------------------------------

def _is_good_signal(tool: str, payload: Dict[str, Any]) -> bool:
    if tool == "iron-thread":
        return payload.get("status") == "passed"
    if tool == "agentid":
        return payload.get("recommendation") == "ALLOW"
    if tool == "chainthread":
        return payload.get("status") == "delivered"
    if tool == "policythread":
        return payload.get("passed") is True
    return False


def _tool_score(tool: str) -> float:
    signals = sb_select(
        "tw_signals",
        {
            "source_tool": f"eq.{tool}",
            "select": "payload",
            "order": "recorded_at.desc",
            "limit": "20",
        },
    )
    if not signals:
        return 1.0

    good = sum(1 for s in signals if _is_good_signal(tool, s.get("payload") or {}))
    return round(good / len(signals), 4)


def _status_label(score: float) -> str:
    if score >= 0.8:
        return "healthy"
    if score >= 0.5:
        return "degraded"
    return "critical"


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.post("/signals/{tool}")
def ingest_signal(tool: str, body: SignalCreate):
    if tool not in VALID_TOOLS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown tool '{tool}'. Must be one of: {', '.join(VALID_TOOLS)}",
        )

    record = sb_insert("tw_signals", {
        "source_tool": tool,
        "signal_type": body.signal_type,
        "payload": body.payload,
        "chain_id": body.chain_id,
    })
    return record


@app.get("/pipeline/health")
def pipeline_health():
    tool_scores = {tool: _tool_score(tool) for tool in VALID_TOOLS}
    health_score = round(sum(tool_scores.values()) / len(tool_scores), 4)

    return {
        "health_score": health_score,
        "status": _status_label(health_score),
        "tool_scores": tool_scores,
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/signals")
def list_signals(tool: Optional[str] = None, chain_id: Optional[str] = None, limit: int = 50):
    params = {"select": "*", "order": "recorded_at.desc", "limit": str(limit)}
    if tool:
        if tool not in VALID_TOOLS:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown tool '{tool}'. Must be one of: {', '.join(VALID_TOOLS)}",
            )
        params["source_tool"] = f"eq.{tool}"
    if chain_id:
        params["chain_id"] = f"eq.{chain_id}"
    return sb_select("tw_signals", params)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/")
def root():
    return {"tool": "ThreadWatch", "version": "1.0.0", "status": "running"}