"""
Local test harness for ThreadWatch — mocks Supabase so we can exercise
/signals/{tool}, /pipeline/health, and /signals end-to-end without a real
Supabase project.

Run: python test_local.py
"""

import json
import uuid
from datetime import datetime, timedelta, timezone

import main
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# In-memory fake Supabase
# ---------------------------------------------------------------------------

FAKE_DB = {
    "tw_signals": [],
}

_clock = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _next_timestamp():
    global _clock
    _clock += timedelta(seconds=1)
    return _clock.isoformat()


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
    record.setdefault("recorded_at", _next_timestamp())
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
# 2. Pipeline health with ZERO signals -> all tools score 1.0
# ---------------------------------------------------------------------------

r = client.get("/pipeline/health")
print("\nGET /pipeline/health (no signals) ->", r.status_code)
result = r.json()
print(json.dumps(result, indent=2))
assert result["health_score"] == 1.0
assert result["status"] == "healthy"
assert all(v == 1.0 for v in result["tool_scores"].values())

# ---------------------------------------------------------------------------
# 3. Invalid tool name -> 400
# ---------------------------------------------------------------------------

r = client.post("/signals/not-a-real-tool",
                json={"signal_type": "x", "payload": {}})
print("\nPOST /signals/not-a-real-tool ->", r.status_code)
assert r.status_code == 400

# ---------------------------------------------------------------------------
# 4. Ingest iron-thread signals: 3 passed, 1 failed -> score 0.75
# ---------------------------------------------------------------------------

for status in ["passed", "passed", "passed", "failed"]:
    r = client.post("/signals/iron-thread", json={
        "signal_type": "validation",
        "payload": {"status": status, "confidence_score": 1.0 if status == "passed" else 0.0},
        "chain_id": "loan-test-1",
    })
    assert r.status_code == 200

# ---------------------------------------------------------------------------
# 5. Ingest agentid signals: 4 ALLOW, 1 BLOCK -> score 0.8
# ---------------------------------------------------------------------------

for rec in ["ALLOW", "ALLOW", "ALLOW", "ALLOW", "BLOCK"]:
    r = client.post("/signals/agentid", json={
        "signal_type": "trust_lookup",
        "payload": {"recommendation": rec},
        "chain_id": "loan-test-1",
    })
    assert r.status_code == 200

# ---------------------------------------------------------------------------
# 6. Ingest chainthread signals: all delivered -> score 1.0
# ---------------------------------------------------------------------------

for _ in range(3):
    r = client.post("/signals/chainthread", json={
        "signal_type": "handoff",
        "payload": {"status": "delivered"},
        "chain_id": "loan-test-1",
    })
    assert r.status_code == 200

# ---------------------------------------------------------------------------
# 7. policythread: no signals yet -> should score 1.0 (no data)
# ---------------------------------------------------------------------------

print("\nIngested iron-thread (3/4 passed), agentid (4/5 ALLOW), chainthread (3/3 delivered)")
print("policythread: no signals yet")

# ---------------------------------------------------------------------------
# 8. Check pipeline health
#    iron-thread = 0.75, agentid = 0.8, chainthread = 1.0, policythread = 1.0
#    health_score = (0.75 + 0.8 + 1.0 + 1.0) / 4 = 0.8875
# ---------------------------------------------------------------------------

r = client.get("/pipeline/health")
print("\nGET /pipeline/health (after signals) ->", r.status_code)
result = r.json()
print(json.dumps(result, indent=2))
assert result["tool_scores"]["iron-thread"] == 0.75
assert result["tool_scores"]["agentid"] == 0.8
assert result["tool_scores"]["chainthread"] == 1.0
assert result["tool_scores"]["policythread"] == 1.0
assert abs(result["health_score"] - 0.8875) < 1e-9
assert result["status"] == "healthy"  # >= 0.8

# ---------------------------------------------------------------------------
# 9. Ingest policythread signals: 1 passed, 4 violated -> score 0.2
#    New health_score = (0.75 + 0.8 + 1.0 + 0.2) / 4 = 0.6875 -> degraded
# ---------------------------------------------------------------------------

for passed in [True, False, False, False, False]:
    r = client.post("/signals/policythread", json={
        "signal_type": "evaluation",
        "payload": {"passed": passed},
        "chain_id": "loan-test-2",
    })
    assert r.status_code == 200

r = client.get("/pipeline/health")
print("\nGET /pipeline/health (after policythread signals) ->", r.status_code)
result = r.json()
print(json.dumps(result, indent=2))
assert result["tool_scores"]["policythread"] == 0.2
assert abs(result["health_score"] - 0.6875) < 1e-9
assert result["status"] == "degraded"  # 0.5 <= x < 0.8

# ---------------------------------------------------------------------------
# 10. Add 16 more "failed" iron-thread signals. Combined with the earlier
#     3 passed + 1 failed, the last-20 window is now 3 passed + 17 failed.
#     iron-thread score becomes 3/20 = 0.15
#     New health_score = (0.15 + 0.8 + 1.0 + 0.2) / 4 = 0.5375 -> degraded
# ---------------------------------------------------------------------------

for _ in range(16):
    r = client.post("/signals/iron-thread", json={
        "signal_type": "validation",
        "payload": {"status": "failed"},
        "chain_id": "loan-test-3",
    })
    assert r.status_code == 200

r = client.get("/pipeline/health")
print("\nGET /pipeline/health (iron-thread now mostly failed in last 20) ->", r.status_code)
result = r.json()
print(json.dumps(result, indent=2))
assert result["tool_scores"]["iron-thread"] == 0.15
assert abs(result["health_score"] - 0.5375) < 1e-9
assert result["status"] == "degraded"  # 0.5 <= x < 0.8

# ---------------------------------------------------------------------------
# 11. Push it below 0.5 -> critical
#     Drop agentid score too: add 16 BLOCK signals. Combined with the
#     earlier 4 ALLOW + 1 BLOCK (5 total), there are now 21 signals.
#     The last-20 window drops the single oldest signal (an ALLOW),
#     leaving 3 ALLOW + 17 BLOCK -> agentid score = 3/20 = 0.15
#     New health_score = (0.15 + 0.15 + 1.0 + 0.2) / 4 = 0.375 -> critical
# ---------------------------------------------------------------------------

for _ in range(16):
    r = client.post("/signals/agentid", json={
        "signal_type": "trust_lookup",
        "payload": {"recommendation": "BLOCK"},
        "chain_id": "loan-test-3",
    })
    assert r.status_code == 200

r = client.get("/pipeline/health")
print("\nGET /pipeline/health (agentid now mostly BLOCK in last 20) ->", r.status_code)
result = r.json()
print(json.dumps(result, indent=2))
assert result["tool_scores"]["agentid"] == 0.15
assert abs(result["health_score"] - 0.375) < 1e-9
assert result["status"] == "critical"  # < 0.5

# ---------------------------------------------------------------------------
# 12. GET /signals — list all, list filtered by tool, list filtered by chain_id
# ---------------------------------------------------------------------------

r = client.get("/signals")
print("\nGET /signals ->", r.status_code,
      f"({len(r.json())} total, default limit 50)")
total_signals = len(FAKE_DB["tw_signals"])
print(f"  total signals ingested: {total_signals}")

r = client.get("/signals", params={"tool": "iron-thread"})
print("GET /signals?tool=iron-thread ->",
      r.status_code, f"({len(r.json())} signals)")
assert all(s["source_tool"] == "iron-thread" for s in r.json())

r = client.get("/signals", params={"chain_id": "loan-test-1"})
print("GET /signals?chain_id=loan-test-1 ->",
      r.status_code, f"({len(r.json())} signals)")
assert all(s["chain_id"] == "loan-test-1" for s in r.json())

r = client.get("/signals", params={"tool": "bad-tool"})
print("GET /signals?tool=bad-tool ->", r.status_code)
assert r.status_code == 400

print("\nALL CHECKS PASSED")
