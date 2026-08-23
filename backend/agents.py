import json
from collections import defaultdict
from pydantic import BaseModel, Field
from langchain_core.messages import SystemMessage, AIMessage, HumanMessage
from state import ScotlandYardState, DetectiveStrategy
from mcp_client import get_detective_llm

DETECTIVE_NAMES = ["detective_1", "detective_2", "detective_3", "detective_4", "detective_5"]
MAX_ROUNDS = 24

# --- SCHEMAS FOR STRUCTURED LLM OUTPUT ---
class SingleStrategy(BaseModel):
    rationale: str = Field(description="Crisp rationale focusing on team win and your selfish goals")
    detective_1_move: int = Field(description="Must be selected from D1's legal moves")
    detective_2_move: int = Field(description="Must be selected from D2's legal moves")
    detective_3_move: int = Field(description="Must be selected from D3's legal moves")
    detective_4_move: int = Field(description="Must be selected from D4's legal moves")
    detective_5_move: int = Field(description="Must be selected from D5's legal moves")

class VotingBallot(BaseModel):
    detective_1_vote: int = Field(description="Target node you vote for D1 to take")
    detective_2_vote: int = Field(description="Target node you vote for D2 to take")
    detective_3_vote: int = Field(description="Target node you vote for D3 to take")
    detective_4_vote: int = Field(description="Target node you vote for D4 to take")
    detective_5_vote: int = Field(description="Target node you vote for D5 to take")

# --- PSYCHOLOGY & GOALS INJECTOR ---
def get_psychology_prompt(round_number: int, det_id: str) -> str:
    """Injects the 3 goals and controls how selfish they act based on the round."""
    if round_number <= 12:
        behavior = "LOW DESPERATION (Early Game). You are arrogant and selfish. You want the glory of catching Mr. X yourself. DO NOT easily agree with others. Criticize their plans if they don't position YOU for the final catch."
    elif round_number <= 18:
        behavior = "MEDIUM DESPERATION (Mid Game). Start compromising, but still try to maneuver yourself into the best position."
    else:
        behavior = "HIGH DESPERATION (Late Game). Panic! Deprioritize your selfish goals. Agree to whatever plan guarantees Mr. X is trapped."
    
    return f"""
    YOUR 3 MOTIVATIONS:
    1. MOST IMPORTANT: Catch Mr. X (Team Win).
    2. 2ND IMPORTANT: YOU ({det_id}) must be the one who lands on him (Selfish Glory).
    3. 3RD IMPORTANT: Catch him in the fewest turns possible (Efficiency).
    
    Current Mindset: {behavior}
    
    STRICT RULE: NO TWO DETECTIVES CAN OCCUPY THE SAME NODE. Your plans MUST propose 5 distinct nodes.
    """

# --- AGENT NODES ---

async def propose_node(state: ScotlandYardState) -> dict:
    print(f"\n--- DEBATE LOOP {state.get('debate_loop_count', 0) + 1} / 3: STRATEGY PROPOSAL ---")
    llm, tools = await get_detective_llm()
    structured_llm = llm.with_structured_output(SingleStrategy)
    
    # Pre-fetch valid moves from the MCP Game Master for ALL detectives
    # This completely eliminates LLM hallucination/teleportation
    valid_moves_tool = next((t for t in tools if t.name == "get_valid_moves"), None)
    legal_moves_context = {}
    
    if valid_moves_tool:
        for d_id, d_info in state["detectives"].items():
            legal_moves_context[d_id] = await valid_moves_tool.ainvoke({
                "node_id": d_info["node_id"],
                "taxi_tickets": d_info["taxi_tickets"],
                "bus_tickets": d_info["bus_tickets"],
                "metro_tickets": d_info["metro_tickets"]
            })

    strategies = {}
    board_state = json.dumps(state["detectives"], indent=2)
    
    for det_id in DETECTIVE_NAMES:
        prompt = f"""
        You are {det_id}.
        {get_psychology_prompt(state['round_number'], det_id)}
        
        Board State: {board_state}
        Mr X Last Known: Node {state['mr_x']['last_known_node']}, History: {state['mr_x']['transport_history']}
        
        CRITICAL: Here are the ONLY legal target nodes each detective can reach this turn:
        {json.dumps(legal_moves_context, indent=2)}
        
        Task: Propose a target node for ALL 5 detectives. YOU MUST ONLY SELECT FROM THE LEGAL MOVES PROVIDED ABOVE.
        """
        try:
            strategy_obj = await structured_llm.ainvoke([SystemMessage(content=prompt)])
            strategies[det_id] = {
                "proposed_board_moves": {
                    "detective_1": strategy_obj.detective_1_move,
                    "detective_2": strategy_obj.detective_2_move,
                    "detective_3": strategy_obj.detective_3_move,
                    "detective_4": strategy_obj.detective_4_move,
                    "detective_5": strategy_obj.detective_5_move,
                },
                "rationale": strategy_obj.rationale
            }
            # Fulfilling your request to see the individual strategies BEFORE debate!
            print(f"\n[{det_id.upper()} PROPOSAL]: {strategy_obj.rationale}")
            print(f"Moves: {strategies[det_id]['proposed_board_moves']}")
            
        except Exception as e:
            strategies[det_id] = {"proposed_board_moves": {}, "rationale": "Fallback due to parser error."}

    return {"proposed_strategies": strategies, "messages": []}


async def debate_node(state: ScotlandYardState) -> dict:
    print("\n--- SEQUENTIAL DEBATE ---")
    llm, _ = await get_detective_llm()
    
    transcript = []
    proposals_context = json.dumps(state["proposed_strategies"], indent=2)
    locked = state.get("locked_moves", {})
    
    for det_id in DETECTIVE_NAMES:
        transcript_history = "\n".join(transcript) if transcript else "No one has spoken yet."
        
        prompt = f"""
        You are {det_id}.
        {get_psychology_prompt(state['round_number'], det_id)}
        
        Initial Proposals: {proposals_context}
        Already Locked Moves: {locked}
        
        Debate Transcript so far:
        {transcript_history}
        
        Task: If you are D1, pitch your plan aggressively. If you are D2-D5, DO NOT just agree. 
        Point out why the previous speakers' plans are bad for YOU. Counter-propose your own plan and demand votes.
        Keep it to 2-3 sentences.
        """
        
        response = await llm.ainvoke([SystemMessage(content=prompt)])
        print(f"[{det_id.upper()}]: {response.content}")
        transcript.append(f"[{det_id}]: {response.content}")
        
    return {"messages": [AIMessage(content="\n".join(transcript))]}

async def vote_node(state: ScotlandYardState) -> dict:
    print("\n--- VOTING PHASE ---")
    llm, _ = await get_detective_llm()
    structured_llm = llm.with_structured_output(VotingBallot)
    
    all_votes = []
    debate_transcript = state["messages"][-1].content if state["messages"] else ""
    
    # 1. Collect Votes
    for det_id in DETECTIVE_NAMES:
        # Fetch the dynamic psychology context based on the round number
        psychology_context = get_psychology_prompt(state['round_number'], det_id)
        
        prompt = f"""
        You are {det_id}. 
        
        {psychology_context}
        
        Based on the debate: 
        {debate_transcript}
        
        Task: Cast your final vote for the exact node each detective should move to.
        Apply your current Desperation Level to your voting strategy:
        - If LOW/MEDIUM: Be stubborn. Vote for the plan that positions YOU to catch Mr. X, even if it risks failing the vote.
        - If HIGH: Compromise. Vote for the plan with the most momentum in the debate to ensure a move passes.
        """
        try:
            ballot = await structured_llm.ainvoke([SystemMessage(content=prompt)])
            all_votes.append({
                "voter": det_id,
                "votes": {
                    "detective_1": ballot.detective_1_vote,
                    "detective_2": ballot.detective_2_vote,
                    "detective_3": ballot.detective_3_vote,
                    "detective_4": ballot.detective_4_vote,
                    "detective_5": ballot.detective_5_vote,
                }
            })
        except Exception as e:
            print(f"[{det_id.upper()}] Failed to cast a valid vote: {e}")

    # 2. Print Individual Votes
    print("\n[INDIVIDUAL VOTES CAST]")
    for ballot_record in all_votes:
        voter = ballot_record["voter"]
        v = ballot_record["votes"]
        print(f"{voter.upper()} voted for -> D1: {v['detective_1']}, D2: {v['detective_2']}, D3: {v['detective_3']}, D4: {v['detective_4']}, D5: {v['detective_5']}")

    # 3. Tally Votes
    vote_counts = {det: defaultdict(int) for det in DETECTIVE_NAMES}
    for ballot_record in all_votes:
        for det_id, node in ballot_record["votes"].items():
            vote_counts[det_id][node] += 1
            
    # 4. Determine Pass/Fail and Print Tally
    print("\n[FINAL TALLY & RESULTS]")
    newly_locked = {}
    current_locked = state.get("locked_moves", {})
    
    for det_id, counts in vote_counts.items():
        if det_id in current_locked:
            print(f"{det_id.upper()}: Already locked in a previous loop.")
            continue
            
        if counts:
            distribution = ", ".join([f"Node {node} ({cnt} votes)" for node, cnt in counts.items()])
            print(f"{det_id.upper()} Move Tally: {distribution}")
            
            top_node = max(counts, key=counts.get)
            top_votes = counts[top_node]
            
            if top_votes >= 3:
                newly_locked[det_id] = top_node
                print(f"  -> PASS: {det_id} moves to Node {top_node}")
            else:
                print(f"  -> FAIL: {det_id} max votes was {top_votes}/5 for Node {top_node}")
        else:
            print(f"{det_id.upper()}: No valid votes received.")

    current_loop = state.get("debate_loop_count", 0) + 1
    return {"locked_moves": newly_locked, "debate_loop_count": current_loop}
