"""
The OpenRouter-backed LLM clients the detective agents use.

There is no MCP client here any more. The agents used to bind the Game Master's MCP tools
to the LLM, but they never actually took a tool-calling path: propose/vote wrap the model in
`with_structured_output(...)`, the psychology prompt explicitly forbids tool use, and
debate_node deliberately runs tool-less (ISSUE-003). The board lookups that looked like tool
calls were pre-fetches this application makes on the agents' behalf, so they now call
game_master.compute_valid_moves directly in-process. See ADR-0001 for the full reasoning and
for what still uses the MCP server.

Two separately-cached clients live here because they are configured differently:
  get_detective_llm()  propose_node / vote_node - structured output.
  get_debate_llm()     debate_node - free-text pitch plus a structured stance.
Both are built once and reused across every node call, loop, and round.
"""
import asyncio
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI

logger = logging.getLogger(__name__)

# Detective agents run through OpenRouter (an OpenAI-API-compatible aggregator), not a
# provider-specific SDK - this is what makes swapping the underlying model a one-line change.
# See ADR-0004 for why this provider/model, and for the reasoning-budget findings below.
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_MODEL = "deepseek/deepseek-v4-flash-0731"

# Locate backend directory and load environment variables from backend/.env
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(dotenv_path=BASE_DIR / ".env")

# Cached clients. Each guards its own construction with its own lock: the two are
# differently-configured instances, so they cannot share a cache slot.
_cached_detective_llm = None
_detective_cache_lock = asyncio.Lock()
_cached_debate_llm = None
_debate_cache_lock = asyncio.Lock()


def _build_chat_llm() -> ChatOpenAI:
    """
    Shared OpenRouter client config for every detective-facing LLM instance (tool-bound or
    not) - get_detective_llm() and get_debate_llm() both build on this rather than duplicating
    the constructor call.

    The optional headers just identify this app on OpenRouter's dashboard/leaderboards -
    harmless to omit, but recommended by their docs.

    timeout is NOT optional: under this app's concurrent asyncio.gather load (5 detectives
    firing at once), OpenRouter has been observed to leave one request in a batch hanging
    indefinitely (confirmed empirically - 4/5 concurrent calls returned in 3-36s, the 5th never
    returned even after 75+s). With no client-side timeout, that hang never raises an error, so
    neither the SDK's own retry nor this app's per-call try/except fallback (agents.py) ever
    gets a chance to run - the coroutine just blocks forever.

    What 45s actually bounds (ISSUE-009): this is enforced by the underlying HTTP client as an
    IDLE-GAP timeout - reset by each streamed chunk - not as a hard wall-clock deadline. Calls
    lasting up to 11m 27s have been observed to succeed without ever tripping it, because they
    kept streaming. So it reliably kills a genuinely STUCK call, which is what it was added for,
    but it does not cap total call duration. A real wall-clock cap would need asyncio.wait_for
    around each call, plus a decision about what a timed-out detective should do.

    max_tokens/reasoning cap (ISSUE-006/007, resolved): deepseek-v4-flash-0731's reasoning
    budget was previously uncapped, and a real failure (llm_io_log_full_round_e2e.txt CALL #13)
    showed it can burn the model's entire ~32768-token output ceiling on hidden reasoning and
    never emit the structured answer at all (LengthFinishReasonError). Four configurations were
    tested on real traffic (one full propose/debate/vote loop each, 15 calls/run) before landing
    here - every attempt to BOUND the reasoning budget failed identically, only fully disabling
    it worked:
      - reasoning.max_tokens=2000: 10/30 calls failed (33%), reasoning_tokens 3355-4000 - cap
        ignored entirely by this route's self-hosted vLLM backends (system_fingerprints
        vllm-dev-ep-4997cd02, vllm-0.26.0-dp4-ep-86dc62bb observed).
      - reasoning.effort="low": 3/15 failed (20%), reasoning_tokens 3996-4000 on every failure -
        cap ignored the same way; ~361s total loop latency (12x slower than the fix below).
      - reasoning.effort="minimal" (not an OpenRouter-documented value for this route): 2/15
        failed (13%), reasoning_tokens 4000 on every failure - ~557s total loop latency, the
        slowest of all four configurations tested.
      - reasoning.enabled=False: 0/15 failed (0%), reasoning_tokens confirmed 0, ~30s total loop
        latency. The only configuration that actually disables the reasoning phase rather than
        trying to cap it - this is what's set below.
    Conclusion: this OpenRouter route does not honor any budget-shaped reasoning parameter
    (exact token cap or qualitative effort level) - only the boolean enabled=False actually
    takes effect, presumably because it skips the reasoning code path entirely rather than
    trying to constrain it. max_tokens=4000 is kept as an independent guard regardless.
    `reasoning` is an OpenRouter-specific extension, not part of the standard OpenAI schema, so
    it's passed via extra_body rather than a typed ChatOpenAI field.
    """
    return ChatOpenAI(
        model=OPENROUTER_MODEL,
        base_url=OPENROUTER_BASE_URL,
        api_key=os.getenv("OPENROUTER_API_KEY"),
        temperature=0.2, # Low temperature for logical deduction
        timeout=45,
        max_tokens=4000,
        extra_body={
            "reasoning": {"enabled": False}
        },
        default_headers={
            "HTTP-Referer": "https://github.com/sarkarshubhanshuk/scotlandyard",
            "X-Title": "Scotland Yard AI",
        }
    )

async def get_detective_llm() -> ChatOpenAI:
    """
    propose_node's and vote_node's shared LLM instance. Built once, then cached.

    No tools are bound. Both callers wrap this in `with_structured_output(...)`, which
    constrains the response to a schema rather than to a tool call, and both pre-fetch the
    board data the model needs and inject it as prompt text. See ADR-0001.
    """
    global _cached_detective_llm

    if _cached_detective_llm is not None:
        return _cached_detective_llm

    async with _detective_cache_lock:
        # Re-check in case another coroutine populated the cache while we waited.
        if _cached_detective_llm is None:
            _cached_detective_llm = _build_chat_llm()
        return _cached_detective_llm


async def get_debate_llm() -> ChatOpenAI:
    """
    debate_node's dedicated LLM instance - same OpenRouter config as get_detective_llm()
    (neither binds tools any more; see ADR-0001). Kept as a separate cached instance because
    it historically differed, and because debate_node's task is a 2-3 sentence
    text pitch, and it has no tool-execution loop to handle a tool_calls response - previously,
    reusing the tool-bound instance let the model call get_node_info instead of answering in
    prose (e.g. when a prompt was short on board context), and that response's empty `.content`
    was silently appended to the debate transcript as a blank pitch. With no tools bound here,
    the model has nothing to call, so `.content` is guaranteed to be real prose. Cached the same
    way get_detective_llm() is, so it's built once and reused across every node call/loop/round.
    """
    global _cached_debate_llm

    if _cached_debate_llm is not None:
        return _cached_debate_llm

    async with _debate_cache_lock:
        if _cached_debate_llm is not None:
            return _cached_debate_llm

        _cached_debate_llm = _build_chat_llm()
        return _cached_debate_llm

if __name__ == "__main__":
    async def test_connection():
        llm = await get_detective_llm()
        response = await llm.ainvoke("Reply with the single word: ready")
        print(f"OpenRouter reachable via {OPENROUTER_MODEL}. Response: {response.content!r}")

    asyncio.run(test_connection())
