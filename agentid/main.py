"""
AgentID — Governed Agents hackathon project.

A FastAPI service that:
  - Issues a SHA-256 credential to each registered agent
  - Verifies an agent's credential against its public key
  - Makes trust decisions (ALLOW/BLOCK) based on credential validity +
    reputation score, and logs every lookup
  - Updates reputation scores after interactions

Run locally:
    python -m uvicorn main:app --reload

Deploy: Render (see render.yaml). GitHub push triggers auto-deploy.
"""

import hashlib
import json
import os
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

app = FastAPI(title="AgentID", version="1.0.0")

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


def sb_update(table: str, params: Dict[str, str], payload: Dict[str, Any]) -> Dict[str, Any]:
    headers = {**SUPABASE_HEADERS, "Prefer": "return=representation"}
    with httpx.Client(timeout=15) as client:
        resp = client.patch(_sb_url(table), headers=headers, params=params, json=payload)
    if resp.status_code >= 300:
        raise HTTPException(status_code=502, detail=f"Supabase error ({table}): {resp.text}")
    data = resp.json()
    if isinstance(data, list):
        if not data:
            raise HTTPException(status_code=404, detail=f"No row updated in {table}")
        return data[0]
    return data


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class AgentCreate(BaseModel):
    agent_id: str
    agent_name: str
    public_key: str


class VerifyRequest(BaseModel):
    public_key: str


class TrustLookupRequest(BaseModel):
    querying_agent: Optional[str] = None
    queried_agent: str
    min_reputation: float = 0.7


class ReputationUpdate(BaseModel):
    interaction_success: bool
    violation: bool = False
    pii_incident: bool = False


# ---------------------------------------------------------------------------
# Credential logic
# ---------------------------------------------------------------------------

CREDENTIAL_VERSION = "AgentID-v1.0"


def compute_credential_hash(agent_id: str, public_key: str) -> str:
    """
    credential_hash = sha256(json.dumps({agent_id, public_key, version}, sort_keys=True))
    Canonical JSON ensures the hash is reproducible regardless of how the
    caller orders fields.
    """
    payload = {"agent_id": agent_id, "public_key": public_key, "version": CREDENTIAL_VERSION}
    canonical = json.dumps(payload, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.post("/agents")
def register_agent(body: AgentCreate):
    # Idempotent: if this agent_id is already registered, return the
    # existing record instead of erroring (agents may re-register on restart).
    existing = sb_select("aid_agents", {"agent_id": f"eq.{body.agent_id}", "select": "*"})
    if existing:
        return existing[0]

    credential_hash = compute_credential_hash(body.agent_id, body.public_key)

    record = sb_insert("aid_agents", {
        "agent_id": body.agent_id,
        "agent_name": body.agent_name,
        "public_key": body.public_key,
        "credential_hash": credential_hash,
        "reputation_score": 1.0,
        "active": True,
        "total_interactions": 0,
        "successful_interactions": 0,
    })
    return record


@app.post("/agents/{agent_id}/verify")
def verify_agent(agent_id: str, body: VerifyRequest):
    agents = sb_select("aid_agents", {"agent_id": f"eq.{agent_id}", "select": "*"})
    if not agents:
        raise HTTPException(status_code=404, detail="Agent not found")
    agent = agents[0]

    recomputed = compute_credential_hash(agent_id, body.public_key)
    verified = (recomputed == agent.get("credential_hash")) and bool(agent.get("active"))

    return {
        "verified": verified,
        "agent_name": agent.get("agent_name"),
        "reputation_score": agent.get("reputation_score"),
    }


@app.post("/trust/lookup")
def trust_lookup(body: TrustLookupRequest):
    agents = sb_select("aid_agents", {"agent_id": f"eq.{body.queried_agent}", "select": "*"})

    if not agents:
        trusted = False
        recommendation = "BLOCK"
        reason = f"Agent '{body.queried_agent}' is not registered"
        reputation_score = 0.0
    else:
        agent = agents[0]
        reputation_score = agent.get("reputation_score", 0.0)

        if not agent.get("active", False):
            trusted = False
            recommendation = "BLOCK"
            reason = f"Agent '{body.queried_agent}' credential is inactive"
        elif reputation_score < body.min_reputation:
            trusted = False
            recommendation = "BLOCK"
            reason = (
                f"Reputation {reputation_score} is below minimum "
                f"{body.min_reputation}"
            )
        else:
            trusted = True
            recommendation = "ALLOW"
            reason = (
                f"Reputation {reputation_score} meets minimum "
                f"{body.min_reputation}"
            )

    # Log every lookup
    sb_insert("aid_trust_lookups", {
        "querying_agent": body.querying_agent,
        "queried_agent": body.queried_agent,
        "trusted": trusted,
        "recommendation": recommendation,
        "reputation_score": reputation_score,
    })

    return {
        "trusted": trusted,
        "recommendation": recommendation,
        "reason": reason,
        "reputation_score": reputation_score,
    }


@app.patch("/agents/{agent_id}/reputation")
def update_reputation(agent_id: str, body: ReputationUpdate):
    agents = sb_select("aid_agents", {"agent_id": f"eq.{agent_id}", "select": "*"})
    if not agents:
        raise HTTPException(status_code=404, detail="Agent not found")
    agent = agents[0]

    total = int(agent.get("total_interactions") or 0) + 1
    successful = int(agent.get("successful_interactions") or 0)
    if body.interaction_success:
        successful += 1

    base = successful / total

    penalty = 0.0
    if body.violation:
        penalty += 0.02
    if body.pii_incident:
        penalty += 0.05

    new_score = max(0.0, min(1.0, base - penalty))

    updated = sb_update(
        "aid_agents",
        {"agent_id": f"eq.{agent_id}"},
        {
            "reputation_score": round(new_score, 4),
            "total_interactions": total,
            "successful_interactions": successful,
        },
    )
    return updated


@app.get("/agents")
def list_agents():
    return sb_select("aid_agents", {"select": "*", "order": "created_at.desc"})


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/")
def root():
    return {"tool": "AgentID", "version": "1.0.0", "status": "running"}