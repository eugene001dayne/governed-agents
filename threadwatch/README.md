# ThreadWatch

Pipeline health monitoring service for the Governed Agents hackathon project.

Ingests signals from the other four governance tools (Iron-Thread, AgentID,
ChainThread, PolicyThread) and computes an overall pipeline health score
from 0.0 to 1.0.

## Files

- `main.py` — the complete FastAPI app (single file)
- `requirements.txt`
- `render.yaml` — Render deploy config (service name: `threadwatch-hackathon`)
- `.env.example` — copy to `.env` and fill in for local runs
- `test_local.py` — offline test harness, mocks Supabase to verify scoring
  logic across all four tools and all three health statuses

No Supabase schema changes needed — `tw_signals` is used exactly as
originally created.

## Setup (local, PowerShell)

```powershell
cd threadwatch
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
# edit .env and paste your Supabase URL + anon key (same project as the other four)
python -m uvicorn main:app --reload
```

Run the offline sanity check (no Supabase needed):

```powershell
python test_local.py
```

## IMPORTANT: signal payload contract

ThreadWatch does not query the other services' tables directly — it only
looks at what gets POSTed to `/signals/{tool}`. Each governance service (or
the agent calling it) is responsible for posting a signal after every
check, with a `payload` shaped like this:

| Tool           | Required payload key | "Good" value    |
|----------------|-----------------------|-----------------|
| `iron-thread`  | `status`              | `"passed"`      |
| `agentid`      | `recommendation`      | `"ALLOW"`       |
| `chainthread`  | `status`              | `"delivered"`   |
| `policythread` | `passed`              | `true`          |

A signal missing the relevant key counts as not good — conservative, so
that an agent forgetting to report status doesn't inflate the health score.

### Example signal payloads

After Iron-Thread `/validate`:
```json
POST /signals/iron-thread
{
  "signal_type": "validation",
  "payload": { "status": "passed", "confidence_score": 1.0, "run_id": "..." },
  "chain_id": "loan-xxxx"
}
```

After AgentID `/trust/lookup`:
```json
POST /signals/agentid
{
  "signal_type": "trust_lookup",
  "payload": { "recommendation": "ALLOW", "reputation_score": 1.0 },
  "chain_id": "loan-xxxx"
}
```

After ChainThread `/envelopes`:
```json
POST /signals/chainthread
{
  "signal_type": "handoff",
  "payload": { "status": "delivered", "envelope_id": "..." },
  "chain_id": "loan-xxxx"
}
```

After PolicyThread `/evaluate`:
```json
POST /signals/policythread
{
  "signal_type": "evaluation",
  "payload": { "passed": true, "violation_count": 0 },
  "chain_id": "loan-xxxx"
}
```

## Scoring logic

For each of the four tools:
1. Fetch the last 20 signals (most recent first).
2. If zero signals exist for that tool, score `1.0` (no data is not degraded).
3. Otherwise, score = (count of "good" signals) / (count of signals, up to 20).

```
health_score = average(iron-thread, agentid, chainthread, policythread)

status:
  health_score >= 0.8  -> "healthy"
  health_score >= 0.5  -> "degraded"
  health_score <  0.5  -> "critical"
```

## Testing against your live Supabase (PowerShell)

### 1. Pipeline health with no signals yet

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8000/pipeline/health"
```

Expect `health_score: 1`, `status: healthy`, all four tool scores `1`.

### 2. Send a few real signals

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8000/signals/iron-thread" -Method Post -ContentType "application/json" -Body (@{
  signal_type = "validation"
  payload = @{ status = "passed"; confidence_score = 1.0 }
  chain_id = "loan-demo001"
} | ConvertTo-Json -Depth 5)

Invoke-RestMethod -Uri "http://127.0.0.1:8000/signals/agentid" -Method Post -ContentType "application/json" -Body (@{
  signal_type = "trust_lookup"
  payload = @{ recommendation = "ALLOW"; reputation_score = 1.0 }
  chain_id = "loan-demo001"
} | ConvertTo-Json -Depth 5)

Invoke-RestMethod -Uri "http://127.0.0.1:8000/signals/chainthread" -Method Post -ContentType "application/json" -Body (@{
  signal_type = "handoff"
  payload = @{ status = "delivered" }
  chain_id = "loan-demo001"
} | ConvertTo-Json -Depth 5)

Invoke-RestMethod -Uri "http://127.0.0.1:8000/signals/policythread" -Method Post -ContentType "application/json" -Body (@{
  signal_type = "evaluation"
  payload = @{ passed = $true; violation_count = 0 }
  chain_id = "loan-demo001"
} | ConvertTo-Json -Depth 5)
```

### 3. Check pipeline health again

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8000/pipeline/health"
```

Expect `health_score: 1`, `status: healthy`, all four tool scores `1`.

### 4. List signals

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8000/signals"
Invoke-RestMethod -Uri "http://127.0.0.1:8000/signals?tool=iron-thread"
Invoke-RestMethod -Uri "http://127.0.0.1:8000/signals?chain_id=loan-demo001"
```

### 5. Trigger a degraded/critical state (optional)

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8000/signals/policythread" -Method Post -ContentType "application/json" -Body (@{
  signal_type = "evaluation"
  payload = @{ passed = $false; violation_count = 2 }
  chain_id = "loan-demo002"
} | ConvertTo-Json -Depth 5)

Invoke-RestMethod -Uri "http://127.0.0.1:8000/pipeline/health"
```

## Deploy to Render

1. Push this folder to the `governed-agents` GitHub repo as the `threadwatch/` subfolder.
2. New Web Service on Render, Root Directory = `threadwatch`.
3. Build command: `pip install -r requirements.txt`
4. Start command: `python -m uvicorn main:app --host 0.0.0.0 --port $PORT`
5. Env vars: `SUPABASE_URL`, `SUPABASE_KEY`.

Verify: `GET https://<your-threadwatch-service>.onrender.com/health` -> `{"status": "ok"}`