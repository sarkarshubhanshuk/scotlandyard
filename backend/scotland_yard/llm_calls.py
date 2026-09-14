"""
The structured-output contracts a detective answers with, and the plumbing that gets an answer
out of the model - or deterministically resolves the turn when it cannot.

Nothing here trusts the model. `_invoke` bounds every call with a real wall-clock deadline and
returns None rather than raising; `_enforce_legal_node` overrides whatever node was named if it
is not in the legal set this application computed itself; `_choose_move` allows exactly one
self-correction retry before that enforcement takes over. The retry only reduces how often
enforcement has to fire - the deterministic pass is what guarantees correctness (ADR-0001).

Split out of agents.py so the rules-critical enforcement path is readable on its own, rather
than buried between prompt strings and the turn orchestration.
"""
import asyncio
import logging
from typing import Optional

from langchain_core.messages import HumanMessage
from pydantic import BaseModel, Field

from .limits import record_llm_call
from .rules_constants import LLM_CALL_DEADLINE_SECONDS
from .state import ScotlandYardState

logger = logging.getLogger(__name__)


# --- STRUCTURED LLM OUTPUT SCHEMAS ---
# Fixed, not built per call. Under the old simultaneous design every call had to name a node
# for all five detectives, so the schemas were generated dynamically from whichever ones were
# still undecided. Turn-wise play means each call decides exactly one node, which makes these
# static - and makes each structured response small enough that the reasoning-budget
# truncation failure documented in ISSUE-006/007 has far less room to occur.
class MoveChoice(BaseModel):
    """The mover's own destination, used for both its opening proposal and its final decision."""
    target_node: int = Field(description="The node YOU will move to. Must be one of your legal moves.")
    rationale: str = Field(description="Crisp rationale - 2-3 sentences - for this destination")


class TurnResponseChoice(BaseModel):
    """One non-mover's advisory answer to the mover's proposal."""
    response: str = Field(
        description="Your 2-3 sentence response to the proposal on the table - agree, or say why it is wrong"
    )
    preferred_node: int = Field(
        description="The node YOU would take on your own turn, given everything argued so far. Must be one of YOUR legal moves."
    )


# --- LLM CALL PLUMBING ---

async def _invoke(structured_llm, prompt: str, label: str):
    """
    One structured LLM call, bounded by a real wall-clock deadline, returning None on any
    failure so the caller can resolve the outcome deterministically.

    llm_client.py's `timeout=45` is enforced by the HTTP client as an IDLE-GAP timeout, reset
    by every streamed chunk - it kills a genuinely stuck call but not a merely slow one, and
    calls lasting many minutes have been observed to complete without tripping it (ISSUE-009).
    That was tolerable while propose/vote fired five calls concurrently and a straggler
    overlapped its siblings. Turn-wise play makes all 30 of a round's calls sequential, so one
    slow call adds its full duration to the round. asyncio.wait_for is the wall-clock cap the
    idle-gap timeout never was.

    Timeouts and errors are logged at WARNING, not raised: a round must always produce a legal
    move for every detective, and every caller here has a deterministic fallback.

    Every attempt is counted against the deployment's daily budget (limits.py), including ones
    that fail - a call that times out has still been paid for.
    """
    record_llm_call()
    try:
        return await asyncio.wait_for(
            structured_llm.ainvoke([HumanMessage(content=prompt)]),
            timeout=LLM_CALL_DEADLINE_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.warning("[TIMEOUT] %s exceeded the %ds per-call deadline - falling back.",
                       label, LLM_CALL_DEADLINE_SECONDS)
        return None
    except Exception as e:
        logger.warning("[LLM ERROR] %s failed: %s", label, e)
        return None


def _enforce_legal_node(proposed_node: Optional[int], det_id: str, legal_set: set,
                        state: ScotlandYardState, context: str) -> int:
    """
    Deterministic backup enforcement: the node this detective will actually be recorded as
    choosing, whatever the model said.

    Never trusts the LLM's raw output - a small model can still ignore a prompt instruction,
    and under turn-wise play an out-of-bounds node would otherwise flow straight into
    committed_moves with no vote tally left to discard it. `legal_set` already excludes every
    destination committed earlier this round, so the occupancy rule is enforced here too.

    Falls back to the detective's own lowest-numbered legal move rather than leaving it
    stationary: a detective must move whenever a legal move is available to it. Staying put is
    only correct when it genuinely has none.
    """
    if proposed_node is not None and proposed_node in legal_set:
        return proposed_node

    remaining = sorted(legal_set)
    if remaining:
        logger.warning(
            "[VALIDATION] %s's %s (Node %s) is not one of its legal moves %s. Reassigned to Node %s.",
            det_id, context, proposed_node, remaining, remaining[0])
        return remaining[0]

    current_node = state["detectives"][det_id]["node_id"]
    logger.warning(
        "[VALIDATION] %s's %s (Node %s) is illegal and it has no free legal move to fall back "
        "to. Staying at Node %s.", det_id, context, proposed_node, current_node)
    return current_node


async def _choose_move(structured_llm, prompt: str, det_id: str, legal_set: set,
                       state: ScotlandYardState, phase: str) -> tuple[int, str]:
    """
    A mover's node choice (proposal or final decision), with one self-correction retry before
    deterministic enforcement takes over.

    The retry is best-effort and only reduces how often `_enforce_legal_node` has to intervene;
    it is not what guarantees correctness. It costs one extra sequential call when it fires,
    which is why it only fires on an actually-illegal answer.
    """
    label = f"{det_id} {phase}"
    choice = await _invoke(structured_llm, prompt, label)

    if choice is not None and choice.target_node not in legal_set and legal_set:
        retry_prompt = prompt + f"""

        YOUR PREVIOUS ANSWER (Node {choice.target_node}) IS NOT A LEGAL MOVE FOR YOU.
        Choose again, using ONLY the legal destinations listed above: {sorted(legal_set)}.
        """
        retried = await _invoke(structured_llm, retry_prompt, f"{label} (retry)")
        if retried is not None:
            choice = retried

    node = _enforce_legal_node(
        choice.target_node if choice is not None else None, det_id, legal_set, state, phase,
    )
    rationale = choice.rationale if choice is not None else "No rationale (the call failed)."
    return node, rationale
