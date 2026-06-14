"""
ChainThread — Governed Agents hackathon project.

A FastAPI service that wraps agent-to-agent handoffs in signed envelopes:
  - Verifies sender identity via AgentID (POST /trust/lookup)
  - Checks the payload against a contract (required_fields)
  - Computes a SHA-256 signature over the envelope
  - Logs every hop to ct_handoff_log
  - Exposes the full envelope and the per-chain hop sequence

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
AGENTID_URL = os.environ.get("AGENTID_URL", "").rstrip("/")
AGENTID_TIMEOUT = float(os.environ.get("AGENTID_TIMEOUT", "45"))

if not SUPABASE_URL or not SUPABASE_KEY:
    print("WARNING: SUPABASE_URL / SUPABASE_KEY are not set. "
          "Set them in a .env file (local) or in Render env vars (deployed).")
if not AGENTID_URL:
    print("WARNING: AGENTID_URL is not set. Sender trust checks will be skipped/fail.")

SUPABASE_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
}

app = FastAPI(title="ChainThread", version="1.0.0")

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
        resp = client.get(
            _sb_url(table), headers=SUPABASE_HEADERS, params=params)
    if resp.status_code >= 300:
        raise HTTPException(
            status_code=502, detail=f"Supabase error ({table}): {resp.text}")
    return resp.json()


def sb_insert(table: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    headers = {**SUPABASE_HEADERS, "Prefer": "return=representation"}
    with httpx.Client(timeout=15) as client:
        resp = client.post(_sb_url(table), headers=headers, json=payload)
    if resp.status_code >= 300:
        raise HTTPException(
            status_code=502, detail=f"Supabase error ({table}): {resp.text}")
    data = resp.json()
    if isinstance(data, list):
        if not data:
            raise HTTPException(
                status_code=502, detail=f"Supabase insert into {table} returned no rows")
        return data[0]
    return data


# ---------------------------------------------------------------------------
# AgentID client
# ---------------------------------------------------------------------------

def agentid_trust_lookup(querying_agent: Optional[str], queried_agent: str,
                         min_reputation: float = 0.7) -> Dict[str, Any]:
    """
    Calls AgentID POST /trust/lookup. If AgentID is unreachable or
    misconfigured, fail closed: treat as BLOCK rather than silently
    allowing unverified handoffs.
    """
    if not AGENTID_URL:
        return {
            "trusted": False,
            "recommendation": "BLOCK",
            "reason": "AGENTID_URL not configured",
            "reputation_score": 0.0,
        }

    body = {
        "querying_agent": querying_agent,
        "queried_agent": queried_agent,
        "min_reputation": min_reputation,
    }
    try:
        # AGENTID_TIMEOUT is generous (default 45s) because Render free-tier
        # services spin down when idle; the first request after a cold spell
        # can take 30-60s to wake the container. A short timeout here would
        # misread "still booting" as "untrusted" and block real handoffs.
        with httpx.Client(timeout=AGENTID_TIMEOUT) as client:
            resp = client.post(f"{AGENTID_URL}/trust/lookup", json=body)
        if resp.status_code >= 300:
            return {
                "trusted": False,
                "recommendation": "BLOCK",
                "reason": f"AgentID error: {resp.text}",
                "reputation_score": 0.0,
            }
        return resp.json()
    except httpx.HTTPError as exc:
        return {
            "trusted": False,
            "recommendation": "BLOCK",
            "reason": f"AgentID unreachable: {exc}",
            "reputation_score": 0.0,
        }


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class Contract(BaseModel):
    required_fields: List[str] = []
    on_fail: str = "block"


class EnvelopeCreate(BaseModel):
    chain_id: str
    sender_id: str
    sender_public_key: Optional[str] = None
    receiver_id: str
    payload: Dict[str, Any]
    contract: Optional[Contract] = None


# ---------------------------------------------------------------------------
# Signature / contract logic
# ---------------------------------------------------------------------------

def compute_signature(sender_id: str, receiver_id: str, payload: Dict[str, Any], chain_id: str) -> str:
    canonical_payload = json.dumps(payload, sort_keys=True)
    base = f"{sender_id}{receiver_id}{canonical_payload}{chain_id}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


def check_contract(payload: Dict[str, Any], contract: Optional[Contract]) -> List[str]:
    if not contract:
        return []
    violations = []
    for field in contract.required_fields:
        if field not in payload:
            violations.append(f"Missing required field: {field}")
    return violations


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.post("/envelopes")
def create_envelope(body: EnvelopeCreate):
    violations: List[str] = []

    # 1. Verify sender identity via AgentID
    trust_result = agentid_trust_lookup(
        querying_agent=body.receiver_id,
        queried_agent=body.sender_id,
        min_reputation=0.7,
    )
    sender_trusted = trust_result.get("recommendation") == "ALLOW"
    if not sender_trusted:
        violations.append(
            f"Sender identity check failed: {trust_result.get('reason', 'not trusted')}"
        )

    # 2. Check payload against contract
    contract_violations = check_contract(body.payload, body.contract)
    violations.extend(contract_violations)

    contract_passed = len(contract_violations) == 0
    status = "delivered" if (sender_trusted and contract_passed) else "blocked"

    # 3. Compute signature regardless of status — the envelope is still a
    #    real artifact even if blocked, useful for audit.
    signature = compute_signature(
        body.sender_id, body.receiver_id, body.payload, body.chain_id)

    contract_dict = body.contract.model_dump() if body.contract else None

    envelope = sb_insert("ct_envelopes", {
        "chain_id": body.chain_id,
        "sender_id": body.sender_id,
        "receiver_id": body.receiver_id,
        "payload": body.payload,
        "contract": contract_dict,
        "contract_passed": contract_passed,
        "violations": violations,
        "signature": signature,
        "status": status,
    })

    # 4. Determine hop number for this chain
    existing_hops = sb_select(
        "ct_handoff_log",
        {"chain_id": f"eq.{body.chain_id}", "select": "hop_number",
            "order": "hop_number.desc", "limit": "1"},
    )
    hop_number = (existing_hops[0]["hop_number"] + 1) if existing_hops else 1

    # 5. Log the hop
    sb_insert("ct_handoff_log", {
        "envelope_id": envelope["id"],
        "chain_id": body.chain_id,
        "hop_number": hop_number,
        "from_agent": body.sender_id,
        "to_agent": body.receiver_id,
        "passed": status == "delivered",
    })

    return envelope


@app.get("/envelopes/{envelope_id}")
def get_envelope(envelope_id: str):
    envelopes = sb_select(
        "ct_envelopes", {"id": f"eq.{envelope_id}", "select": "*"})
    if not envelopes:
        raise HTTPException(status_code=404, detail="Envelope not found")
    return envelopes[0]


@app.get("/chain/{chain_id}")
def get_chain(chain_id: str):
    hops = sb_select(
        "ct_handoff_log",
        {"chain_id": f"eq.{chain_id}", "select": "*", "order": "hop_number.asc"},
    )
    all_passed = all(h["passed"] for h in hops) if hops else True
    return {
        "chain_id": chain_id,
        "hops": hops,
        "total_hops": len(hops),
        "all_passed": all_passed,
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/")
def root():
    return {"tool": "ChainThread", "version": "1.0.0", "status": "running"}
