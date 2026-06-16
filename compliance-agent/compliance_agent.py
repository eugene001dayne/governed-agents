"""
Compliance Agent — Governed Agents Hackathon
Build Chat 2, Agent 3 of 4

Receives verified handoffs from Risk Agent, verifies envelope and sender,
runs PolicyThread compliance check, and hands off to Decision Agent.
"""

import asyncio
import json
import logging
import os

import httpx
from dotenv import load_dotenv
from pydantic import BaseModel
from band import Agent
from band.adapters import AnthropicAdapter
from band.config import load_agent_config

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
IRON_THREAD_URL   = os.environ.get("IRON_THREAD_URL",   "https://governed-agents-iron-thread.onrender.com").rstrip("/")
AGENTID_URL       = os.environ.get("AGENTID_URL",       "https://governed-agents-agentid.onrender.com").rstrip("/")
CHAINTHREAD_URL   = os.environ.get("CHAINTHREAD_URL",   "https://governed-agents-chainthread.onrender.com").rstrip("/")
POLICYTHREAD_URL  = os.environ.get("POLICYTHREAD_URL",  "https://governed-agents-policythread.onrender.com").rstrip("/")
THREADWATCH_URL   = os.environ.get("THREADWATCH_URL",   "https://governed-agents-threadwatch.onrender.com").rstrip("/")

TIMEOUT    = 45.0
AGENT_ID   = "compliance-agent"
PUBLIC_KEY = "compliance-public-key-v1"

# ── ThreadWatch helper ────────────────────────────────────────────────────────

async def post_signal(tool: str, signal_type: str, payload: dict, chain_id: str) -> None:
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            await client.post(
                f"{THREADWATCH_URL}/signals/{tool}",
                json={"signal_type": signal_type, "payload": payload, "chain_id": chain_id},
            )
    except Exception as e:
        logger.warning(f"ThreadWatch {tool} signal failed (non-fatal): {e}")

# ── Tool input models ─────────────────────────────────────────────────────────
# Tool name = class name minus "Input", lowercased.
# FetchInput  -> "fetch"
# VerifyInput -> "verify"
# CheckInput  -> "check"
# HandoffInput -> "handoff"

class FetchInput(BaseModel):
    """Fetch the handoff envelope from ChainThread and extract the full payload including application and risk assessment."""
    envelope_id: str

class VerifyInput(BaseModel):
    """Verify the sender (risk-agent) identity and reputation via AgentID trust lookup."""
    chain_id: str

class CheckInput(BaseModel):
    """Run PolicyThread compliance evaluation on the full payload. Returns passed (true/false) and violations list."""
    chain_id: str
    payload_json: str

class HandoffInput(BaseModel):
    """Create a signed ChainThread handoff envelope to pass everything to Decision Agent."""
    chain_id: str
    application_json: str
    risk_assessment_json: str
    compliance_result_json: str

# ── Handlers ──────────────────────────────────────────────────────────────────

async def handle_fetch(args: FetchInput) -> dict:
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.get(f"{CHAINTHREAD_URL}/envelopes/{args.envelope_id}")
        resp.raise_for_status()
        result = resp.json()
    payload = result.get("payload", {})
    return {"envelope": result, "payload": payload, "payload_json": json.dumps(payload)}


async def handle_verify(args: VerifyInput) -> dict:
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.post(
            f"{AGENTID_URL}/trust/lookup",
            json={
                "querying_agent": AGENT_ID,
                "queried_agent": "risk-agent",
                "min_reputation": 0.7,
            },
        )
        resp.raise_for_status()
        result = resp.json()
    await post_signal(
        "agentid", "trust_lookup",
        {"recommendation": result.get("recommendation"), "reputation_score": result.get("reputation_score")},
        args.chain_id,
    )
    return result


async def handle_check(args: CheckInput) -> dict:
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.post(
            f"{POLICYTHREAD_URL}/evaluate",
            json={
                "agent_id": AGENT_ID,
                "chain_id": args.chain_id,
                "user_input": "Loan application risk assessment review",
                "ai_output": args.payload_json,
            },
        )
        resp.raise_for_status()
        result = resp.json()

    violations = result.get("violations", [])
    await post_signal(
        "policythread", "evaluation",
        {"passed": result.get("passed"), "violation_count": len(violations)},
        args.chain_id,
    )
    return result


async def handle_handoff(args: HandoffInput) -> dict:
    application       = json.loads(args.application_json)
    risk_assessment   = json.loads(args.risk_assessment_json)
    compliance_result = json.loads(args.compliance_result_json)

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.post(
            f"{CHAINTHREAD_URL}/envelopes",
            json={
                "chain_id": args.chain_id,
                "sender_id": AGENT_ID,
                "sender_public_key": PUBLIC_KEY,
                "receiver_id": "decision-agent",
                "payload": {
                    "application": application,
                    "risk_assessment": risk_assessment,
                    "compliance_result": compliance_result,
                },
                "contract": {
                    "required_fields": ["application", "risk_assessment", "compliance_result"],
                    "on_fail": "block",
                },
            },
        )
        resp.raise_for_status()
        result = resp.json()

    await post_signal(
        "chainthread", "handoff",
        {"status": result.get("status"), "envelope_id": result.get("id")},
        args.chain_id,
    )
    return result

# ── Tools list ────────────────────────────────────────────────────────────────

GOVERNANCE_TOOLS = [
    (FetchInput,   handle_fetch),
    (VerifyInput,  handle_verify),
    (CheckInput,   handle_check),
    (HandoffInput, handle_handoff),
]

# ── System prompt ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are the Compliance Agent in a governed loan review pipeline.

You ONLY respond when you receive a message that contains both "Envelope ID:" and "Chain ID:". If a message does not contain both of these, ignore it silently — do not reply.

When you receive a message with an Envelope ID and Chain ID, extract those two values and follow these steps IN ORDER:

STEP 1: Call the "fetch" tool with the envelope_id to retrieve the full payload (application + risk assessment).

STEP 2: Call the "verify" tool with the chain_id to verify the sender (risk-agent) identity.
- If recommendation is "BLOCK": send "⚠️ Compliance Agent: Sender identity check failed. Rejecting handoff from risk-agent." and stop.

STEP 3: Call the "check" tool with the chain_id and payload_json (the payload_json string from step 1) to run PolicyThread compliance evaluation.

STEP 4: Call the "handoff" tool with:
- chain_id: the chain ID from the message
- application_json: json.dumps of payload["application"] from step 1
- risk_assessment_json: json.dumps of payload["risk_assessment"] from step 1
- compliance_result_json: json.dumps of the full result from step 3
If status is "blocked": send "⚠️ Compliance Agent: Handoff to Decision Agent blocked. {violations}" and stop.

STEP 5: Send this message to the room (fill in real values):

@Decision Agent — Compliance check complete ✓

Policies checked: {number of active policies evaluated}
Result: {PASSED or VIOLATED}
Violations: {violation_count}
Chain ID: {chain_id}
Envelope ID: {id from handoff result}

Governance checks:
✓ Sender identity verified (risk-agent reputation: {reputation_score})
✓ {violation_count} policy violations found
✓ Handoff envelope signed and delivered

Please produce final decision.

Never skip steps. Never fabricate values. Always use real values from tool results.
"""

# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    agent_id_band, api_key_band = load_agent_config("compliance_agent")

    adapter = AnthropicAdapter(
        model="claude-sonnet-4-5-20250929",
        system_prompt=SYSTEM_PROMPT,
        max_tokens=4096,
        additional_tools=GOVERNANCE_TOOLS,
    )

    agent = Agent.create(
        adapter=adapter,
        agent_id=agent_id_band,
        api_key=api_key_band,
    )

    logger.info("Compliance Agent is running. Press Ctrl+C to stop.")
    await agent.run()


if __name__ == "__main__":
    asyncio.run(main())