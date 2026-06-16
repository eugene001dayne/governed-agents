"""
Risk Agent — Governed Agents Hackathon
Build Chat 2, Agent 2 of 4

Receives verified handoffs from Intake Agent, verifies envelope and sender,
produces a risk assessment, validates it with Iron-Thread, and hands off
to Compliance Agent.
"""

import asyncio
import json
import logging
import os
from uuid import uuid4

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
IRON_THREAD_URL   = os.environ.get("IRON_THREAD_URL", "https://governed-agents-iron-thread.onrender.com").rstrip("/")
AGENTID_URL       = os.environ.get("AGENTID_URL",     "https://governed-agents-agentid.onrender.com").rstrip("/")
CHAINTHREAD_URL   = os.environ.get("CHAINTHREAD_URL", "https://governed-agents-chainthread.onrender.com").rstrip("/")
THREADWATCH_URL   = os.environ.get("THREADWATCH_URL", "https://governed-agents-threadwatch.onrender.com").rstrip("/")

TIMEOUT    = 45.0
AGENT_ID   = "risk-agent"
PUBLIC_KEY = "risk-public-key-v1"

# Risk assessment schema ID — registered on first startup, stored here
_RISK_SCHEMA_ID: str | None = None

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

# ── Schema registration ───────────────────────────────────────────────────────

async def ensure_risk_schema() -> str:
    """Register risk assessment schema in Iron-Thread if not already done."""
    global _RISK_SCHEMA_ID
    if _RISK_SCHEMA_ID:
        return _RISK_SCHEMA_ID
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.post(
            f"{IRON_THREAD_URL}/schemas",
            json={
                "name": "risk_assessment_v1",
                "schema_definition": {
                    "required": {
                        "risk_level": "string",
                        "risk_score": "number",
                        "key_factors": "array",
                        "recommendation": "string",
                        "reasoning": "string",
                    },
                    "optional": {},
                },
            },
        )
        resp.raise_for_status()
        _RISK_SCHEMA_ID = resp.json()["id"]
        logger.info(f"Risk schema registered: {_RISK_SCHEMA_ID}")
    return _RISK_SCHEMA_ID

# ── Tool input models ─────────────────────────────────────────────────────────
# Tool name = class name minus "Input", lowercased.
# FetchInput    -> "fetch"
# VerifyInput   -> "verify"
# AssessInput   -> "assess"
# ValidateInput -> "validate"
# HandoffInput  -> "handoff"

class FetchInput(BaseModel):
    """Fetch the handoff envelope from ChainThread and extract the loan application payload."""
    envelope_id: str

class VerifyInput(BaseModel):
    """Verify the sender (intake-agent) identity and reputation via AgentID trust lookup."""
    chain_id: str

class AssessInput(BaseModel):
    """Call Claude to produce a structured risk assessment from the loan application payload."""
    payload_json: str

class ValidateInput(BaseModel):
    """Validate the risk assessment JSON against the Iron-Thread risk schema."""
    raw_json: str
    chain_id: str

class HandoffInput(BaseModel):
    """Create a signed ChainThread handoff envelope to pass the risk assessment to Compliance Agent."""
    chain_id: str
    application_json: str
    risk_assessment_json: str

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
                "queried_agent": "intake-agent",
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


async def handle_assess(args: AssessInput) -> dict:
    prompt = (
        "You are a loan risk analyst. Based on this application, produce a structured risk assessment. "
        "Return ONLY valid JSON with these exact fields: "
        "risk_level (string: low/medium/high), "
        "risk_score (number 0.0-1.0), "
        "key_factors (array of strings), "
        "recommendation (string: approve/decline/escalate), "
        "reasoning (string explaining the decision). "
        f"Application data: {args.payload_json}"
    )
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-5-20250929",
                "max_tokens": 1000,
                "messages": [{"role": "user", "content": prompt}],
            },
        )
        resp.raise_for_status()
        raw = resp.json()["content"][0]["text"].strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        assessment = json.loads(raw.strip())
        return {"assessment": assessment, "raw_json": json.dumps(assessment)}


async def handle_validate(args: ValidateInput) -> dict:
    schema_id = await ensure_risk_schema()
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.post(
            f"{IRON_THREAD_URL}/validate",
            json={"schema_id": schema_id, "raw_output": args.raw_json, "agent_id": AGENT_ID},
        )
        resp.raise_for_status()
        result = resp.json()
    await post_signal(
        "iron-thread", "validation",
        {"status": result.get("status"), "confidence_score": result.get("confidence_score"), "run_id": result.get("run_id")},
        args.chain_id,
    )
    return result


async def handle_handoff(args: HandoffInput) -> dict:
    application = json.loads(args.application_json)
    risk_assessment = json.loads(args.risk_assessment_json)
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.post(
            f"{CHAINTHREAD_URL}/envelopes",
            json={
                "chain_id": args.chain_id,
                "sender_id": AGENT_ID,
                "sender_public_key": PUBLIC_KEY,
                "receiver_id": "compliance-agent",
                "payload": {
                    "application": application,
                    "risk_assessment": risk_assessment,
                },
                "contract": {
                    "required_fields": ["application", "risk_assessment"],
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
    (FetchInput,    handle_fetch),
    (VerifyInput,   handle_verify),
    (AssessInput,   handle_assess),
    (ValidateInput, handle_validate),
    (HandoffInput,  handle_handoff),
]

# ── System prompt ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are the Risk Agent in a governed loan review pipeline.

You ONLY respond when you receive a message that contains both "Envelope ID:" and "Chain ID:". If a message does not contain both of these, ignore it silently — do not reply.

When you receive a message with an Envelope ID and Chain ID, extract those two values and follow these steps IN ORDER:

STEP 1: Call the "fetch" tool with the envelope_id to retrieve the loan application payload.

STEP 2: Call the "verify" tool with the chain_id to verify the sender (intake-agent) identity.
- If recommendation is "BLOCK": send "⚠️ Risk Agent: Sender identity check failed. Rejecting handoff from intake-agent." and stop.

STEP 3: Call the "assess" tool with payload_json (the payload_json string from step 1) to produce a risk assessment.

STEP 4: Call the "validate" tool with raw_json (the raw_json from step 3) and the chain_id to validate the risk assessment structure.

STEP 5: Call the "handoff" tool with:
- chain_id: the chain ID from the message
- application_json: the payload_json from step 1
- risk_assessment_json: the raw_json from step 3
If status is "blocked": send "⚠️ Risk Agent: Handoff to Compliance Agent blocked. {violations}" and stop.

STEP 6: Send this message to the room (fill in real values):

@Compliance Agent — Risk assessment complete ✓

Risk level: {risk_level}
Risk score: {risk_score}
Recommendation: {recommendation}
Chain ID: {chain_id}
Envelope ID: {id from handoff result}

Governance checks:
✓ Sender identity verified (intake-agent reputation: {reputation_score})
✓ Output structure validated
✓ Handoff envelope signed and delivered

Please run compliance check.

Never skip steps. Never fabricate values. Always use real values from tool results.
"""

# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    agent_id_band, api_key_band = load_agent_config("risk_agent")

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

    logger.info("Risk Agent is running. Press Ctrl+C to stop.")
    await agent.run()


if __name__ == "__main__":
    asyncio.run(main())