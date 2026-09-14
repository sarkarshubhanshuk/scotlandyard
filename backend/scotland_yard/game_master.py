"""
The Game Master MCP server: `board.py`'s logic, exposed over the Model Context Protocol.

This is an **adapter, not the enforcement path**. The application's own agents never route
through MCP - they call the plain functions in `board.py` directly, in-process. What lives here
exists only for an external/interactive client such as an IDE assistant; `.cursor/mcp.json`
wires it up. Run it with:

    python -m scotland_yard.game_master

See ADR-0001 for the full history of why the original "MCP prevents hallucination" framing
stopped being true, and what replaced it.

The board logic itself used to live in this file. It was moved to `board.py` so that importing
a board lookup no longer drags the whole FastMCP framework in behind it: `board.py` is imported
by the HTTP API, the agents and every test, none of which have any use for MCP. The dependency
now points the way ADR-0001 says it should - the adapter imports the domain, never the reverse.

Logging goes to stderr, never stdout: when run as an MCP server, stdout carries the JSON-RPC
stream and any stray print would corrupt it.
"""
import sys

from fastmcp import FastMCP

from .board import RULES_PATH, compute_valid_moves, get_node_info, map_data

# Initialize the MCP Server
mcp = FastMCP("ScotlandYardGameMaster")

# Registered here rather than decorated at the definition site, which is the whole point of the
# split: `board.get_node_info` stays a plain function that anything can import and call without
# FastMCP being involved, and this line is the only thing that also makes it an MCP tool.
mcp.tool()(get_node_info)


@mcp.tool()
def get_valid_moves(
    node_id: int,
    taxi_tickets: int,
    bus_tickets: int,
    metro_tickets: int,
    black_tickets: int = 0,
    occupied_nodes: list | None = None,
) -> list:
    """
    Calculates all legally available target nodes a player can move to from their current node,
    based STRICTLY on the tickets they currently possess. Destinations listed in occupied_nodes
    (nodes currently held by another player) are excluded, per the board's node-occupancy rule.
    """
    return compute_valid_moves(
        node_id, taxi_tickets, bus_tickets, metro_tickets, black_tickets, occupied_nodes
    )


@mcp.tool()
def read_rules() -> str:
    """
    Returns the raw text of the game rules.
    Agents should call this if they need to check win conditions or turn sequence.
    """
    with open(RULES_PATH, "r", encoding="utf-8") as f:
        return f.read()


def main() -> None:
    """
    MCP server entrypoint: `python -m scotland_yard.game_master`.

    Communicates over stdio (required by MCP), so the startup banner goes to stderr - a
    stdout write here would corrupt the JSON-RPC stream. Only external MCP clients use
    this; the application's own agents call board.py's plain functions directly (ADR-0001).
    """
    print(f"Starting Game Master MCP Server... Loaded {len(map_data)} nodes.", file=sys.stderr)
    mcp.run()


if __name__ == "__main__":
    main()

