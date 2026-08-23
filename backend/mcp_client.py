import os
import asyncio
from pathlib import Path
from dotenv import load_dotenv
from langchain_groq import ChatGroq
from langchain_mcp_adapters.client import MultiServerMCPClient

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

async def get_detective_llm():
    """
    Retrieves the tools from the Game Master and binds them to the Groq LLM.
    Returns the configured LLM and the tools for LangGraph execution.
    """
    # Load tools dynamically from the client
    tools = await mcp_client.get_tools()
    
    # Initialize the Groq LLM (using the recommended developer model)
    llm = ChatGroq(
        model="openai/gpt-oss-20b",
        api_key=os.getenv("GROQ_API_KEY"),
        temperature=0.2 # Low temperature for logical deduction
    )
    
    # Bind the MCP tools to the LLM
    llm_with_tools = llm.bind_tools(tools)
    return llm_with_tools, tools

if __name__ == "__main__":
    async def test_connection():
        print("Connecting to Game Master MCP Server...")
        llm, tools = await get_detective_llm()
        print(f"Success! Loaded {len(tools)} tools: {[t.name for t in tools]}")
        print("Successfully bound MCP tools to Groq LLM. Ready for LangGraph!")

    asyncio.run(test_connection())