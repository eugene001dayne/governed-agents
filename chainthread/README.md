# ChainThread

Handoff verification service for the Governed Agents hackathon project.

Wraps every agent-to-agent handoff in a signed envelope: verifies the
sender's identity via AgentID, checks the payload against a contract,
signs the envelope, and logs every hop in the pipeline chain.

## Files

- `main.py` — the complete FastAPI app (single file)
- `requirements.txt`
- `render.yaml` — Render deploy config (service name: `chainthread-hackathon`)
- `.env.example` — copy to `.env` and fill in for local runs
- `test_local.py` — offline test harness, mocks Supabase **and** the AgentID
  HTTP call so you can sanity-check the logic without live services

No Supabase schema changes needed — `ct_envelopes` and `ct_handoff_log` are
used exactly as originally created.

## Setup (local, PowerShell)

```powershell
cd chainthread
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
# edit .env: paste your Supabase URL + anon key (same project as before)
# AGENTID_URL defaults to your live AgentID Render URL — adjust if different
python -m uvicorn main:app --reload
```

Run the offline sanity check (no Supabase or AgentID needed):

```powershell
python test_local.py
```

## How `/envelopes` works

1. **Identity check** — calls AgentID `POST /trust/lookup` with
   `queried_agent = sender_id`, `min_reputation = 0.7`. If the
   recommendation isn't `ALLOW`, a violation is recorded.
   - If `AGENTID_URL` is missing or AgentID is unreachable, this **fails
     closed** (treated as BLOCK) — an envelope is never silently trusted.
2. **Contract check** — if a `contract.required_fields` list is provided,
   every field must exist in `payload`. Missing fields become violations.
   If no `contract` is sent, this check always passes.
3. **Status** — `delivered` only if both checks pass; otherwise `blocked`.
   A signature is computed and the envelope is stored either way (blocked
   envelopes are still useful for the audit trail).
4. **Signature** — `sha256(sender_id + receiver_id + json.dumps(payload, sort_keys=True) + chain_id)`
5. **Hop logging** — every call appends a row to `ct_handoff_log` with an
   auto-incrementing `hop_number` per `chain_id` (starts at 1).

## Testing against your live services (PowerShell)

Make sure AgentID is live (Brief 2) — ChainThread calls it for every
`/envelopes` request.

### 1. Successful handoff: intake-agent → risk-agent

```powershell
$envelope = @{
  chain_id = "loan-demo001"
  sender_id = "intake-agent"
  sender_public_key = "intake-public-key-v1"
  receiver_id = "risk-agent"
  payload = @{
    applicant_name = "Jane Doe"
    loan_amount    = 25000
    annual_income  = 85000
    loan_purpose   = "Home renovation"
  }
  contract = @{
    required_fields = @("applicant_name","loan_amount","annual_income","loan_purpose")
    on_fail = "block"
  }
} | ConvertTo-Json -Depth 5

Invoke-RestMethod -Uri "http://127.0.0.1:8000/envelopes" -Method Post -Body $envelope -ContentType "application/json"
```

Expect `status: delivered`, `contract_passed: True`, a `signature`, and an
`id` (the envelope ID — save it).

### 2. Get the envelope back

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8000/envelopes/<envelope_id>"
```

### 3. Check the chain

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8000/chain/loan-demo001"
```

Expect `total_hops: 1`, `all_passed: True`.

### 4. Trigger a blocked handoff (missing contract field)

```powershell
$bad = @{
  chain_id = "loan-demo001"
  sender_id = "risk-agent"
  sender_public_key = "risk-public-key-v1"
  receiver_id = "compliance-agent"
  payload = @{ applicant_name = "Jane Doe"; risk_level = "low" }
  contract = @{ required_fields = @("applicant_name","risk_level","risk_score"); on_fail = "block" }
} | ConvertTo-Json -Depth 5

Invoke-RestMethod -Uri "http://127.0.0.1:8000/envelopes" -Method Post -Body $bad -ContentType "application/json"
```

Expect `status: blocked`, `violations` containing `"Missing required field: risk_score"`.

## Deploy to Render

1. Push this folder to the `governed-agents` GitHub repo as the `chainthread/` subfolder.
2. New Web Service on Render, Root Directory = `chainthread`.
3. Build command: `pip install -r requirements.txt`
4. Start command: `python -m uvicorn main:app --host 0.0.0.0 --port $PORT`
5. Env vars: `SUPABASE_URL`, `SUPABASE_KEY`, `AGENTID_URL` (your live AgentID URL).

Verify: `GET https://<your-chainthread-service>.onrender.com/health` → `{"status": "ok"}`