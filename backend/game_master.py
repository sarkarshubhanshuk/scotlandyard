import sys
import json
from pathlib import Path
from typing import Dict, Iterable, List, Optional
from fastmcp import FastMCP

# Initialize the MCP Server
mcp = FastMCP("ScotlandYardGameMaster")

# Resolve paths dynamically based on this file's location
BASE_DIR = Path(__file__).resolve().parent.parent
MAP_PATH = BASE_DIR / "docs" / "map" / "map.json"
NODE_POSITIONS_PATH = BASE_DIR / "docs" / "map" / "node_positions.json"
RULES_PATH = BASE_DIR / "docs" / "rules" / "rules.md"

# Load the Map Data into memory
def load_map():
    with open(MAP_PATH, "r", encoding="utf-8") as f:
        return json.load(f)["nodes"]

def load_node_positions() -> Dict[str, dict]:
    """Per-node {x, y} board pixel-percentage coordinates, keyed by node id as a string (see docs/map/node_positions.json)."""
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

if __name__ == "__main__":
    # Run the server using Standard I/O (required for MCP communication)
    print(f"Starting Game Master MCP Server... Loaded {len(map_data)} nodes.", file=sys.stderr)
    mcp.run()