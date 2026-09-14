"""
The Game Master: the board graph, move-legality rules, and topology (BFS) helpers.

This module has two distinct consumers, and the distinction matters:

1. **In-process callers** (the vast majority). Everything else in this package imports
   `compute_valid_moves` / `get_node_info` / `compute_mrx_zone` and calls them directly as
   plain Python. This is what actually enforces move legality in the game - see ADR-0001.

2. **MCP clients** (external/interactive only). The `@mcp.tool()`-decorated wrappers below
   expose the same logic over the Model Context Protocol for an external client such as an
   IDE assistant - `.cursor/mcp.json` wires this up. Run it with:
       python -m scotland_yard.game_master
   The application's own agents deliberately do NOT route through MCP; see ADR-0001 for the
   full history of why that changed.

Logging goes to stderr, never stdout: when run as an MCP server, stdout carries the
JSON-RPC stream and any stray print would corrupt it.
"""
import sys
import json
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from fastmcp import FastMCP

# Initialize the MCP Server
mcp = FastMCP("ScotlandYardGameMaster")

# Repo root is three levels up: backend/scotland_yard/game_master.py -> backend -> repo root.
BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / "data"
MAP_PATH = DATA_DIR / "board" / "map.json"
NODE_POSITIONS_PATH = DATA_DIR / "board" / "node_positions.json"
RULES_PATH = BASE_DIR / "docs" / "rules" / "rules.md"

# Load the Map Data into memory
def load_map():
    with open(MAP_PATH, "r", encoding="utf-8") as f:
        return json.load(f)["nodes"]

def load_node_positions() -> Dict[str, dict]:
    """Per-node {x, y} board pixel-percentage coordinates, keyed by node id as a string (see data/board/node_positions.json)."""
    with open(NODE_POSITIONS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

map_data = load_map()
node_index: Dict[int, dict] = {node["id"]: node for node in map_data}
node_positions = load_node_positions()

@mcp.tool()
def get_node_info(node_id: int) -> dict:
    """
    Returns the allowed transport modes and all valid connections for a specific node.
    Use this to inspect the board.
    """
    node = node_index.get(node_id)
    if node is None:
        return {"error": f"Node {node_id} does not exist on the board."}
    return node

def compute_valid_moves(
    node_id: int,
    taxi_tickets: int,
    bus_tickets: int,
    metro_tickets: int,
    black_tickets: int = 0,
    occupied_nodes: Optional[List[int]] = None
) -> list:
    """
    Calculates all legally available target nodes a player can move to from their current node,
    based STRICTLY on the tickets they currently possess. Destinations listed in occupied_nodes
    (nodes currently held by another player) are excluded, per the board's node-occupancy rule.

    Plain function (not an MCP tool) so server-side code - round resolution, Mr. X's human turn -
    can call it directly in-process, without paying the MCP stdio subprocess round-trip that only
    LLM tool-calling actually needs.
    """
    node_info = get_node_info(node_id)
    if "error" in node_info:
        return [node_info]

    occupied = set(occupied_nodes or [])
    valid_moves = []
    for connection in node_info["connections"]:
        req_type = connection["type"]
        target = connection["destination"]

        if target in occupied:
            continue

        # Check if the player has the required ticket for this connection
        can_move = False
        if req_type == "taxi" and (taxi_tickets > 0 or black_tickets > 0):
            can_move = True
        elif req_type == "bus" and (bus_tickets > 0 or black_tickets > 0):
            can_move = True
        elif req_type == "metro" and (metro_tickets > 0 or black_tickets > 0):
            can_move = True
        elif req_type == "boat" and black_tickets > 0:
            can_move = True

        if can_move:
            valid_moves.append({
                "target_node": target,
                "transport_used": req_type
            })

    return valid_moves

def _bfs_from(start_nodes: set, max_hops: Optional[int] = None, blocked_nodes: Optional[set] = None) -> Dict[int, int]:
    """
    Multi-source, unweighted BFS over the board graph (any connection type, ignoring ticket
    availability - a plain hop-count distance, not a ticket-constrained one). Returns
    {node_id: hop_distance}, seeded at distance 0 for every node in start_nodes. blocked_nodes
    are never added to the frontier, so they're excluded as both a destination and a
    pass-through point. Stops expanding past max_hops if given; runs to exhaustion if None.
    """
    blocked = blocked_nodes or set()
    visited = {n: 0 for n in start_nodes if n not in blocked}
    frontier = list(visited.keys())
    hop = 0
    while frontier and (max_hops is None or hop < max_hops):
        hop += 1
        next_frontier = []
        for node in frontier:
            node_info = get_node_info(node)
            if "error" in node_info:
                continue
            for connection in node_info["connections"]:
                dest = connection["destination"]
                if dest in blocked or dest in visited:
                    continue
                visited[dest] = hop
                next_frontier.append(dest)
        frontier = next_frontier
    return visited

def compute_mrx_zone(last_known_node: int, max_hops: int, occupied_nodes: Optional[List[int]] = None) -> Dict[int, int]:
    """
    Every node Mr. X could plausibly be standing on right now: everything reachable from his
    last-known node within max_hops (== turns elapsed since he last surfaced, capped by the
    caller), via any connection type. Ticket-blind - does not check whether Mr. X's actual
    remaining ticket inventory could really pay for a given path (documented simplification;
    see docs/issues/known_issues.md). Blocks through currently-occupied detective nodes, per
    rules.md: "Mr. X cannot move to, or pass through, a Node occupied by a Detective."
    """
    return _bfs_from({last_known_node}, max_hops=max_hops, blocked_nodes=set(occupied_nodes or []))

# Every transport type a board edge can carry. A black ticket pays for any of them (rules.md
# §3), which is exactly why a black-ticket hop below restores the full untyped fan-out.
ALL_TRANSPORT_TYPES = frozenset({"taxi", "bus", "metro", "boat"})


def _step_typed(frontier: Iterable[int], allowed_types: Iterable[str], blocked: set) -> set:
    """One hop from every node in `frontier`, over edges whose type is in `allowed_types`."""
    allowed = set(allowed_types)
    reached = set()
    for node in frontier:
        node_info = get_node_info(node)
        if "error" in node_info:
            continue
        for connection in node_info["connections"]:
            destination = connection["destination"]
            if connection["type"] in allowed and destination not in blocked:
                reached.add(destination)
    return reached


def compute_mrx_zone_from_tickets(
    last_known_node: int,
    tickets_spent: Iterable[str],
    occupied_nodes: Optional[List[int]] = None,
) -> Optional[set]:
    """
    Where Mr. X can be after spending EXACTLY this sequence of tickets from his last-known
    node - the ticket-typed replacement for `compute_mrx_zone`'s untyped ball.

    The travel log is public and exact (rules.md §2), so each hop's connection type is known,
    not guessed: a `"bus"` entry means that hop crossed a bus edge and nothing else. Walking
    the graph one typed layer at a time is therefore not a heuristic - it is the precise set of
    nodes consistent with what the detectives have actually been told. Measured on the real
    board this is roughly half the size of the untyped ball at every hop count (9.5 vs 18.2
    nodes at 2 hops, 35 vs 88 at 4).

    This is a different technique from the one `known_issues.md` ISSUE-015 declined, and the
    distinction is the whole reason it is safe: that proposal was to model whether Mr. X's
    remaining INVENTORY could have afforded a hypothetical path, which gets harder as his
    inventory shrinks. This models only which edges the tickets he demonstrably spent are able
    to cross. A `"black"` entry widens back to every type, since that is precisely what a black
    ticket buys him - the obfuscation still works, it just now costs him a scarce ticket.

    Returns the set of nodes at exactly `len(tickets_spent)` hops - not a ball, because Mr. X
    must move every round, so he cannot still be standing where he was last seen after one hop
    (he can of course return there later, and a longer walk will include it again).

    Returns None if the walk dead-ends, which can only mean an input disagrees with the board
    (a ticket type that crosses no edge from anywhere reachable). Callers fall back rather than
    hand detectives an empty or wrong zone: this set is used to RULE OUT locations, so being a
    superset of the truth is safe and being a subset is not.
    """
    blocked = set(occupied_nodes or [])
    frontier = {last_known_node}
    for ticket in tickets_spent:
        allowed = ALL_TRANSPORT_TYPES if ticket == "black" else {ticket}
        frontier = _step_typed(frontier, allowed, blocked)
        if not frontier:
            return None
    return frontier


def project_zone_one_hop(zone_nodes: Iterable[int], blocked_nodes: Optional[Iterable[int]] = None) -> set:
    """
    Where Mr. X could be one round from now, given where he could be now.

    Untyped on purpose: his NEXT ticket has not been played yet, so unlike
    `compute_mrx_zone_from_tickets` there is nothing to filter the edges by. Boat edges are
    included without checking that he still holds a black ticket to pay for one - the same
    deliberate over-inclusion as ISSUE-015, and safe for the same reason (a superset).

    Used to score a candidate destination by how much of Mr. X's escape space standing there
    would close off, by passing that destination in `blocked_nodes`.
    """
    return _step_typed(zone_nodes, ALL_TRANSPORT_TYPES, set(blocked_nodes or []))


def compute_distances_to_zone(zone_nodes: Iterable[int]) -> Dict[int, int]:
    """
    {node_id: hops_to_nearest_zone_node} for every node on the board, computed as a single
    multi-source BFS seeded from every node in zone_nodes at once (distance 0) - far cheaper
    than running a separate BFS per node that needs a distance looked up. Not occupancy-
    blocked: this projects several ROUNDS into the future (how many moves it would take a
    detective to eventually reach this area), by which point current occupancy will already
    have changed, so blocking on today's board state would just be misleading.
    """
    return _bfs_from(set(zone_nodes), max_hops=None, blocked_nodes=None)

@mcp.tool()
def get_valid_moves(
    node_id: int,
    taxi_tickets: int,
    bus_tickets: int,
    metro_tickets: int,
    black_tickets: int = 0,
    occupied_nodes: Optional[List[int]] = None
) -> list:
    """
    Calculates all legally available target nodes a player can move to from their current node,
    based STRICTLY on the tickets they currently possess. Destinations listed in occupied_nodes
    (nodes currently held by another player) are excluded, per the board's node-occupancy rule.
    """
    return compute_valid_moves(node_id, taxi_tickets, bus_tickets, metro_tickets, black_tickets, occupied_nodes)

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
    this; the application's own agents call the plain functions above directly (ADR-0001).
    """
    print(f"Starting Game Master MCP Server... Loaded {len(map_data)} nodes.", file=sys.stderr)
    mcp.run()


if __name__ == "__main__":
    main()