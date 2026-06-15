"""
Local test harness for PolicyThread — mocks Supabase and the Claude API so
we can exercise /policies, /evaluate, and /evaluations end-to-end without a
live database or API key.

Run: python test_local.py
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
    "pt_policies": [],
    "pt_evaluations": [],
}


def fake_sb_select(table, params):
    rows = list(FAKE_DB[table])

    for key, val in params.items():
        if key in ("order", "select", "limit"):
            continue
        if val == "eq.true":
            rows = [r for r in rows if r.get(key) is True]
        elif val.startswith("eq."):
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

# ---------------------------------------------------------------------------
# Fake Claude API — keyed by which rule is being checked so we can return
# different verdicts for different semantic policies in one test run.
# ---------------------------------------------------------------------------


def fake_claude_semantic_check(rule, ai_output):
    if "race, gender, or religion" in rule:
        if "because the applicant is" in ai_output.lower():
            return {"passed": False, "reason": "Output ties the decision to a protected characteristic"}
        return {"passed": True, "reason": "No protected-characteristic reasoning found"}
    return {"passed": True, "reason": "Stubbed: rule not recognized in test"}


main.claude_semantic_check = fake_claude_semantic_check

client = TestClient(main.app)

# ---------------------------------------------------------------------------
# 1. Root / health
# ---------------------------------------------------------------------------

r = client.get("/")
print("GET /            ->", r.status_code, r.json())

r = client.get("/health")
print("GET /health      ->", r.status_code, r.json())

# ---------------------------------------------------------------------------
# 2. Create policies — one of each condition type
# ---------------------------------------------------------------------------

policies_to_create = [
    {
        "name": "No discriminatory language",
        "description": "Output must not contain slurs or discriminatory terms",
        "condition": {"type": "keyword_exclude", "keywords": ["slur1", "slur2"]},
        "severity": "critical",
    },
    {
        "name": "Must include disclosure",
        "description": "Loan decisions must include a reasoning disclosure",
        "condition": {"type": "keyword_require", "keywords": ["reasoning"]},
        "severity": "medium",
    },
    {
        "name": "Output length cap",
        "description": "Outputs must stay under 500 characters",
        "condition": {"type": "max_length", "value": 500},
        "severity": "low",
    },
    {
        "name": "No discriminatory loan reasoning",
        "description": "Decisions must not be based on protected characteristics",
        "condition": {
            "type": "semantic",
            "rule": "The output must not recommend loan approval or denial based on race, gender, or religion",
        },
        "severity": "critical",
    },
]

created_ids = []
for p in policies_to_create:
    r = client.post("/policies", json=p)
    assert r.status_code == 200, r.text
    created_ids.append(r.json()["id"])
print(f"\nPOST /policies x{len(policies_to_create)} -> all 200")

# ---------------------------------------------------------------------------
# 3. GET /policies
# ---------------------------------------------------------------------------

r = client.get("/policies")
print("\nGET /policies ->", r.status_code,
      f"({len(r.json())} active policies)")
assert len(r.json()) == 4

# ---------------------------------------------------------------------------
# 4. Evaluate — clean output, should pass all policies
# ---------------------------------------------------------------------------

clean_output = json.dumps({
    "decision": "approved",
    "reasoning": "Applicant has stable income and good credit history.",
})

r = client.post("/evaluate", json={
    "agent_id": "decision-agent",
    "chain_id": "loan-test-1",
    "user_input": "Review this application",
    "ai_output": clean_output,
})
print("\nPOST /evaluate (clean output) ->", r.status_code)
result = r.json()
print(json.dumps(result, indent=2))
assert result["passed"] is True
assert result["violations"] == []

# ---------------------------------------------------------------------------
# 5. Evaluate — output with excluded keyword
# ---------------------------------------------------------------------------

bad_keyword_output = json.dumps({
    "decision": "declined",
    "reasoning": "This is a slur1 in the output that should be caught.",
})

r = client.post("/evaluate", json={
    "agent_id": "decision-agent",
    "chain_id": "loan-test-2",
    "user_input": "Review this application",
    "ai_output": bad_keyword_output,
})
print("\nPOST /evaluate (excluded keyword) ->", r.status_code)
result = r.json()
print(json.dumps(result, indent=2))
assert result["passed"] is False
assert any(v["policy_name"] ==
           "No discriminatory language" for v in result["violations"])

# ---------------------------------------------------------------------------
# 6. Evaluate — output missing required keyword ("reasoning")
# ---------------------------------------------------------------------------

missing_keyword_output = json.dumps(
    {"decision": "approved", "notes": "Looks fine."})

r = client.post("/evaluate", json={
    "agent_id": "decision-agent",
    "chain_id": "loan-test-3",
    "user_input": "Review this application",
    "ai_output": missing_keyword_output,
})
print("\nPOST /evaluate (missing required keyword) ->", r.status_code)
result = r.json()
print(json.dumps(result, indent=2))
assert result["passed"] is False
assert any(v["policy_name"] ==
           "Must include disclosure" for v in result["violations"])

# ---------------------------------------------------------------------------
# 7. Evaluate — output exceeds max_length
# ---------------------------------------------------------------------------

long_output = json.dumps({"decision": "approved", "reasoning": "x" * 600})

r = client.post("/evaluate", json={
    "agent_id": "decision-agent",
    "chain_id": "loan-test-4",
    "user_input": "Review this application",
    "ai_output": long_output,
})
print("\nPOST /evaluate (exceeds max_length) ->", r.status_code)
result = r.json()
print(json.dumps({**result, "violations": result["violations"]}, indent=2))
assert result["passed"] is False
assert any(v["policy_name"] ==
           "Output length cap" for v in result["violations"])

# ---------------------------------------------------------------------------
# 8. Evaluate — semantic violation (discriminatory reasoning)
# ---------------------------------------------------------------------------

discriminatory_output = json.dumps({
    "decision": "declined",
    "reasoning": "We declined this loan because the applicant is of a certain religion.",
})

r = client.post("/evaluate", json={
    "agent_id": "decision-agent",
    "chain_id": "loan-test-5",
    "user_input": "Review this application",
    "ai_output": discriminatory_output,
})
print("\nPOST /evaluate (semantic violation) ->", r.status_code)
result = r.json()
print(json.dumps(result, indent=2))
assert result["passed"] is False
assert any(v["policy_name"] ==
           "No discriminatory loan reasoning" for v in result["violations"])

# ---------------------------------------------------------------------------
# 9. Evaluate — multiple simultaneous violations
# ---------------------------------------------------------------------------

multi_violation_output = json.dumps({
    "decision": "declined",
    "notes": "slur1 used here, no disclosure field, " + ("x" * 600),
})

r = client.post("/evaluate", json={
    "agent_id": "decision-agent",
    "chain_id": "loan-test-6",
    "user_input": "Review this application",
    "ai_output": multi_violation_output,
})
print("\nPOST /evaluate (multiple violations) ->", r.status_code)
result = r.json()
print(
    f"  passed={result['passed']}, violation_count={len(result['violations'])}")
for v in result["violations"]:
    print(f"    - {v['policy_name']} ({v['severity']}): {v['reason']}")
assert result["passed"] is False
# keyword_exclude, keyword_require, max_length
assert len(result["violations"]) == 3

# ---------------------------------------------------------------------------
# 10. GET /evaluations
# ---------------------------------------------------------------------------

r = client.get("/evaluations")
print("\nGET /evaluations ->", r.status_code, f"({len(r.json())} total)")
assert len(r.json()) == 6

# ---------------------------------------------------------------------------
# 11. GET /evaluations?chain_id=loan-test-2
# ---------------------------------------------------------------------------

r = client.get("/evaluations", params={"chain_id": "loan-test-2"})
print("GET /evaluations?chain_id=loan-test-2 ->",
      r.status_code, f"({len(r.json())} match)")
assert len(r.json()) == 1
assert r.json()[0]["chain_id"] == "loan-test-2"

print("\nALL CHECKS PASSED")
