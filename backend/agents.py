import asyncio
import json
from collections import defaultdict
from pydantic import BaseModel, Field, create_model
from langchain_core.messages import SystemMessage, AIMessage, HumanMessage
from state import ScotlandYardState, DetectiveStrategy
from mcp_client import get_detective_llm

DETECTIVE_NAMES = ["detective_1", "detective_2", "detective_3", "detective_4", "detective_5"]
MAX_ROUNDS = 24

# --- DYNAMIC SCHEMAS FOR STRUCTURED LLM OUTPUT ---
# Built fresh each loop from only the still-undecided ("pending") detectives, so once a
# detective's move locks, the LLM is never even asked to fill in a field for them again -
# this is what actually saves the call/token cost, on top of skipping locked *voters*.
def build_strategy_schema(pending_targets: list) -> type[BaseModel]:
    fields = {
        "rationale": (str, Field(description="Crisp rationale focusing on team win and your selfish goals"))
    }
    for det_id in pending_targets:
        fields[f"{det_id}_move"] = (int, Field(description=f"Must be selected from {det_id}'s legal moves"))
    return create_model("StrategyProposal", **fields)

def build_ballot_schema(pending_targets: list) -> type[BaseModel]:
    fields = {
        f"{det_id}_vote": (int, Field(description=f"Target node you vote for {det_id} to take"))
        for det_id in pending_targets
    }
    return create_model("VotingBallot", **fields)

# --- PROPOSAL CONFLICT DETECTION (used to decide whether to retry a proposer) ---
def find_proposal_conflicts(strategy_obj, pending_targets: list, legal_move_sets: dict) -> list[str]:
    """
    Checks one proposer's raw strategy_obj for (b) out-of-bounds destinations and (c)
    duplicate destinations across the targets in this same call. Does not fix anything -
    just reports human-readable conflicts so the caller can decide whether a retry is
    worthwhile. (Locked-destination violations, rule (d), can't occur here: locked nodes
    are already excluded from legal_move_sets before this is called, so they show up as an
    ordinary out-of-bounds conflict.)
    """
    conflicts = []
    claimed = {}
    for target_id in pending_targets:
        node = getattr(strategy_obj, f"{target_id}_move")
        legal_for_target = legal_move_sets.get(target_id, set())
        if node not in legal_for_target:
            conflicts.append(
                f"{target_id}: proposed Node {node} is not one of its legal moves "
                f"{sorted(legal_for_target)}"
            )
        elif node in claimed:
            conflicts.append(
                f"{target_id}: proposed Node {node} duplicates {claimed[node]}'s destination - "
                f"every detective in this proposal needs a distinct node"
            )
        else:
            claimed[node] = target_id
    return conflicts

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
    
    STRICT RULE: NO TWO DETECTIVES CAN OCCUPY THE SAME NODE. Never propose or vote for a node
    another detective is already using or already locked into this round.
    """

# --- AGENT NODES ---

async def propose_node(state: ScotlandYardState) -> dict:
    print(f"\n--- DEBATE LOOP {state.get('debate_loop_count', 0) + 1} / 3: STRATEGY PROPOSAL ---")

    locked = state.get("locked_moves", {})
    pending_targets = [d for d in DETECTIVE_NAMES if d not in locked]

    if not pending_targets:
        # Everyone already locked this round - nothing left to propose.
        return {"proposed_strategies": {}, "messages": []}

    llm, tools = await get_detective_llm()
    # Schema only asks about still-undecided targets - locked detectives are never
    # re-asked about, which is what actually saves tokens/call complexity each loop.
    structured_llm = llm.with_structured_output(build_strategy_schema(pending_targets))

    # Pre-fetch valid moves from the MCP Game Master for every still-undecided detective.
    # occupied_nodes enforces the board's node-occupancy rule (no two detectives share a
    # node) server-side, rather than relying on the LLM to honor a prompt instruction.
    valid_moves_tool = next((t for t in tools if t.name == "get_valid_moves"), None)
    legal_moves_context = {}
    legal_move_sets = {}
    all_detective_nodes = {d["node_id"] for d in state["detectives"].values()}

    if valid_moves_tool:
        # These per-detective MCP lookups are independent of each other - fetch them
        # concurrently instead of paying 5 sequential round-trips.
        # occupied also includes locked.values() (rule (d)): a locked detective is still
        # physically standing at their OLD node until finalize, but their destination is
        # already reserved, so no still-undecided detective should be offered it as legal.
        reserved_nodes = set(locked.values())

        async def fetch_moves(d_id):
            d_info = state["detectives"][d_id]
            occupied = list(all_detective_nodes - {d_info["node_id"]} | reserved_nodes)
            moves = await valid_moves_tool.ainvoke({
                "node_id": d_info["node_id"],
                "taxi_tickets": d_info["taxi_tickets"],
                "bus_tickets": d_info["bus_tickets"],
                "metro_tickets": d_info["metro_tickets"],
                "occupied_nodes": occupied
            })
            return d_id, moves

        fetch_results = await asyncio.gather(*[fetch_moves(d_id) for d_id in pending_targets])
        for d_id, moves in fetch_results:
            legal_moves_context[d_id] = moves
            legal_move_sets[d_id] = {m["target_node"] for m in moves if "target_node" in m}

    # (c) Proactive collision hint: nodes reachable by more than one still-undecided
    # detective this turn. Naming these explicitly to the proposer is far more effective
    # at preventing duplicates up front than a generic "don't duplicate" instruction.
    contested_nodes = defaultdict(list)
    for target_id in pending_targets:
        for node in legal_move_sets.get(target_id, set()):
            contested_nodes[node].append(target_id)
    contested_nodes = {node: dets for node, dets in contested_nodes.items() if len(dets) > 1}
    contested_note = ""
    if contested_nodes:
        contested_lines = "\n".join(
            f"- Node {node}: reachable by {', '.join(dets)} this turn - assign AT MOST ONE of them here"
            for node, dets in contested_nodes.items()
        )
        contested_note = (
            "\n\n        NODES MORE THAN ONE DETECTIVE CAN REACH THIS TURN (pick at most one "
            f"detective per node below):\n{contested_lines}"
        )

    strategies = {}
    board_state = json.dumps(state["detectives"], indent=2)

    # Every detective proposes every loop, even one whose OWN move already locked -
    # they still have a voice on where the remaining, still-undecided detectives should go.
    # Proposals are blind/independent of each other (nobody sees anyone else's proposal
    # before making their own), so fetch all 5 concurrently instead of sequentially.
    async def get_proposal(det_id):
        prompt = f"""
        You are {det_id}.
        {get_psychology_prompt(state['round_number'], det_id)}

        Board State: {board_state}
        Mr X Last Known: Node {state['mr_x']['last_known_node']}, History: {state['mr_x']['transport_history']}

        Already Locked Moves This Round (FINAL - these detectives are done, do not send anyone
        else to their nodes): {json.dumps(locked, indent=2)}

        The only detectives who still need a move decided this round are: {pending_targets}.
        CRITICAL: Here are the ONLY legal target nodes each of them can reach this turn (already
        excludes nodes currently occupied by other detectives and nodes already locked as
        someone else's destination this round): {json.dumps(legal_moves_context, indent=2)}{contested_note}

        Task: Propose a target node ONLY for the still-undecided detectives listed above
        ({', '.join(pending_targets)}). YOU MUST ONLY SELECT FROM THE LEGAL MOVES PROVIDED ABOVE.
        CRITICAL: Every destination you propose must be a DIFFERENT node from every other
        detective's destination in this same response - two detectives can never be sent to
        the same node.
        """
        try:
            strategy_obj = await structured_llm.ainvoke([SystemMessage(content=prompt)])
        except Exception as e:
            return det_id, None, e

        # Best-effort self-correction: give the proposer exactly one chance to fix its own
        # conflicts before we fall back to deterministic resolution below. Ideally this retry
        # is rarely needed, but a small model can still slip - the deterministic pass is what
        # guarantees correctness either way, this just reduces how often it has to intervene.
        conflicts = find_proposal_conflicts(strategy_obj, pending_targets, legal_move_sets)
        if conflicts:
            retry_prompt = prompt + f"""

        YOUR PREVIOUS PROPOSAL HAD THE FOLLOWING CONFLICTS - FIX THEM:
        {chr(10).join(f'- {c}' for c in conflicts)}

        Provide a corrected proposal for ALL still-undecided detectives ({', '.join(pending_targets)})
        that resolves every conflict above, still only using each detective's legal moves listed
        earlier, with no two detectives sharing a destination.
        """
            try:
                strategy_obj = await structured_llm.ainvoke([SystemMessage(content=retry_prompt)])
            except Exception:
                pass  # Keep the pre-retry proposal; deterministic backup will fix what's left.

        return det_id, strategy_obj, None

    proposal_results = await asyncio.gather(*[get_proposal(det_id) for det_id in DETECTIVE_NAMES])

    # Process in fixed DETECTIVE_NAMES order (asyncio.gather preserves input order in its
    # results regardless of which call actually finished first) so console output stays
    # readable even though the calls above ran concurrently.
    for det_id, strategy_obj, error in proposal_results:
        if error is not None:
            strategies[det_id] = {"proposed_board_moves": dict(locked), "rationale": "Fallback due to parser error."}
            continue

        proposed_moves = dict(locked)  # locked detectives keep their final node
        claimed = set(locked.values())

        # Deterministic backup enforcement (never trust the LLM's raw output, since a
        # small/local model can still ignore prompt instructions) - processed in fixed
        # DETECTIVE_NAMES order so conflicts resolve in favor of the earlier detective:
        #   (b) destination must be in target_id's own legal-move set
        #       (this set already excludes locked destinations, so (d) is enforced here too)
        #   (c) destination must not already be claimed by an earlier target this call
        #   (a) if the raw proposal fails (b) or (c), prefer reassigning target_id to one of
        #       ITS OWN remaining legal+unclaimed moves over leaving them stationary - a
        #       detective must move whenever a legal move is actually available to them.
        #       Staying put is only correct when no such move exists (rare: either they had
        #       no legal moves at all this turn, or every one was claimed by an earlier target).
        for target_id in pending_targets:
            proposed_node = getattr(strategy_obj, f"{target_id}_move")
            legal_for_target = legal_move_sets.get(target_id, set())

            if proposed_node in legal_for_target and proposed_node not in claimed:
                final_node = proposed_node
            else:
                remaining = sorted(legal_for_target - claimed)
                if remaining:
                    final_node = remaining[0]
                    reason = "a duplicate destination" if proposed_node in claimed else "an illegal destination"
                    print(f"[VALIDATION] {det_id}'s proposal for {target_id} (Node "
                          f"{proposed_node}) was {reason}. Reassigned {target_id} to Node "
                          f"{final_node}.")
                else:
                    final_node = state["detectives"][target_id]["node_id"]
                    print(f"[VALIDATION] {det_id}'s proposal for {target_id} (Node "
                          f"{proposed_node}) has no free legal move to fall back to. "
                          f"Reverting {target_id} to stay at Node {final_node}.")

            proposed_moves[target_id] = final_node
            claimed.add(final_node)

        strategies[det_id] = {
            "proposed_board_moves": proposed_moves,
            "rationale": strategy_obj.rationale
        }
        # Fulfilling your request to see the individual strategies BEFORE debate!
        print(f"\n[{det_id.upper()} PROPOSAL]: {strategy_obj.rationale}")
        print(f"Moves: {strategies[det_id]['proposed_board_moves']}")

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

    current_locked = state.get("locked_moves", {})
    pending_targets = [d for d in DETECTIVE_NAMES if d not in current_locked]

    if not pending_targets:
        # Nothing left to vote on this round.
        current_loop = state.get("debate_loop_count", 0) + 1
        return {"locked_moves": {}, "debate_loop_count": current_loop}

    llm, tools = await get_detective_llm()
    # Ballot only asks about still-undecided targets - locked detectives are never
    # re-voted on, which is what actually saves tokens/call complexity each loop.
    structured_llm = llm.with_structured_output(build_ballot_schema(pending_targets))

    # Recompute legal targets (with occupancy) for still-undecided detectives so we can
    # discard any illegal vote before it reaches the tally, mirroring propose_node.
    valid_moves_tool = next((t for t in tools if t.name == "get_valid_moves"), None)
    legal_move_sets = {}
    all_detective_nodes = {d["node_id"] for d in state["detectives"].values()}

    if valid_moves_tool:
        # These per-detective MCP lookups are independent of each other - fetch them
        # concurrently instead of paying 5 sequential round-trips.
        async def fetch_moves(d_id):
            d_info = state["detectives"][d_id]
            occupied = list(all_detective_nodes - {d_info["node_id"]})
            moves = await valid_moves_tool.ainvoke({
                "node_id": d_info["node_id"],
                "taxi_tickets": d_info["taxi_tickets"],
                "bus_tickets": d_info["bus_tickets"],
                "metro_tickets": d_info["metro_tickets"],
                "occupied_nodes": occupied
            })
            return d_id, moves

        fetch_results = await asyncio.gather(*[fetch_moves(d_id) for d_id in pending_targets])
        for d_id, moves in fetch_results:
            legal_move_sets[d_id] = {m["target_node"] for m in moves if "target_node" in m}

    debate_transcript = state["messages"][-1].content if state["messages"] else ""

    # 1. Collect Votes - every detective votes, including ones already locked, since they
    # still have a stake in where the remaining, still-undecided detectives end up. Ballots
    # are cast simultaneously/independently against the same finished debate transcript (no
    # voter sees another's ballot), so cast all 5 concurrently instead of sequentially.
    async def cast_ballot(det_id):
        psychology_context = get_psychology_prompt(state['round_number'], det_id)

        prompt = f"""
        You are {det_id}.

        {psychology_context}

        Based on the debate:
        {debate_transcript}

        The only detectives who still need a vote this round are: {pending_targets}.
        Task: Cast your final vote ONLY for the exact node each of those still-undecided
        detectives should move to.
        Apply your current Desperation Level to your voting strategy:
        - If LOW/MEDIUM: Be stubborn. Vote for the plan that positions YOU to catch Mr. X, even if it risks failing the vote.
        - If HIGH: Compromise. Vote for the plan with the most momentum in the debate to ensure a move passes.
        """
        try:
            ballot = await structured_llm.ainvoke([SystemMessage(content=prompt)])
            votes = {target_id: getattr(ballot, f"{target_id}_vote") for target_id in pending_targets}
            return det_id, votes, None
        except Exception as e:
            return det_id, None, e

    ballot_results = await asyncio.gather(*[cast_ballot(det_id) for det_id in DETECTIVE_NAMES])

    # Process in fixed DETECTIVE_NAMES order (asyncio.gather preserves input order in its
    # results regardless of which call actually finished first) so console output stays
    # readable even though the calls above ran concurrently.
    all_votes = []
    for det_id, votes, error in ballot_results:
        if error is not None:
            print(f"[{det_id.upper()}] Failed to cast a valid vote: {error}")
            continue
        all_votes.append({"voter": det_id, "votes": votes})

    # 2. Print Individual Votes
    print("\n[INDIVIDUAL VOTES CAST]")
    for ballot_record in all_votes:
        voter = ballot_record["voter"]
        vote_summary = ", ".join([f"{det}: {node}" for det, node in ballot_record["votes"].items()])
        print(f"{voter.upper()} voted for -> {vote_summary}")

    # 3. Tally Votes (ballots only ever contain still-undecided targets). A single vote is
    # discarded, never counted, if it's illegal OR if it duplicates a node this same voter
    # already voted for a different detective within this same ballot - never trust the LLM's
    # raw output for either. Targets are checked in the ballot's fixed insertion order (==
    # pending_targets, i.e. DETECTIVE_NAMES order), so within one voter's ballot the
    # earlier-listed detective keeps its vote and a later duplicate is dropped. Unlike
    # propose_node, a discarded vote has no fallback to reassign - it's simply not counted,
    # since nothing requires every voter to vote for every target.
    vote_counts = {det: defaultdict(int) for det in pending_targets}
    for ballot_record in all_votes:
        voter = ballot_record["voter"]
        claimed_nodes = set()
        for det_id, node in ballot_record["votes"].items():
            if node not in legal_move_sets.get(det_id, set()):
                print(f"[VALIDATION] {voter}'s vote for {det_id} (Node {node}) is illegal and was discarded.")
                continue
            if node in claimed_nodes:
                print(f"[VALIDATION] {voter}'s vote for {det_id} (Node {node}) duplicates another "
                      f"detective's vote within the same ballot and was discarded.")
                continue
            claimed_nodes.add(node)
            vote_counts[det_id][node] += 1

    # 4. Determine Pass/Fail and Print Tally
    print("\n[FINAL TALLY & RESULTS]")
    newly_locked = {}

    for det_id, counts in vote_counts.items():
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
