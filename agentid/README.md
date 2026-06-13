# AgentID

Agent identity & trust governance service for the Governed Agents hackathon project.

Issues a SHA-256 credential to every registered agent, verifies credentials,
makes ALLOW/BLOCK trust decisions based on credential validity + reputation,
and tracks reputation over time.

## Files

- `main.py` — the complete FastAPI app (single file)
- `requirements.txt`
- `render.yaml` — Render deploy config (service name: `agentid-hackathon`)
- `.env.example` — copy to `.env` and fill in for local runs
- `test_local.py` — offline test harness, mocks Supabase so you can sanity-check
  the logic without a live database

## ⚠️ One-time Supabase change needed first

The reputation formula needs two extra columns on `aid_agents` to track a
running success ratio. Your `aid_agents` table was created from the original
brief without them, so run this once in the Supabase **SQL Editor** before
testing `/agents` or `/agents/{id}/reputation`:

```sql
ALTER TABLE aid_agents ADD COLUMN IF NOT EXISTS total_interactions INTEGER DEFAULT 0;
ALTER TABLE aid_agents ADD COLUMN IF NOT EXISTS successful_interactions INTEGER DEFAULT 0;
```

Everything else in `aid_agents` / `aid_trust_lookups` is used exactly as
originally created.

## Setup (local, PowerShell)

```powershell
cd agentid
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
# edit .env and paste your Supabase URL + anon key (same project as Iron-Thread)
python -m uvicorn main:app --reload
```

Run the offline sanity check (no Supabase needed):

```powershell
python test_local.py
```

## Credential logic

```
credential_hash = sha256(json.dumps(
    {"agent_id": agent_id, "public_key": public_key, "version": "AgentID-v1.0"},
    sort_keys=True
))
```

- `POST /agents` issues this hash on registration and stores it.
- `POST /agents/{agent_id}/verify` recomputes it from the supplied `public_key`
  and compares — `verified` is only `true` if the hash matches **and** the
  agent is `active`.
- Re-registering the same `agent_id` is idempotent — it returns the existing
  record rather than erroring, since agents may call `/agents` on every restart.

## Trust lookup logic

`POST /trust/lookup` checks (in order):
1. Is `queried_agent` registered? If not → `BLOCK`.
2. Is it `active`? If not → `BLOCK`.
3. Is `reputation_score >= min_reputation`? If not → `BLOCK`. Otherwise → `ALLOW`.

Every lookup — including unknown agents — is logged to `aid_trust_lookups`.

## Reputation update logic

`PATCH /agents/{agent_id}/reputation`:

```
total_interactions += 1
successful_interactions += 1 if interaction_success else +0
base = successful_interactions / total_interactions
penalty = 0.02 if violation else 0, plus 0.05 if pii_incident
reputation_score = clamp(base - penalty, 0.0, 1.0)
```

## Testing against your live Supabase (PowerShell)

### 1. Register the four pipeline agents

```powershell
$agents = @(
  @{ agent_id = "intake-agent";     agent_name = "Intake Agent";     public_key = "intake-public-key-v1" },
  @{ agent_id = "risk-agent";       agent_name = "Risk Agent";       public_key = "risk-public-key-v1" },
  @{ agent_id = "compliance-agent"; agent_name = "Compliance Agent"; public_key = "compliance-public-key-v1" },
  @{ agent_id = "decision-agent";   agent_name = "Decision Agent";   public_key = "decision-public-key-v1" }
)

foreach ($a in $agents) {
  $body = $a | ConvertTo-Json
  Invoke-RestMethod -Uri "http://127.0.0.1:8000/agents" -Method Post -Body $body -ContentType "application/json"
}
```

You should see four records come back, each with a `credential_hash`,
`reputation_score: 1.0`, `active: true`.

### 2. Verify a credential

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8000/agents/intake-agent/verify" -Method Post `
  -Body (@{ public_key = "intake-public-key-v1" } | ConvertTo-Json) -ContentType "application/json"
```

Expect `verified: True`.

### 3. Trust lookup

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8000/trust/lookup" -Method Post `
  -Body (@{ querying_agent = "intake-agent"; queried_agent = "intake-agent"; min_reputation = 0.5 } | ConvertTo-Json) `
  -ContentType "application/json"
```

Expect `trusted: True`, `recommendation: ALLOW`.

### 4. Reputation update

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8000/agents/intake-agent/reputation" -Method Patch `
  -Body (@{ interaction_success = $true; violation = $false; pii_incident = $false } | ConvertTo-Json) `
  -ContentType "application/json"
```

Expect `reputation_score: 1`, `total_interactions: 1`, `successful_interactions: 1`.

### 5. List agents

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8000/agents"
```

## Deploy to Render

1. Push this folder to the `governed-agents` GitHub repo as the `agentid/` subfolder.
2. New Web Service on Render, Root Directory = `agentid`.
3. Build command: `pip install -r requirements.txt`
4. Start command: `python -m uvicorn main:app --host 0.0.0.0 --port $PORT`
5. Env vars: `SUPABASE_URL`, `SUPABASE_KEY` (same project as Iron-Thread).

Verify: `GET https://<your-agentid-service>.onrender.com/health` → `{"status": "ok"}`