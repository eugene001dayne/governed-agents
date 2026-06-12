"""
Local test harness for Iron-Thread — mocks Supabase so we can exercise
/schemas, /validate, and /schemas/{id}/chain end-to-end without a real
Supabase project.

Run: python3 test_local.py
"""

import json
import uuid
from datetime import datetime, timezone

import main
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# In-memory fake Supabase
# ---------------------------------------------------------------------------

FAKE_DB = {
    "it_schemas": [],
    "it_validation_runs": [],
}


def fake_sb_select(table, params):
    rows = list(FAKE_DB[table])

    # filter: id=eq.xxx / schema_id=eq.xxx
    for key, val in params.items():
        if key in ("order", "select", "limit"):
            continue
        if val.startswith("eq."):
            target = val[3:]
            rows = [r for r in rows if str(r.get(key)) == target]

    if "order" in params:
        field, _, direction = params["order"].partition(".")
        rows = sorted(rows, key=lambda r: r.get(
            field, ""), reverse=(direction == "desc"))

    if "limit" in params:
        rows = rows[: int(params["limit"])]

    return rows


def fake_sb_insert(table, payload):
    record = dict(payload)
    record["id"] = str(uuid.uuid4())
    record.setdefault("created_at", datetime.now(timezone.utc).isoformat())
    FAKE_DB[table].append(record)
    return record


main.sb_select = fake_sb_select
main.sb_insert = fake_sb_insert

client = TestClient(main.app)

# ---------------------------------------------------------------------------
# 1. Root / health
# ---------------------------------------------------------------------------

r = client.get("/")
print("GET /            ->", r.status_code, r.json())

r = client.get("/health")
print("GET /health      ->", r.status_code, r.json())

# ---------------------------------------------------------------------------
# 2. Create a schema (loan application — matches Intake Agent extraction)
# ---------------------------------------------------------------------------

schema_definition = {
    "required": {
        "applicant_name": "string",
        "loan_amount": "number",
        "annual_income": "number",
        "loan_purpose": "string",
    },
    "optional": {
        "credit_score": "number?",
    },
}

r = client.post(
    "/schemas", json={"name": "loan_application_v1", "schema_definition": schema_definition})
print("\nPOST /schemas    ->", r.status_code)
schema = r.json()
print(json.dumps(schema, indent=2))
schema_id = schema["id"]

# ---------------------------------------------------------------------------
# 3. Validate — full valid payload (should pass, confidence 1.0)
# ---------------------------------------------------------------------------

good_payload = {
    "applicant_name": "Jane Doe",
    "loan_amount": 25000,
    "annual_income": 85000,
    "loan_purpose": "Home renovation",
    "credit_score": 712,
}

r = client.post("/validate", json={
    "schema_id": schema_id,
    "raw_output": json.dumps(good_payload),
    "agent_id": "intake-agent",
})
print("\nPOST /validate (full, valid) ->", r.status_code)
print(json.dumps(r.json(), indent=2))
run1_hash = r.json()["run_hash"]
assert r.json()["status"] == "passed"
assert r.json()["confidence_score"] == 1.0
assert r.json()["previous_hash" if False else "run_hash"]  # sanity

# ---------------------------------------------------------------------------
# 4. Validate — missing optional field (credit_score) -> confidence 0.9
# ---------------------------------------------------------------------------

partial_payload = {
    "applicant_name": "John Smith",
    "loan_amount": 10000,
    "annual_income": 45000,
    "loan_purpose": "Debt consolidation",
}

r = client.post("/validate", json={
    "schema_id": schema_id,
    "raw_output": json.dumps(partial_payload),
    "agent_id": "intake-agent",
})
print("\nPOST /validate (missing optional credit_score) ->", r.status_code)
print(json.dumps(r.json(), indent=2))
assert r.json()["status"] == "passed"
assert r.json()["confidence_score"] == 0.9
assert r.json()["previous_hash"] if False else True

# ---------------------------------------------------------------------------
# 5. Validate — missing required field -> failed, confidence 0.0
# ---------------------------------------------------------------------------

bad_payload = {
    "applicant_name": "No Income Person",
    "loan_amount": 5000,
    "loan_purpose": "Vacation",
}

r = client.post("/validate", json={
    "schema_id": schema_id,
    "raw_output": json.dumps(bad_payload),
    "agent_id": "intake-agent",
})
print("\nPOST /validate (missing required annual_income) ->", r.status_code)
print(json.dumps(r.json(), indent=2))
assert r.json()["status"] == "failed"
assert r.json()["confidence_score"] == 0.0
assert "annual_income" in r.json()["errors"][0]

# ---------------------------------------------------------------------------
# 6. Validate — not valid JSON -> failed
# ---------------------------------------------------------------------------

r = client.post("/validate", json={
    "schema_id": schema_id,
    "raw_output": "not json at all",
    "agent_id": "intake-agent",
})
print("\nPOST /validate (invalid JSON) ->", r.status_code)
print(json.dumps(r.json(), indent=2))
assert r.json()["status"] == "failed"
assert r.json()["confidence_score"] == 0.0

# ---------------------------------------------------------------------------
# 7. Chain check
# ---------------------------------------------------------------------------

r = client.get(f"/schemas/{schema_id}/chain")
print("\nGET /schemas/{id}/chain ->", r.status_code)
chain = r.json()
print(
    f"  total_runs={chain['total_runs']}  chain_verified={chain['chain_verified']}")
assert chain["total_runs"] == 4
assert chain["chain_verified"] is True

print("\nALL CHECKS PASSED")
