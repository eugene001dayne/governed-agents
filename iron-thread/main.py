"""
Iron-Thread — Governed Agents hackathon project.

A FastAPI service that:
  - Stores JSON schemas agents must conform to
  - Validates agent outputs against those schemas
  - Scores confidence (1.0 = perfect, drops 0.1 per missing optional field,
    0.0 if any required field is missing/wrong type)
  - Maintains a tamper-evident SHA-256 hash chain per schema

Run locally:
    python -m uvicorn main:app --reload

Deploy: Render (see render.yaml). GitHub push triggers auto-deploy.
"""

import hashlib
import json
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

app = FastAPI(title="Iron-Thread", version="1.0.0")

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

class SchemaCreate(BaseModel):
    name: str
    schema_definition: Dict[str, Any]


class ValidateRequest(BaseModel):
    schema_id: str
    raw_output: str
    agent_id: Optional[str] = "unknown"


# ---------------------------------------------------------------------------
# Validation logic
# ---------------------------------------------------------------------------

def check_type(value: Any, expected_type: Any) -> bool:
    """
    Supported type tags: string, number, integer, boolean, array, object, null, any.
    Append '?' to a type (e.g. "number?") to allow null as well.
    """
    if not isinstance(expected_type, str):
        return True  # unrecognised spec — don't fail validation on it

    nullable = expected_type.endswith("?")
    t = expected_type[:-1] if nullable else expected_type

    if value is None:
        return nullable or t == "null" or t == "any"

    if t == "string":
        return isinstance(value, str)
    if t == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if t == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if t == "boolean":
        return isinstance(value, bool)
    if t == "array":
        return isinstance(value, list)
    if t == "object":
        return isinstance(value, dict)
    if t == "null":
        return value is None
    if t == "any":
        return True

    return True  # unknown type tag — don't fail validation on it


def run_validation(data: Dict[str, Any], schema_definition: Dict[str, Any]):
    """
    Returns (status, confidence_score, errors).

    - status is "failed" if any required field is missing or wrong type, else "passed"
    - confidence_score starts at 1.0, drops 0.1 per missing/mis-typed optional field
    - confidence_score is 0.0 whenever status is "failed"
    """
    required = schema_definition.get("required", {}) or {}
    optional = schema_definition.get("optional", {}) or {}

    errors: List[str] = []
    failed = False

    for field, expected_type in required.items():
        if field not in data:
            errors.append(f"Missing required field: {field}")
            failed = True
            continue
        if not check_type(data[field], expected_type):
            errors.append(
                f"Type mismatch for required field '{field}': "
                f"expected {expected_type}, got {type(data[field]).__name__}"
            )
            failed = True

    missing_optional = 0
    for field, expected_type in optional.items():
        if field not in data:
            errors.append(f"Missing optional field: {field}")
            missing_optional += 1
            continue
        if not check_type(data[field], expected_type):
            errors.append(
                f"Type mismatch for optional field '{field}': "
                f"expected {expected_type}, got {type(data[field]).__name__}"
            )
            missing_optional += 1

    if failed:
        return "failed", 0.0, errors

    confidence = round(max(0.0, 1.0 - 0.1 * missing_optional), 2)
    return "passed", confidence, errors


def compute_hash(agent_id: str, schema_id: str, raw_output: str,
                 status: str, created_at: str, previous_hash: str) -> str:
    base = f"{agent_id}{schema_id}{raw_output}{status}{created_at}{previous_hash}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.post("/schemas")
def create_schema(body: SchemaCreate):
    record = sb_insert("it_schemas", {
        "name": body.name,
        "schema_definition": body.schema_definition,
    })
    return record


@app.post("/validate")
def validate(body: ValidateRequest):
    schemas = sb_select("it_schemas", {"id": f"eq.{body.schema_id}", "select": "*"})
    if not schemas:
        raise HTTPException(status_code=404, detail="Schema not found")
    schema_definition = schemas[0].get("schema_definition") or {}

    errors: List[str] = []
    validated_output: Optional[Dict[str, Any]] = None

    try:
        parsed = json.loads(body.raw_output)
        if not isinstance(parsed, dict):
            errors.append("raw_output must be a JSON object")
            parsed = None
    except json.JSONDecodeError as e:
        errors.append(f"raw_output is not valid JSON: {e}")
        parsed = None

    if parsed is None:
        status = "failed"
        confidence_score = 0.0
    else:
        validated_output = parsed
        status, confidence_score, type_errors = run_validation(parsed, schema_definition)
        errors.extend(type_errors)

    # Find previous hash for this schema's chain
    previous_runs = sb_select(
        "it_validation_runs",
        {
            "schema_id": f"eq.{body.schema_id}",
            "order": "created_at.desc",
            "limit": "1",
            "select": "run_hash",
        },
    )
    previous_hash = previous_runs[0]["run_hash"] if previous_runs else "GENESIS"

    created_at = datetime.now(timezone.utc).isoformat()
    agent_id = body.agent_id or "unknown"
    run_hash = compute_hash(agent_id, body.schema_id, body.raw_output, status, created_at, previous_hash)

    record = sb_insert("it_validation_runs", {
        "schema_id": body.schema_id,
        "raw_output": body.raw_output,
        "validated_output": validated_output,
        "status": status,
        "confidence_score": confidence_score,
        "run_hash": run_hash,
        "previous_hash": previous_hash,
        "agent_id": agent_id,
        "created_at": created_at,
    })

    return {
        "run_id": record.get("id"),
        "status": status,
        "confidence_score": confidence_score,
        "errors": errors,
        "run_hash": run_hash,
    }


@app.get("/schemas/{schema_id}/chain")
def get_chain(schema_id: str):
    runs = sb_select(
        "it_validation_runs",
        {
            "schema_id": f"eq.{schema_id}",
            "order": "created_at.asc",
            "select": "*",
        },
    )

    chain_verified = True
    expected_previous = "GENESIS"

    for run in runs:
        recomputed = compute_hash(
            run.get("agent_id") or "unknown",
            schema_id,
            run.get("raw_output", ""),
            run.get("status", ""),
            run.get("created_at", ""),
            run.get("previous_hash", ""),
        )
        if recomputed != run.get("run_hash"):
            chain_verified = False
        if run.get("previous_hash") != expected_previous:
            chain_verified = False
        expected_previous = run.get("run_hash")

    return {
        "schema_id": schema_id,
        "total_runs": len(runs),
        "chain_verified": chain_verified,
        "runs": runs,
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/")
def root():
    return {"tool": "Iron-Thread", "version": "1.0.0", "status": "running"}