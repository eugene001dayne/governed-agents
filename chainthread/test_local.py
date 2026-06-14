"""
Local test harness for ChainThread — mocks Supabase and the AgentID HTTP
call so we can exercise /envelopes, /envelopes/{id}, and /chain/{chain_id}
end-to-end without a live database or AgentID service.

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
    "ct_envelopes": [],
    "ct_handoff_log": [],
}


def fake_sb_select(table, params):
    rows = list(FAKE_DB[table])

    for key, val in params.items():
        if key in ("order", "select", "limit"):
            continue
        if val.startswith("eq."):
            target = val[3:]
            rows = [r for r in rows if str(r.get(key)) == target]

    if "order" in params:
        field, _, direction = params["order"].partition(".")
        rows = sorted(rows, key=lambda r: r.get(
            field, 0), reverse=(direction == "desc"))

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
# Fake AgentID trust lookup
#
# - "intake-agent"   -> trusted (ALLOW)
# - "risk-agent"     -> trusted (ALLOW)
# - "shady-agent"    -> not trusted (BLOCK)
# ---------------------------------------------------------------------------


def fake_trust_lookup(querying_agent, queried_agent, min_reputation=0.7):
    if queried_agent in ("intake-agent", "risk-agent", "compliance-agent", "decision-agent"):
        return {
            "trusted": True,
            "recommendation": "ALLOW",
            "reason": f"Reputation 1.0 meets minimum {min_reputation}",
            "reputation_score": 1.0,
        }
    return {
        "trusted": False,
        "recommendation": "BLOCK",
        "reason": f"Reputation 0.0 is below minimum {min_reputation}",
        "reputation_score": 0.0,
    }


main.agentid_trust_lookup = fake_trust_lookup

client = TestClient(main.app)

# ---------------------------------------------------------------------------
# 1. Root / health
# ---------------------------------------------------------------------------

r = client.get("/")
print("GET /            ->", r.status_code, r.json())

r = client.get("/health")
print("GET /health      ->", r.status_code, r.json())

# ---------------------------------------------------------------------------
# 2. Successful handoff: intake-agent -> risk-agent, contract satisfied
# ---------------------------------------------------------------------------

chain_id = "loan-test1234"

r = client.post("/envelopes", json={
    "chain_id": chain_id,
    "sender_id": "intake-agent",
    "sender_public_key": "intake-public-key-v1",
    "receiver_id": "risk-agent",
    "payload": {
        "applicant_name": "Jane Doe",
        "loan_amount": 25000,
        "annual_income": 85000,
        "loan_purpose": "Home renovation",
    },
    "contract": {
        "required_fields": ["applicant_name", "loan_amount", "annual_income", "loan_purpose"],
        "on_fail": "block",
    },
})
print("\nPOST /envelopes (intake -> risk, contract OK) ->", r.status_code)
envelope1 = r.json()
print(json.dumps(envelope1, indent=2))
assert envelope1["status"] == "delivered"
assert envelope1["contract_passed"] is True
assert envelope1["violations"] == []
assert envelope1["signature"]
envelope1_id = envelope1["id"]

# ---------------------------------------------------------------------------
# 3. Handoff with a missing required field -> blocked (contract violation)
# ---------------------------------------------------------------------------

r = client.post("/envelopes", json={
    "chain_id": chain_id,
    "sender_id": "risk-agent",
    "sender_public_key": "risk-public-key-v1",
    "receiver_id": "compliance-agent",
    "payload": {
        "applicant_name": "Jane Doe",
        "risk_level": "low",
        # missing risk_score on purpose
    },
    "contract": {
        "required_fields": ["applicant_name", "risk_level", "risk_score"],
        "on_fail": "block",
    },
})
print("\nPOST /envelopes (risk -> compliance, missing risk_score) ->", r.status_code)
envelope2 = r.json()
print(json.dumps(envelope2, indent=2))
assert envelope2["status"] == "blocked"
assert envelope2["contract_passed"] is False
assert any("risk_score" in v for v in envelope2["violations"])

# ---------------------------------------------------------------------------
# 4. Handoff from an untrusted sender -> blocked (identity check)
# ---------------------------------------------------------------------------

r = client.post("/envelopes", json={
    "chain_id": chain_id,
    "sender_id": "shady-agent",
    "sender_public_key": "shady-key",
    "receiver_id": "decision-agent",
    "payload": {"foo": "bar"},
    "contract": {"required_fields": [], "on_fail": "block"},
})
print("\nPOST /envelopes (shady-agent -> decision, identity check fails) ->", r.status_code)
envelope3 = r.json()
print(json.dumps(envelope3, indent=2))
assert envelope3["status"] == "blocked"
assert any("identity check failed" in v for v in envelope3["violations"])

# ---------------------------------------------------------------------------
# 5. Handoff with no contract at all -> always passes contract check
# ---------------------------------------------------------------------------

r = client.post("/envelopes", json={
    "chain_id": chain_id,
    "sender_id": "compliance-agent",
    "sender_public_key": "compliance-public-key-v1",
    "receiver_id": "decision-agent",
    "payload": {"compliance_result": {"passed": True}},
})
print("\nPOST /envelopes (compliance -> decision, no contract) ->", r.status_code)
envelope4 = r.json()
print(json.dumps(envelope4, indent=2))
assert envelope4["status"] == "delivered"
assert envelope4["contract_passed"] is True
assert envelope4["contract"] is None

# ---------------------------------------------------------------------------
# 6. GET /envelopes/{id}
# ---------------------------------------------------------------------------

r = client.get(f"/envelopes/{envelope1_id}")
print("\nGET /envelopes/{id} ->", r.status_code)
fetched = r.json()
assert fetched["id"] == envelope1_id
assert fetched["payload"]["applicant_name"] == "Jane Doe"
print("  payload.applicant_name:", fetched["payload"]["applicant_name"])

# ---------------------------------------------------------------------------
# 7. GET /envelopes/{id} for nonexistent id -> 404
# ---------------------------------------------------------------------------

r = client.get("/envelopes/00000000-0000-0000-0000-000000000000")
print("\nGET /envelopes/{nonexistent} ->", r.status_code)
assert r.status_code == 404

# ---------------------------------------------------------------------------
# 8. GET /chain/{chain_id} — 4 hops total, hop 3 (shady-agent) failed
# ---------------------------------------------------------------------------

r = client.get(f"/chain/{chain_id}")
print("\nGET /chain/{chain_id} ->", r.status_code)
chain = r.json()
print(json.dumps(chain, indent=2))
assert chain["total_hops"] == 4
assert chain["all_passed"] is False  # hop 3 failed
assert [h["hop_number"] for h in chain["hops"]] == [1, 2, 3, 4]
assert chain["hops"][2]["passed"] is False
assert chain["hops"][2]["from_agent"] == "shady-agent"

# ---------------------------------------------------------------------------
# 9. Hop numbering across a fresh chain starts at 1
# ---------------------------------------------------------------------------

chain_id_2 = "loan-test5678"
r = client.post("/envelopes", json={
    "chain_id": chain_id_2,
    "sender_id": "intake-agent",
    "sender_public_key": "intake-public-key-v1",
    "receiver_id": "risk-agent",
    "payload": {"applicant_name": "John Smith", "loan_amount": 5000},
    "contract": {"required_fields": ["applicant_name"], "on_fail": "block"},
})
new_envelope = r.json()
r = client.get(f"/chain/{chain_id_2}")
chain2 = r.json()
print("\nGET /chain/{chain_id_2} (fresh chain) -> total_hops:",
      chain2["total_hops"])
assert chain2["total_hops"] == 1
assert chain2["hops"][0]["hop_number"] == 1
assert chain2["all_passed"] is True

print("\nALL CHECKS PASSED")
