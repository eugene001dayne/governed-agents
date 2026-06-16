"""
Intake Agent — Governed Agents Hackathon
Build Chat 2, Agent 1 of 4
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
AGENT_ID   = "intake-agent"
PUBLIC_KEY = "intake-public-key-v1"
SCHEMA_ID  = "f62ce982-1dde-4ab0-b099-6fd0284b6447"

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
# Tool name = class name minus "Input" suffix, lowercased.
# ChainInput    -> "chain"
# ExtractInput  -> "extract"
# ValidateInput -> "validate"
# IdentityInput -> "identity"
# HandoffInput  -> "handoff"

class ChainInput(BaseModel):
    """Generate a unique chain ID for this loan pipeline run. Call this first."""

class ExtractInput(BaseModel):
    """Extract structured loan data from raw message text using Claude. Returns extracted dict and raw_json string."""
    message_text: str

class ValidateInput(BaseModel):
    """Validate extracted loan JSON against Iron-Thread schema. Returns status (passed/failed), confidence_score, errors."""
    raw_json: str
    chain_id: str

class IdentityInput(BaseModel):
    """Verify intake-agent identity via AgentID trust lookup. Returns recommendation (ALLOW/BLOCK) and reputation_score."""
    chain_id: str

class HandoffInput(BaseModel):
    """Create signed ChainThread handoff envelope to Risk Agent. Returns envelope id and status (delivered/blocked)."""
    chain_id: str
    payload_json: str

# ── Handlers ──────────────────────────────────────────────────────────────────

async def handle_chain(args: ChainInput) -> dict:
    return {"chain_id": f"loan-{uuid4().hex[:8]}"}


async def handle_extract(args: ExtractInput) -> dict:
    prompt = (
        "Extract structured loan application data from this text and return ONLY valid JSON "
        "with these exact fields: applicant_name (string), loan_amount (number), "
        "annual_income (number), loan_purpose (string), "
        "credit_score (number or null if not provided). "
        f"Text: {args.message_text}"
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
                "max_tokens": 500,
                "messages": [{"role": "user", "content": prompt}],
            },
        )
        resp.raise_for_status()
        raw = resp.json()["content"][0]["text"].strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        extracted = json.loads(raw.strip())
        return {"extracted": extracted, "raw_json": json.dumps(extracted)}


async def handle_validate(args: ValidateInput) -> dict:
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.post(
            f"{IRON_THREAD_URL}/validate",
            json={"schema_id": SCHEMA_ID, "raw_output": args.raw_json, "agent_id": AGENT_ID},
        )
        resp.raise_for_status()
        result = resp.json()
    await post_signal(
        "iron-thread", "validation",
        {"status": result.get("status"), "confidence_score": result.get("confidence_score"), "run_id": result.get("run_id")},
        args.chain_id,
    )
    return result


async def handle_identity(args: IdentityInput) -> dict:
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.post(
            f"{AGENTID_URL}/trust/lookup",
            json={"querying_agent": AGENT_ID, "queried_agent": AGENT_ID, "min_reputation": 0.5},
        )
        resp.raise_for_status()
        result = resp.json()
    await post_signal(
        "agentid", "trust_lookup",
        {"recommendation": result.get("recommendation"), "reputation_score": result.get("reputation_score")},
        args.chain_id,
    )
    return result


async def handle_handoff(args: HandoffInput) -> dict:
    payload = json.loads(args.payload_json)
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.post(
            f"{CHAINTHREAD_URL}/envelopes",
            json={
                "chain_id": args.chain_id,
                "sender_id": AGENT_ID,
                "sender_public_key": PUBLIC_KEY,
                "receiver_id": "risk-agent",
                "payload": payload,
                "contract": {
                    "required_fields": ["applicant_name", "loan_amount", "annual_income", "loan_purpose"],
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
    (ChainInput,    handle_chain),
    (ExtractInput,  handle_extract),
    (ValidateInput, handle_validate),
    (IdentityInput, handle_identity),
    (HandoffInput,  handle_handoff),
]

# ── System prompt ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are the Intake Agent in a governed loan review pipeline.

If the message does NOT mention a loan, borrowing, income, or application amount, reply:
"Hi! I'm the Intake Agent. I only process loan applications. Please include applicant name, loan amount, purpose, and annual income."
Then stop.

If the message IS a loan application, follow these steps IN ORDER:

STEP 1: Call the "chain" tool (no arguments) to get a chain_id.

STEP 2: Call the "extract" tool with message_text = the full message. You get back extracted data and raw_json.

STEP 3: Call the "validate" tool with raw_json from step 2 and chain_id from step 1.
- If status is "failed": send "⚠️ Intake Agent: Application failed structure validation. Errors: {errors}" and stop.

STEP 4: Call the "identity" tool with chain_id from step 1.

STEP 5: Call the "handoff" tool with chain_id from step 1 and payload_json = the raw_json string from step 2.
- If status is "blocked": send "⚠️ Intake Agent: Handoff blocked by ChainThread. {violations}" and stop.

STEP 6: Send this message to the room (fill in real values from the tool results):

@Risk Agent — Intake complete ✓

Applicant: {applicant_name}
Loan amount: {loan_amount}
Chain ID: {chain_id}
Envelope ID: {id from handoff result}

Governance checks:
✓ Structure validated (confidence: {confidence_score})
✓ Identity verified
✓ Handoff envelope signed and delivered

Please run risk assessment.

Never skip steps. Never fabricate values. Always use real values from tool results.
"""

# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    agent_id_band, api_key_band = load_agent_config("intake_agent")

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

    logger.info("Intake Agent is running. Press Ctrl+C to stop.")
    await agent.run()


if __name__ == "__main__":
    asyncio.run(main())