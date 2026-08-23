import asyncio
from graph import detective_graph

async def run_test_round():
    print("\n=== STARTING PHASE 3 END-TO-END TEST ===")
    print("Initializing Game State (Round 3 - Mr. X Revealed at Node 13)...")

    # Mock starting state for Round 3
    initial_state = {
        "round_number": 3,
        "debate_loop_count": 0,
        "mr_x": {
            "last_known_node": 13,
            "last_known_round": 3,
            "transport_history": ["taxi", "taxi", "bus"],
            "taxi_tickets": 10,
            "bus_tickets": 8,
            "metro_tickets": 4,
            "black_tickets": 5,
            "double_tickets": 2
        },
        # Spread the detectives out across the map
        "detectives": {
            "detective_1": {"node_id": 29, "taxi_tickets": 10, "bus_tickets": 8, "metro_tickets": 4},
            "detective_2": {"node_id": 50, "taxi_tickets": 10, "bus_tickets": 8, "metro_tickets": 4},
            "detective_3": {"node_id": 91, "taxi_tickets": 10, "bus_tickets": 8, "metro_tickets": 4},
            "detective_4": {"node_id": 117, "taxi_tickets": 10, "bus_tickets": 8, "metro_tickets": 4},
            "detective_5": {"node_id": 123, "taxi_tickets": 10, "bus_tickets": 8, "metro_tickets": 4},
        },
        "messages": [],
        "proposed_strategies": {},
        "locked_moves": {},
        "final_moves": {}
    }

    print("\nFiring up the LangGraph Multi-Agent Engine...")
    
    # We use astream() to process the graph asynchronously and watch the steps unfold
    async for output in detective_graph.astream(initial_state):
        # Output is a dict showing which node just completed
        for node_name, state_update in output.items():
            print(f"\n[SYSTEM] --> Node '{node_name.upper()}' finished execution.")
            
    print("\n=== ROUND COMPLETE ===")

if __name__ == "__main__":
    asyncio.run(run_test_round())