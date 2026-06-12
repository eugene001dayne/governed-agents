# Iron-Thread

Structure-validation governance service for the Governed Agents hackathon project.

Validates AI agent outputs against a registered JSON schema, scores confidence,
and maintains a tamper-evident SHA-256 hash chain per schema.

## Files

- `main.py` — the complete FastAPI app (single file)
- `requirements.txt`
- `render.yaml` — Render deploy config (service name: `iron-thread-hackathon`)
- `.env.example` — copy to `.env` and fill in for local runs
- `test_local.py` — optional local test harness, mocks Supabase so you can sanity-check
  the validation logic and hash chain without a live database

## Setup (local, PowerShell)

```powershell
cd iron-thread
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
# edit .env and paste your Supabase URL + anon key
python -m uvicorn main:app --reload
```

Run the offline sanity check (no Supabase needed):

```powershell
python test_local.py
```

## Schema definition format

`schema_definition` (stored in `it_schemas.schema_definition`) has two optional
sections, `required` and `optional`, each mapping field names to a type tag:

| Type tag   | Matches                                  |
|------------|-------------------------------------------|
| `string`   | `str`                                      |
| `number`   | `int` or `float` (not `bool`)              |
| `integer`  | `int` (not `bool`)                         |
| `boolean`  | `bool`                                     |
| `array`    | `list`                                     |
| `object`   | `dict`                                     |
| `any`      | anything                                    |

Append `?` to any type (e.g. `number?`) to allow `null` as well.

### Example — register the loan application schema

This matches what the Intake Agent extracts (Brief 6):

```powershell
$body = @{
  name = "loan_application_v1"
  schema_definition = @{
    required = @{
      applicant_name = "string"
      loan_amount    = "number"
      annual_income  = "number"
      loan_purpose   = "string"
    }
    optional = @{
      credit_score = "number?"
    }
  }
} | ConvertTo-Json -Depth 5

Invoke-RestMethod -Uri "http://127.0.0.1:8000/schemas" -Method Post -Body $body -ContentType "application/json"
```

Copy the returned `id` — that's your `SCHEMA_ID` for the Intake Agent's `.env`.

### Validate an output

```powershell
$validate = @{
  schema_id = "PASTE_SCHEMA_ID_HERE"
  raw_output = '{"applicant_name":"Jane Doe","loan_amount":25000,"annual_income":85000,"loan_purpose":"Home renovation","credit_score":712}'
  agent_id = "intake-agent"
} | ConvertTo-Json

Invoke-RestMethod -Uri "http://127.0.0.1:8000/validate" -Method Post -Body $validate -ContentType "application/json"
```

Response:
```json
{
  "run_id": "uuid",
  "status": "passed",
  "confidence_score": 1.0,
  "errors": [],
  "run_hash": "sha256..."
}
```

### Get the hash chain

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8000/schemas/PASTE_SCHEMA_ID_HERE/chain"
```

## Validation / confidence rules

- Every field in `required` must be present **and** match its type tag, or the
  run is `"failed"` with `confidence_score = 0.0`.
- Every field in `optional` that is missing or mis-typed drops the confidence
  score by `0.1` (floor at `0.0`). Status stays `"passed"`.
- If `raw_output` isn't valid JSON (or isn't a JSON object), the run is
  `"failed"` with `confidence_score = 0.0`.

## Hash chain

Each run's `run_hash = sha256(agent_id + schema_id + raw_output + status + created_at + previous_hash)`.

- The first run for a schema uses `previous_hash = "GENESIS"`.
- Every subsequent run's `previous_hash` is the prior run's `run_hash`.
- `GET /schemas/{id}/chain` recomputes every hash and checks the links;
  `chain_verified` is `false` if anything has been tampered with.

## Deploy to Render

1. Push this folder to the `governed-agents` GitHub repo (e.g. as the `iron-thread/` subfolder).
2. Create a new Web Service on Render pointing at this folder, or use `render.yaml`.
3. Set env vars `SUPABASE_URL` and `SUPABASE_KEY` in the Render dashboard.
4. Push triggers auto-deploy.

Verify: `GET https://iron-thread-hackathon.onrender.com/health` → `{"status": "ok"}`