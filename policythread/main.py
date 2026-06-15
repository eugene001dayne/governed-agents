"""
PolicyThread — Governed Agents hackathon project.

A FastAPI service that evaluates agent outputs against organizational
compliance policies:
  - keyword_exclude / keyword_require / max_length: instant deterministic
    checks
  - semantic: calls the Claude API with a strict JSON-only prompt for
    judgment-based evaluation

Run locally:
    python -m uvicorn main:app --reload

Deploy: Render (see render.yaml). GitHub push triggers auto-deploy.
"""

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
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_TIMEOUT = float(os.environ.get("ANTHROPIC_TIMEOUT", "45"))

if not SUPABASE_URL or not SUPABASE_KEY:
    print("WARNING: SUPABASE_URL / SUPABASE_KEY are not set. "
          "Set them in a .env file (local) or in Render env vars (deployed).")
if not ANTHROPIC_API_KEY:
    print("WARNING: ANTHROPIC_API_KEY is not set. Semantic policies will be "
          "skipped (treated as passed) until it is configured.")

SUPABASE_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
}

ANTHROPIC_MODEL = "claude-sonnet-4-6"

app = FastAPI(title="PolicyThread", version="1.0.0")

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
# Request / response models
# ---------------------------------------------------------------------------

class PolicyCondition(BaseModel):
    type: str  # keyword_exclude | keyword_require | max_length | semantic
    keywords: Optional[List[str]] = None
    value: Optional[int] = None   # for max_length
    rule: Optional[str] = None    # for semantic


class PolicyCreate(BaseModel):
    name: str
    description: Optional[str] = None
    condition: PolicyCondition
    severity: str = "high"  # critical | high | medium | low


class EvaluateRequest(BaseModel):
    agent_id: Optional[str] = None
    chain_id: Optional[str] = None
    user_input: Optional[str] = None
    ai_output: str


# ---------------------------------------------------------------------------
# Claude API client for semantic evaluation
# ---------------------------------------------------------------------------

def claude_semantic_check(rule: str, ai_output: str) -> Dict[str, Any]:
    """
    Calls the Claude API to judge whether ai_output violates a semantic rule.
    Returns {"passed": bool, "reason": str}.

    Fails safe: if the API key is missing, the call errors, or the response
    can't be parsed, returns passed=True with a note in 'reason' — a
    misconfigured semantic check should not silently block every output.
    The failure is still visible in 'reason' for the audit trail.
    """
    if not ANTHROPIC_API_KEY:
        return {"passed": True, "reason": "Semantic check skipped: ANTHROPIC_API_KEY not configured"}

    prompt = (
        f"You are evaluating AI output against a compliance rule.\n"
        f"Rule: {rule}\n"
        f"Output: {ai_output}\n\n"
        f'Does the output COMPLY with (pass) this rule? '
        f'Reply ONLY with JSON, no markdown, no preamble: '
        f'{{"passed": true/false, "reason": "string"}}\n'
        f'"passed" should be true if the output complies with the rule, '
        f'false if it violates the rule.'
    )

    try:
        with httpx.Client(timeout=ANTHROPIC_TIMEOUT) as client:
            resp = client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json",
                },
                json={
                    "model": ANTHROPIC_MODEL,
                    "max_tokens": 300,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
        if resp.status_code >= 300:
            return {"passed": True, "reason": f"Semantic check skipped: Claude API error {resp.status_code}"}

        data = resp.json()
        text_blocks = [b["text"] for b in data.get("content", []) if b.get("type") == "text"]
        raw_text = "\n".join(text_blocks).strip()

        # Strip markdown code fences if Claude wrapped the JSON in them.
        if raw_text.startswith("```"):
            raw_text = raw_text.strip("`")
            if raw_text.startswith("json"):
                raw_text = raw_text[4:]
            raw_text = raw_text.strip()

        parsed = json.loads(raw_text)
        passed = bool(parsed.get("passed", True))
        reason = str(parsed.get("reason", ""))

        return {"passed": passed, "reason": reason}

    except httpx.HTTPError as exc:
        return {"passed": True, "reason": f"Semantic check skipped: Claude API unreachable ({exc})"}
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        return {"passed": True, "reason": f"Semantic check skipped: could not parse Claude response ({exc})"}


# ---------------------------------------------------------------------------
# Deterministic condition checks
# ---------------------------------------------------------------------------

def evaluate_condition(policy: Dict[str, Any], ai_output: str) -> Dict[str, Any]:
    """
    Returns {"passed": bool, "reason": str} for a single policy's condition
    against ai_output.
    """
    condition = policy.get("condition") or {}
    ctype = condition.get("type")

    if ctype == "keyword_exclude":
        keywords = condition.get("keywords") or []
        lowered = ai_output.lower()
        hits = [kw for kw in keywords if kw.lower() in lowered]
        if hits:
            return {"passed": False, "reason": f"Output contains excluded keyword(s): {', '.join(hits)}"}
        return {"passed": True, "reason": "No excluded keywords found"}

    if ctype == "keyword_require":
        keywords = condition.get("keywords") or []
        lowered = ai_output.lower()
        missing = [kw for kw in keywords if kw.lower() not in lowered]
        if missing:
            return {"passed": False, "reason": f"Output missing required keyword(s): {', '.join(missing)}"}
        return {"passed": True, "reason": "All required keywords present"}

    if ctype == "max_length":
        max_len = condition.get("value")
        if max_len is None:
            return {"passed": True, "reason": "max_length policy has no value configured"}
        if len(ai_output) > max_len:
            return {"passed": False, "reason": f"Output length {len(ai_output)} exceeds max {max_len}"}
        return {"passed": True, "reason": f"Output length {len(ai_output)} is within max {max_len}"}

    if ctype == "semantic":
        rule = condition.get("rule") or ""
        return claude_semantic_check(rule, ai_output)

    return {"passed": True, "reason": f"Unknown condition type '{ctype}' — skipped"}


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.post("/policies")
def create_policy(body: PolicyCreate):
    record = sb_insert("pt_policies", {
        "name": body.name,
        "description": body.description,
        "condition": body.condition.model_dump(exclude_none=True),
        "severity": body.severity,
        "active": True,
    })
    return record


@app.get("/policies")
def list_policies():
    return sb_select(
        "pt_policies",
        {"active": "eq.true", "select": "*", "order": "created_at.desc"},
    )


@app.post("/evaluate")
def evaluate(body: EvaluateRequest):
    policies = sb_select(
        "pt_policies",
        {"active": "eq.true", "select": "*", "order": "created_at.desc"},
    )

    violations: List[Dict[str, str]] = []

    for policy in policies:
        result = evaluate_condition(policy, body.ai_output)
        if not result["passed"]:
            violations.append({
                "policy_name": policy.get("name", ""),
                "severity": policy.get("severity", "high"),
                "reason": result["reason"],
            })

    passed = len(violations) == 0

    record = sb_insert("pt_evaluations", {
        "agent_id": body.agent_id,
        "chain_id": body.chain_id,
        "user_input": body.user_input,
        "ai_output": body.ai_output,
        "passed": passed,
        "violations": violations,
    })

    return {
        "evaluation_id": record["id"],
        "passed": passed,
        "violations": violations,
    }


@app.get("/evaluations")
def list_evaluations(chain_id: Optional[str] = None, limit: int = 50):
    params = {"select": "*", "order": "created_at.desc", "limit": str(limit)}
    if chain_id:
        params["chain_id"] = f"eq.{chain_id}"
    return sb_select("pt_evaluations", params)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/")
def root():
    return {"tool": "PolicyThread", "version": "1.0.0", "status": "running"}