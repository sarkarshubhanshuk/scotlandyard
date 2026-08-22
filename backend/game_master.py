import json
from pathlib import Path
from mcp.server.mcpserver import MCPServer

# Initialize the MCP Server
mcp = MCPServer("ScotlandYardGameMaster")

# Resolve paths dynamically based on this file's location
BASE_DIR = Path(__file__).resolve().parent.parent
MAP_PATH = BASE_DIR / "docs" / "map" / "map.json"
RULES_PATH = BASE_DIR / "docs" / "rules" / "rules.md"

# Load the Map Data into memory
def load_map():
    with open(MAP_PATH, "r", encoding="utf-8") as f:
        return json.load(f)["nodes"]

map_data = load_map()

@mcp.tool()
def get_node_info(node_id: int) -> dict:
    """
    Returns the allowed transport modes and all valid connections for a specific node.
    Use this to inspect the board.
    """
    for node in map_data:
        if node["id"] == node_id:
            return node
    return {"error": f"Node {node_id} does not exist on the board."}

@mcp.tool()
def get_valid_moves(node_id: int, taxi_tickets: int, bus_tickets: int, metro_tickets: int, black_tickets: int = 0) -> list:
    """
    Calculates all legally available target nodes a player can move to from their current node,
    based STRICTLY on the tickets they currently possess.
    """
    node_info = get_node_info(node_id)
    if "error" in node_info:
        return [node_info]

    valid_moves = []
    for connection in node_info["connections"]:
        req_type = connection["type"]
        target = connection["destination"]
        
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
    print(f"Starting Game Master MCP Server... Loaded {len(map_data)} nodes.")
    mcp.run(transport="stdio")