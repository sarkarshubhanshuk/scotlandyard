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

async def get_detective_llm():
    """
    Retrieves the tools from the Game Master and binds them to the detective LLM (served via
    OpenRouter). Returns the configured LLM and the tools for LangGraph execution. Result is
    cached after the first call.
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

        # Initialize the LLM via OpenRouter (OpenAI-compatible endpoint). The optional
        # headers just identify this app on OpenRouter's dashboard/leaderboards - harmless
        # to omit, but recommended by their docs.
        #
        # timeout is NOT optional: under this app's concurrent asyncio.gather load (5
        # detectives firing at once), OpenRouter has been observed to leave one request in
        # a batch hanging indefinitely (confirmed empirically - 4/5 concurrent calls
        # returned in 3-36s, the 5th never returned even after 75+s). With no client-side
        # timeout, that hang never raises an error, so neither the SDK's own retry nor this
        # app's per-call try/except fallback (agents.py) ever gets a chance to run - the
        # coroutine just blocks forever. 45s comfortably clears every observed successful
        # call's latency while still failing fast enough that one stuck call can't stall an
        # entire loop.
        llm = ChatOpenAI(
            model=OPENROUTER_MODEL,
            base_url=OPENROUTER_BASE_URL,
            api_key=os.getenv("OPENROUTER_API_KEY"),
            temperature=0.2, # Low temperature for logical deduction
            timeout=45,
            default_headers={
                "HTTP-Referer": "https://github.com/sarkarshubhanshuk/scotlandyard",
                "X-Title": "Scotland Yard AI",
            }
        )

        # Bind the MCP tools to the LLM
        _cached_tools = tools
        _cached_llm_with_tools = llm.bind_tools(tools)
        return _cached_llm_with_tools, _cached_tools

if __name__ == "__main__":
    async def test_connection():
        print("Connecting to Game Master MCP Server...")
        llm, tools = await get_detective_llm()
        print(f"Success! Loaded {len(tools)} tools: {[t.name for t in tools]}")
        print("Successfully bound MCP tools to the OpenRouter LLM. Ready for LangGraph!")

    asyncio.run(test_connection())