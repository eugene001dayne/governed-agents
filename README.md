# Governed Agents

The governance infrastructure layer for multi-agent AI systems.
Built for the Band of Agents Hackathon — Track 3: Regulated and High-Stakes Workflows.

**Builder:** Eugene Mawuli Attigah / BiteLance — Accra, Ghana

## The Problem

AI agents are being deployed into enterprise workflows right now. But they operate with no governance layer. They pass data to each other blindly. Nobody verifies the handoff happened correctly. Nobody checks that outputs follow organizational rules. Nobody confirms which agent is actually sending the data. There is no audit trail.

## The Solution

Four governance primitives. Any workflow. Any agent framework.

**Structure — Iron-Thread**
Agents return structured JSON. Iron-Thread validates it matches the expected schema before it goes anywhere. SHA-256 hash chain on every validation run. Tamper-evident and provable to a regulator.

**Handoff — ChainThread**
When one agent passes work to another, ChainThread wraps it, signs it, and verifies it on receipt. The receiver knows exactly what was sent, who sent it, and that it was not tampered with.

**Compliance — PolicyThread**
Every output is evaluated against organizational rules. Keyword checks plus semantic evaluation via Claude API. Returns pass or fail with plain-English violation reasons.

**Identity — AgentID**
Every agent has a cryptographic credential and a reputation score. Trust is earned, not assumed. ALLOW or BLOCK on every handoff before data moves.

**Monitoring — ThreadWatch**
Ingests signals from all four tools and computes a real-time pipeline health score from 0.0 to 1.0.


## Live System

### Governance Services (all live on Render)
- Iron-Thread: https://governed-agents-iron-thread.onrender.com
- AgentID: https://governed-agents-agentid.onrender.com
- ChainThread: https://governed-agents-chainthread.onrender.com
- PolicyThread: https://governed-agents-policythread.onrender.com
- ThreadWatch: https://governed-agents-threadwatch.onrender.com

### Org Dashboard (always live)
https://gooverne-agents.lovable.app/

### Dashboard Repository
https://github.com/eugene001dayne/agent-oversight-console

---

## The Demo Workflow

A loan review pipeline running through Band. Four agents coordinate in real time:

1. **Intake Agent** — receives a loan application, extracts structured JSON, calls Iron-Thread to validate the structure, calls AgentID to verify its own identity, calls ChainThread to create a signed handoff envelope, passes to Risk Agent
2. **Risk Agent** — verifies the handoff, calls AgentID to verify the sender, produces a structured risk assessment, validates via Iron-Thread, hands off to Compliance Agent
3. **Compliance Agent** — verifies the handoff, calls PolicyThread to evaluate all outputs against active compliance policies, hands off to Decision Agent
4. **Decision Agent** — receives the complete governed pipeline output, produces the final recommendation, validates via Iron-Thread, posts a full governance audit summary

Every step is recorded in Supabase. The governance console shows every event in real time — validation runs, handoff envelopes, compliance evaluations, agent identities, and the live signal feed.

A confirmed end-to-end run exists with chain ID: loan-f01cfb31

---

## Repository Structure
governed-agents/

├── .github/workflows/keep-warm.yml   ← pings all 5 Render services every 10 min

├── iron-thread/main.py               ← FastAPI, output validation + hash chain

├── agentid/main.py                   ← FastAPI, credential issuance + trust lookup

├── chainthread/main.py               ← FastAPI, signed handoff envelopes

├── policythread/main.py              ← FastAPI, compliance evaluation

├── threadwatch/main.py               ← FastAPI, pipeline health score

├── intake-agent/intake_agent.py      ← Band agent, AnthropicAdapter

├── risk-agent/risk_agent.py          ← Band agent, AnthropicAdapter

├── compliance-agent/compliance_agent.py

└── decision-agent/decision_agent.py

Second repo (dashboard):
https://github.com/eugene001dayne/agent-oversight-console

---

## Stack

- **Band SDK** with AnthropicAdapter for all four agents
- **FastAPI** for all five governance services
- **Supabase** — one shared project, all tables prefixed by service
- **Render** — five web services + keep-alive GitHub Actions workflow
- **Lovable** — org dashboard, TanStack Start, hosted at lovable.app
- **Anthropic Claude API** for PolicyThread semantic evaluation and agent reasoning

---

## Running the Agents Locally

The five governance services are always live on Render. The four Band agents run locally.
cd intake-agent

uv run python intake_agent.py
cd risk-agent

uv run python risk_agent.py
cd compliance-agent

uv run python compliance_agent.py
cd decision-agent

uv run python decision_agent.py

Requires: Python 3.11, uv, ANTHROPIC_API_KEY in each agent's .env file.

---

## Note on API Credits

During the final demo recording window our Anthropic API credits ran out unexpectedly. The governance services continued working (PolicyThread falls back gracefully when the API is unavailable). The agents themselves could not process new pipeline runs during this window. The dashboard and all confirmed pipeline data from chain ID loan-f01cfb31 remained live and visible throughout.

---

## Business Case

The EU AI Act designates loan decisions as high-risk AI requiring ongoing behavioral monitoring by August 2026. Every financial institution deploying AI for credit decisions needs exactly this — a governed, auditable, multi-agent pipeline with tamper-evident records provable to a regulator. Band provides the coordination layer. Thread Suite provides the governance layer. Together they are the infrastructure stack for trustworthy enterprise AI.
