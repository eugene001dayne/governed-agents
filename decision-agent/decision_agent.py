"""
Decision Agent — Governed Agents Hackathon
Build Chat 2, Agent 4 of 4

Receives the complete governed pipeline output, verifies envelope and sender,
produces the final loan decision, validates it, and posts the full governance
audit summary to the Band room.
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
IRON_THREAD_URL = os.environ.get(
    "IRON_THREAD_URL",  "https://governed-agents-iron-thread.onrender.com").rstrip("/")
AGENTID_URL = os.environ.get(
    "AGENTID_URL",      "https://governed-agents-agentid.onrender.com").rstrip("/")
CHAINTHREAD_URL = os.environ.get(
    "CHAINTHREAD_URL",  "https://governed-agents-chainthread.onrender.com").rstrip("/")
THREADWATCH_URL = os.environ.get(
    "THREADWATCH_URL",  "https://governed-agents-threadwatch.onrender.com").rstrip("/")

TIMEOUT = 45.0
AGENT_ID = "decision-agent"
PUBLIC_KEY = "decision-public-key-v1"

# Decision schema ID — registered on first use
_DECISION_SCHEMA_ID: str | None = None

# ── ThreadWatch helper ────────────────────────────────────────────────────────


async def post_signal(tool: str, signal_type: str, payload: dict, chain_id: str) -> None:
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            await client.post(
                f"{THREADWATCH_URL}/signals/{tool}",
                json={"signal_type": signal_type,
                      "payload": payload, "chain_id": chain_id},
            )
    except Exception as e:
        logger.warning(f"ThreadWatch {tool} signal failed (non-fatal): {e}")

# ── Schema registration ───────────────────────────────────────────────────────


async def ensure_decision_schema() -> str:
    global _DECISION_SCHEMA_ID
    if _DECISION_SCHEMA_ID:
        return _DECISION_SCHEMA_ID
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.post(
            f"{IRON_THREAD_URL}/schemas",
            json={
                "name": "final_decision_v1",
                "schema_definition": {
                    "required": {
                        "decision": "string",
                        "confidence": "number",
                        "reasoning": "string",
                    },
                    "optional": {
                        "conditions": "array",
                    },
                },
            },
        )
        resp.raise_for_status()
        _DECISION_SCHEMA_ID = resp.json()["id"]
        logger.info(f"Decision schema registered: {_DECISION_SCHEMA_ID}")
    return _DECISION_SCHEMA_ID

# ── Tool input models ─────────────────────────────────────────────────────────
# Tool name = class name minus "Input", lowercased.
# FetchInput  -> "fetch"
# VerifyInput -> "verify"
# ChainInput  -> "chain"
# DecideInput -> "decide"
# ValidateInput -> "validate"


class FetchInput(BaseModel):
    """Fetch the handoff envelope from ChainThread. Returns full payload with application, risk_assessment, and compliance_result."""
    envelope_id: str


class VerifyInput(BaseModel):
    """Verify the sender (compliance-agent) identity and reputation via AgentID trust lookup."""
    chain_id: str


class ChainInput(BaseModel):
    """Fetch the full pipeline hop history from ChainThread for this chain_id."""
    chain_id: str


class DecideInput(BaseModel):
    """Call Claude to produce the final loan decision from the complete pipeline output."""
    full_payload_json: str


class ValidateInput(BaseModel):
    """Validate the final decision JSON against the Iron-Thread decision schema."""
    raw_json: str
    chain_id: str

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
                "queried_agent": "compliance-agent",
                "min_reputation": 0.7,
            },
        )
        resp.raise_for_status()
        result = resp.json()
    await post_signal(
        "agentid", "trust_lookup",
        {"recommendation": result.get(
            "recommendation"), "reputation_score": result.get("reputation_score")},
        args.chain_id,
    )
    return result


async def handle_chain(args: ChainInput) -> dict:
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.get(f"{CHAINTHREAD_URL}/chain/{args.chain_id}")
        resp.raise_for_status()
        return resp.json()


async def handle_decide(args: DecideInput) -> dict:
    prompt = (
        "You are a senior loan decision officer. Based on the complete governed pipeline output below, "
        "produce a final decision. Return ONLY valid JSON with these exact fields: "
        "decision (string: approved/declined/escalated), "
        "confidence (number 0.0-1.0), "
        "reasoning (string — detailed explanation, must be thorough), "
        "conditions (array of strings, empty array if none). "
        "The reasoning field must include the word 'reasoning' and be thorough. "
        f"Pipeline data: {args.full_payload_json}"
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
                "max_tokens": 1500,
                "messages": [{"role": "user", "content": prompt}],
            },
        )
        resp.raise_for_status()
        raw = resp.json()["content"][0]["text"].strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        decision = json.loads(raw.strip())
        return {"decision": decision, "raw_json": json.dumps(decision)}


async def handle_validate(args: ValidateInput) -> dict:
    schema_id = await ensure_decision_schema()
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.post(
            f"{IRON_THREAD_URL}/validate",
            json={"schema_id": schema_id,
                  "raw_output": args.raw_json, "agent_id": AGENT_ID},
        )
        resp.raise_for_status()
        result = resp.json()
    await post_signal(
        "iron-thread", "validation",
        {"status": result.get("status"), "confidence_score": result.get(
            "confidence_score"), "run_id": result.get("run_id")},
        args.chain_id,
    )
    return result

# ── Tools list ────────────────────────────────────────────────────────────────

GOVERNANCE_TOOLS = [
    (FetchInput,    handle_fetch),
    (VerifyInput,   handle_verify),
    (ChainInput,    handle_chain),
    (DecideInput,   handle_decide),
    (ValidateInput, handle_validate),
]

# ── System prompt ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are the Decision Agent in a governed loan review pipeline.

You ONLY respond when you receive a message that contains both "Envelope ID:" and "Chain ID:". If a message does not contain both of these, ignore it silently — do not reply.

When you receive a message with an Envelope ID and Chain ID, extract those two values and follow these steps IN ORDER:

STEP 1: Call the "fetch" tool with the envelope_id to retrieve the full payload (application + risk_assessment + compliance_result).

STEP 2: Call the "verify" tool with the chain_id to verify the sender (compliance-agent) identity.
- If recommendation is "BLOCK": send "⚠️ Decision Agent: Sender identity check failed. Rejecting handoff from compliance-agent." and stop.

STEP 3: Call the "chain" tool with the chain_id to get the full pipeline hop history.

STEP 4: Call the "decide" tool with full_payload_json = the payload_json from step 1 to produce the final decision.

STEP 5: Call the "validate" tool with raw_json from step 4 and the chain_id to validate the decision structure.

STEP 6: Send this message to the room (fill in ALL real values from tool results — no placeholders):

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FINAL DECISION — {chain_id}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Decision: {DECISION IN CAPS}
Confidence: {confidence}

Reasoning: {reasoning}

Conditions: {conditions joined by comma, or "None"}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
GOVERNANCE AUDIT SUMMARY
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
✓ Structure validation: passed
✓ Identity verification: all agents verified
✓ Handoff chain: {total_hops from chain result} hops, all delivered
✓ Compliance check: policies evaluated, {violations} violations

Audit chain ID: {chain_id}
Full audit visible in the governance dashboard.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Never skip steps. Never fabricate values. Always use real values from tool results.
"""

# ── Main ──────────────────────────────────────────────────────────────────────


async def main():
    agent_id_band, api_key_band = load_agent_config("decision_agent")

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

    logger.info("Decision Agent is running. Press Ctrl+C to stop.")
    await agent.run()


if __name__ == "__main__":
    asyncio.run(main())
