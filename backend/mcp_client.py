import os
import asyncio
from pathlib import Path
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain_mcp_adapters.client import MultiServerMCPClient

# Detective agents run through OpenRouter (an OpenAI-API-compatible aggregator), not a
# provider-specific SDK - this is what makes swapping the underlying model a one-line change.
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_MODEL = "deepseek/deepseek-v4-flash-0731"

# Locate backend directory and load environment variables
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(dotenv_path=BASE_DIR / ".env")

# Define absolute paths for Python interpreter and server script
PYTHON_EXE = BASE_DIR / "venv" / "Scripts" / "python.exe"
SERVER_SCRIPT = BASE_DIR / "game_master.py"

# Initialize the stateless Multi-Server MCP Client.
# This safely manages tool execution lifecycles in the background
# so LangGraph nodes don't suffer from "Connection Closed" errors.
mcp_client = MultiServerMCPClient({
    "game_master": {
        "transport": "stdio",
        "command": str(PYTHON_EXE),
        "args": [str(SERVER_SCRIPT)],
        "cwd": str(BASE_DIR)
    }
})

# Cache the bound LLM + tool list so repeated LangGraph node calls (propose/debate/vote,
# across every loop and round) reuse the same MCP tool fetch and OpenRouter client instead
# of re-initializing both on every single node invocation.
_cached_llm_with_tools = None
_cached_tools = None
_cache_lock = asyncio.Lock()

# Separate cache for debate_node's LLM (see ISSUE-003 in known_issues.md): a text-only pitch
# task has no business being tool-bound, and being bound to get_valid_moves/get_node_info/
# read_rules let the model call get_node_info instead of answering in prose when a prompt was
# short on board context - debate_node has no tool-execution loop, so that call's content came
# back empty and was silently appended to the transcript. Kept as its own cache/lock (not
# reusing _cached_llm_with_tools's) since it's a differently-configured client (no bound tools).
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
    gets a chance to run - the coroutine just blocks forever. 45s comfortably clears every
    observed successful call's latency while still failing fast enough that one stuck call
    can't stall an entire loop.

    max_tokens/reasoning cap (ISSUE-006/007): deepseek-v4-flash-0731's reasoning budget was
    previously uncapped, and a real failure (llm_io_log_full_round_e2e.txt CALL #13) showed it
    can burn the model's entire ~32768-token output ceiling on hidden reasoning and never emit
    the structured answer at all (LengthFinishReasonError). That same call's timestamps give an
    empirical throughput of ~80 tokens/sec for this route. reasoning.max_tokens=2000 is derived
    from a 45s latency target (matching the timeout above) minus a ~1000-token reserve for the
    actual answer: 45 * 80 - 1000 ~= 2600, rounded down for margin - about 15x below the
    33,933 reasoning tokens the observed failure consumed, so that exact failure mode is
    structurally impossible, not just less likely. max_tokens=4000 is a second, independent
    guard (reasoning cap + answer reserve + margin) so the total completion ceiling itself
    can't be fully consumed by reasoning even if the reasoning cap were ever ignored. `reasoning`
    is an OpenRouter-specific extension, not part of the standard OpenAI schema, so it's passed
    via extra_body rather than a typed ChatOpenAI field.
    """
    return ChatOpenAI(
        model=OPENROUTER_MODEL,
        base_url=OPENROUTER_BASE_URL,
        api_key=os.getenv("OPENROUTER_API_KEY"),
        temperature=0.2, # Low temperature for logical deduction
        timeout=45,
        max_tokens=4000,
        extra_body={
            "reasoning": {"max_tokens": 2000}
        },
        default_headers={
            "HTTP-Referer": "https://github.com/sarkarshubhanshuk/scotlandyard",
            "X-Title": "Scotland Yard AI",
        }
    )

async def get_detective_llm():
    """
    Retrieves the tools from the Game Master and binds them to the detective LLM (served via
    OpenRouter). Returns the configured LLM and the tools for LangGraph execution. Result is
    cached after the first call.

    Used by propose_node/vote_node, which actually execute tool calls. debate_node deliberately
    does NOT use this - see get_debate_llm().
    """
    global _cached_llm_with_tools, _cached_tools

    if _cached_llm_with_tools is not None:
        return _cached_llm_with_tools, _cached_tools

    async with _cache_lock:
        # Re-check in case another coroutine populated the cache while we waited.
        if _cached_llm_with_tools is not None:
            return _cached_llm_with_tools, _cached_tools

        # Load tools dynamically from the client
        tools = await mcp_client.get_tools()

        # Bind the MCP tools to the LLM
        _cached_tools = tools
        _cached_llm_with_tools = _build_chat_llm().bind_tools(tools)
        return _cached_llm_with_tools, _cached_tools

async def get_debate_llm() -> ChatOpenAI:
    """
    debate_node's dedicated LLM instance - same OpenRouter config as get_detective_llm(), but
    with NO tools bound (see ISSUE-003 in known_issues.md). debate_node's task is a 2-3 sentence
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
        print("Connecting to Game Master MCP Server...")
        llm, tools = await get_detective_llm()
        print(f"Success! Loaded {len(tools)} tools: {[t.name for t in tools]}")
        print("Successfully bound MCP tools to the OpenRouter LLM. Ready for LangGraph!")

    asyncio.run(test_connection())