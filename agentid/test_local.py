"""
Local test harness for AgentID — mocks Supabase so we can exercise
/agents, /agents/{id}/verify, /trust/lookup, and /agents/{id}/reputation
end-to-end without a real Supabase project.

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
    "aid_agents": [],
    "aid_trust_lookups": [],
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


def fake_sb_update(table, params, payload):
    rows = FAKE_DB[table]
    matched = []
    for r in rows:
        ok = True
        for key, val in params.items():
            if val.startswith("eq."):
                target = val[3:]
                if str(r.get(key)) != target:
                    ok = False
        if ok:
            r.update(payload)
            matched.append(r)
    if not matched:
        from fastapi import HTTPException
        raise HTTPException(
            status_code=404, detail=f"No row updated in {table}")
    return matched[0]


main.sb_select = fake_sb_select
main.sb_insert = fake_sb_insert
main.sb_update = fake_sb_update

client = TestClient(main.app)

# ---------------------------------------------------------------------------
# 1. Root / health
# ---------------------------------------------------------------------------

r = client.get("/")
print("GET /            ->", r.status_code, r.json())

r = client.get("/health")
print("GET /health      ->", r.status_code, r.json())

# ---------------------------------------------------------------------------
# 2. Register an agent (intake-agent)
# ---------------------------------------------------------------------------

r = client.post("/agents", json={
    "agent_id": "intake-agent",
    "agent_name": "Intake Agent",
    "public_key": "intake-public-key-v1",
})
print("\nPOST /agents (register) ->", r.status_code)
agent = r.json()
print(json.dumps(agent, indent=2))
assert agent["reputation_score"] == 1.0
assert agent["active"] is True
credential_hash = agent["credential_hash"]

# ---------------------------------------------------------------------------
# 3. Re-register same agent_id -> idempotent, same record
# ---------------------------------------------------------------------------

r = client.post("/agents", json={
    "agent_id": "intake-agent",
    "agent_name": "Intake Agent",
    "public_key": "intake-public-key-v1",
})
print("\nPOST /agents (re-register, idempotent) ->", r.status_code)
agent2 = r.json()
assert agent2["id"] == agent["id"]
print("  same record id:", agent2["id"] == agent["id"])

# ---------------------------------------------------------------------------
# 4. Verify credential — correct public key
# ---------------------------------------------------------------------------

r = client.post("/agents/intake-agent/verify",
                json={"public_key": "intake-public-key-v1"})
print("\nPOST /agents/intake-agent/verify (correct key) ->", r.status_code)
print(json.dumps(r.json(), indent=2))
assert r.json()["verified"] is True

# ---------------------------------------------------------------------------
# 5. Verify credential — wrong public key
# ---------------------------------------------------------------------------

r = client.post("/agents/intake-agent/verify",
                json={"public_key": "wrong-key"})
print("\nPOST /agents/intake-agent/verify (wrong key) ->", r.status_code)
print(json.dumps(r.json(), indent=2))
assert r.json()["verified"] is False

# ---------------------------------------------------------------------------
# 6. Trust lookup — known agent, reputation 1.0 >= 0.5 -> ALLOW
# ---------------------------------------------------------------------------

r = client.post("/trust/lookup", json={
    "querying_agent": "intake-agent",
    "queried_agent": "intake-agent",
    "min_reputation": 0.5,
})
print("\nPOST /trust/lookup (known agent, min 0.5) ->", r.status_code)
print(json.dumps(r.json(), indent=2))
assert r.json()["trusted"] is True
assert r.json()["recommendation"] == "ALLOW"

# ---------------------------------------------------------------------------
# 7. Trust lookup — unknown agent -> BLOCK
# ---------------------------------------------------------------------------

r = client.post("/trust/lookup", json={
    "querying_agent": "intake-agent",
    "queried_agent": "ghost-agent",
    "min_reputation": 0.5,
})
print("\nPOST /trust/lookup (unknown agent) ->", r.status_code)
print(json.dumps(r.json(), indent=2))
assert r.json()["trusted"] is False
assert r.json()["recommendation"] == "BLOCK"

# ---------------------------------------------------------------------------
# 8. Reputation update — successful interaction -> stays 1.0
# ---------------------------------------------------------------------------

r = client.patch("/agents/intake-agent/reputation", json={
    "interaction_success": True,
    "violation": False,
    "pii_incident": False,
})
print("\nPATCH /agents/intake-agent/reputation (success) ->", r.status_code)
print(json.dumps(r.json(), indent=2))
assert r.json()["reputation_score"] == 1.0
assert r.json()["total_interactions"] == 1
assert r.json()["successful_interactions"] == 1

# ---------------------------------------------------------------------------
# 9. Reputation update — failed interaction + violation
#    total=2, successful=1 -> base=0.5, penalty=0.02 -> 0.48
# ---------------------------------------------------------------------------

r = client.patch("/agents/intake-agent/reputation", json={
    "interaction_success": False,
    "violation": True,
    "pii_incident": False,
})
print("\nPATCH /agents/intake-agent/reputation (failure + violation) ->", r.status_code)
print(json.dumps(r.json(), indent=2))
assert r.json()["total_interactions"] == 2
assert r.json()["successful_interactions"] == 1
assert abs(r.json()["reputation_score"] - 0.48) < 1e-9

# ---------------------------------------------------------------------------
# 10. List agents
# ---------------------------------------------------------------------------

r = client.get("/agents")
print("\nGET /agents ->", r.status_code)
agents = r.json()
print(f"  total agents: {len(agents)}")
assert len(agents) == 1

# ---------------------------------------------------------------------------
# 11. Trust lookups were logged
# ---------------------------------------------------------------------------

print(f"\n  trust lookups logged: {len(FAKE_DB['aid_trust_lookups'])}")
assert len(FAKE_DB["aid_trust_lookups"]) == 2

print("\nALL CHECKS PASSED")
