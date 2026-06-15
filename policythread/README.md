# PolicyThread

Compliance evaluation service for the Governed Agents hackathon project.

Evaluates agent outputs against organizational policies. Two layers:
- **Deterministic** — `keyword_exclude`, `keyword_require`, `max_length` run
  instantly, no external calls.
- **Semantic** — `semantic` policies call the Claude API with a strict
  JSON-only prompt to judge compliance against a natural-language rule.

## Files

- `main.py` — the complete FastAPI app (single file)
- `requirements.txt`
- `render.yaml` — Render deploy config (service name: `policythread-hackathon`)
- `.env.example` — copy to `.env` and fill in for local runs
- `test_local.py` — offline test harness, mocks Supabase **and** the Claude
  API so you can sanity-check all condition types without a live database
  or API key

No Supabase schema changes needed — `pt_policies` and `pt_evaluations` are
used exactly as originally created.

## A note on `anthropic` vs `httpx`

The original brief listed the `anthropic` SDK package in requirements.txt.
This build calls `https://api.anthropic.com/v1/messages` directly via
`httpx` instead — consistent with every other service in this project (no
SDK wrappers, httpx everywhere) and one less dependency to install. The
`anthropic` package is **not** in `requirements.txt` and is not needed.

## Setup (local, PowerShell)

```powershell
cd policythread
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
# edit .env: paste your Supabase URL + anon key, and your Anthropic API key
python -m uvicorn main:app --reload
```

Run the offline sanity check (no Supabase or Claude API needed):

```powershell
python test_local.py
```

## How `/evaluate` works

1. Fetch all `active = true` policies from `pt_policies`.
2. For each policy, run the check matching its `condition.type`:
   - `keyword_exclude` — fails if any keyword (case-insensitive) appears in `ai_output`
   - `keyword_require` — fails if any required keyword is missing
   - `max_length` — fails if `len(ai_output) > condition.value`
   - `semantic` — calls Claude with `condition.rule`; fails if Claude says
     the output violates the rule
3. Collect all violations as `{policy_name, severity, reason}`.
4. `passed = true` only if there are zero violations.
5. Store the full evaluation (including `ai_output` and `violations`) in
   `pt_evaluations`.

### Semantic check fail-safe behavior

If `ANTHROPIC_API_KEY` is missing, the Claude API call fails, times out, or
returns unparseable output, the semantic check is **skipped and treated as
passed** — with the reason recorded (e.g. "Semantic check skipped: ...").
This avoids a misconfigured or temporarily-down API silently blocking every
agent output. The skip reason is visible in the evaluation record for audit.

`ANTHROPIC_TIMEOUT` defaults to 45 seconds (configurable via env var) —
generous enough for normal API latency.

## Loading the demo compliance policies (PowerShell)

These four policies match the loan-review narrative used across the other
services. Run each block once your server is up:

```powershell
# 1. No discriminatory language (keyword_exclude)
Invoke-RestMethod -Uri "http://127.0.0.1:8000/policies" -Method Post -ContentType "application/json" -Body (@{
  name = "No discriminatory language"
  description = "Output must not contain discriminatory or derogatory terms"
  condition = @{ type = "keyword_exclude"; keywords = @("derogatory_term_1","derogatory_term_2") }
  severity = "critical"
} | ConvertTo-Json -Depth 5)

# 2. Decisions must include reasoning (keyword_require)
Invoke-RestMethod -Uri "http://127.0.0.1:8000/policies" -Method Post -ContentType "application/json" -Body (@{
  name = "Decision must include reasoning"
  description = "Loan decisions must explain their reasoning"
  condition = @{ type = "keyword_require"; keywords = @("reasoning") }
  severity = "medium"
} | ConvertTo-Json -Depth 5)

# 3. Output length cap (max_length)
Invoke-RestMethod -Uri "http://127.0.0.1:8000/policies" -Method Post -ContentType "application/json" -Body (@{
  name = "Output length cap"
  description = "Agent outputs must stay under 2000 characters"
  condition = @{ type = "max_length"; value = 2000 }
  severity = "low"
} | ConvertTo-Json -Depth 5)

# 4. No discriminatory loan reasoning (semantic - calls Claude)
Invoke-RestMethod -Uri "http://127.0.0.1:8000/policies" -Method Post -ContentType "application/json" -Body (@{
  name = "No discriminatory loan reasoning"
  description = "Loan decisions must not be based on protected characteristics"
  condition = @{ type = "semantic"; rule = "The output must not recommend loan approval or denial based on race, gender, or religion" }
  severity = "critical"
} | ConvertTo-Json -Depth 5)
```

## Testing against your live services (PowerShell)

### 1. List policies

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8000/policies"
```

Expect 4 records.

### 2. Evaluate a clean decision

```powershell
$clean = @{
  agent_id = "decision-agent"
  chain_id = "loan-demo001"
  user_input = "Review this application"
  ai_output = '{"decision":"approved","reasoning":"Applicant has stable income and strong credit history."}'
} | ConvertTo-Json

Invoke-RestMethod -Uri "http://127.0.0.1:8000/evaluate" -Method Post -Body $clean -ContentType "application/json"
```

Expect `passed: True`, `violations: {}`.

### 3. Evaluate a decision that violates the semantic policy

This one calls Claude - give it up to 45 seconds on a cold start.

```powershell
$bad = @{
  agent_id = "decision-agent"
  chain_id = "loan-demo002"
  user_input = "Review this application"
  ai_output = '{"decision":"declined","reasoning":"Declined because the applicant practices a minority religion."}'
} | ConvertTo-Json

Invoke-RestMethod -Uri "http://127.0.0.1:8000/evaluate" -Method Post -Body $bad -ContentType "application/json"
```

Expect `passed: False`, with a violation for "No discriminatory loan reasoning".

### 4. Check evaluation history

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8000/evaluations"
Invoke-RestMethod -Uri "http://127.0.0.1:8000/evaluations?chain_id=loan-demo002"
```

## Deploy to Render

1. Push this folder to the `governed-agents` GitHub repo as the `policythread/` subfolder.
2. New Web Service on Render, Root Directory = `policythread`.
3. Build command: `pip install -r requirements.txt`
4. Start command: `python -m uvicorn main:app --host 0.0.0.0 --port $PORT`
5. Env vars: `SUPABASE_URL`, `SUPABASE_KEY`, `ANTHROPIC_API_KEY`.

Verify: `GET https://<your-policythread-service>.onrender.com/health` -> `{"status": "ok"}`