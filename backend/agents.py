import asyncio
import json
from collections import defaultdict
from typing import Optional
from pydantic import BaseModel, Field, create_model
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.config import get_stream_writer
from state import ScotlandYardState, DetectiveStrategy
from mcp_client import get_detective_llm, get_debate_llm
from game_master import compute_mrx_zone, compute_distances_to_zone

DETECTIVE_IDS = ["agent_red", "agent_blue", "agent_green", "agent_yellow", "agent_purple"]
# Human-readable callsigns used anywhere a detective's identity appears in LLM-facing prompt
# text (so agents reason about "Agent Red", not the internal id) - DETECTIVE_IDS itself stays the
# system identifier used for dict keys, schema field names, tickets, etc.
AGENT_DISPLAY_NAMES = {
    "agent_red": "Agent Red",
    "agent_blue": "Agent Blue",
    "agent_green": "Agent Green",
    "agent_yellow": "Agent Yellow",
    "agent_purple": "Agent Purple",
}

def agent_names(det_ids) -> str:
    """Comma-separated display names for a list of detective ids, for prompt-text readability."""
    return ", ".join(AGENT_DISPLAY_NAMES[d] for d in det_ids)

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
        fields[f"{det_id}_move"] = (
            int, Field(description=f"Must be selected from {AGENT_DISPLAY_NAMES[det_id]}'s legal moves")
        )
    return create_model("StrategyProposal", **fields)

def build_ballot_schema(pending_targets: list) -> type[BaseModel]:
    fields = {
        f"{det_id}_vote": (
            int, Field(description=f"Target node you vote for {AGENT_DISPLAY_NAMES[det_id]} to take")
        )
        for det_id in pending_targets
    }
    return create_model("VotingBallot", **fields)

def build_debate_position_schema(pending_targets: list) -> type[BaseModel]:
    """
    ISSUE-004 fix: gives each debate speaker's single LLM call a structured stance alongside
    its free-text pitch, at no extra call cost. Previously the only record of a speaker's
    position was the pitch's prose, which vote_node's cast_ballot never even saw - voters had
    to infer "what everyone landed on after debate" from a 2-3 sentence paraphrase (or nothing,
    before ISSUE-003 was fixed). "{det_id}_position" is that speaker's own current preferred
    node for det_id, given everything argued so far - not a proposal for the team, just their
    stated stance, the same way build_ballot_schema's "{det_id}_vote" works for voting.
    """
    fields = {
        "pitch": (str, Field(description="Your 2-3 sentence pitch/argument for this turn"))
    }
    for det_id in pending_targets:
        fields[f"{det_id}_position"] = (
            int, Field(description=f"Your current preferred node for {AGENT_DISPLAY_NAMES[det_id]}, given the debate so far")
        )
    return create_model("DebatePosition", **fields)

# --- MCP TOOL RESULT UNWRAPPING ---
def parse_valid_moves(raw_result) -> list[dict]:
    """
    BaseTool.ainvoke() on an MCP tool never hands back the server's raw Python return
    value - MCP results travel as content blocks, so a tool that returns a JSON-able
    list comes back as [{"type": "text", "text": "<json-encoded list>", "id": ...}].
    The actual move dicts (with "target_node"/"transport_used") are JSON-encoded inside
    that text field and must be unpacked before use.
    """
    if isinstance(raw_result, list) and raw_result and isinstance(raw_result[0], dict) \
            and raw_result[0].get("type") == "text":
        return json.loads(raw_result[0]["text"])
    return raw_result

# --- MR. X POSSIBLE-ZONE CONTEXT (docs/issues/known_issues.md ISSUE-005) ---
# Server-side board-topology computation injected directly as prompt text - no extra
# LLM/tool round-trip, which is what keeps this from compounding the latency issues already
# logged for propose/vote (ISSUE-006/007/009).
MRX_ZONE_LIST_THRESHOLD = 20  # show the literal node list only up to this many possible nodes

def compute_mrx_zone_context(state: ScotlandYardState) -> Optional[dict]:
    """
    Everywhere Mr. X could plausibly be standing right now, and the hop-distance from every
    board node to the nearest such node. Returns None during rounds 1-2, before he has ever
    surfaced (no last-known node to search from).

    turns_since_surfacing is capped at 4 regardless of the raw round gap - even that cap
    already covers up to ~84% of the 199-node board (empirically checked against
    docs/map/map.json), so a larger cap would add cost without adding useful information. This
    also cleanly absorbs the one asymmetric surfacing gap (round 18 -> round 24 is 6 rounds,
    not 5): the rounds that would otherwise compute 5 hops are already "saturated" at 4 anyway.
    """
    mr_x = state["mr_x"]
    last_known_node = mr_x.get("last_known_node")
    last_known_round = mr_x.get("last_known_round")
    if last_known_node is None:
        return None

    turns_since_surfacing = min(state["round_number"] - last_known_round, 4)
    occupied = {d["node_id"] for d in state["detectives"].values()}
    zone = compute_mrx_zone(last_known_node, turns_since_surfacing, occupied_nodes=occupied)
    return {
        "last_known_node": last_known_node,
        "last_known_round": last_known_round,
        "turns_since_surfacing": turns_since_surfacing,
        "zone_nodes": sorted(zone.keys()),
        "distances_to_zone": compute_distances_to_zone(zone.keys()),
    }

def get_mrx_zone_context(state: ScotlandYardState) -> tuple[Optional[dict], dict]:
    """
    Memoizes compute_mrx_zone_context() for the current round. Its inputs (last_known_node/
    round, round_number, detectives' occupied nodes) are identical across propose/debate/vote
    and every debate-loop iteration within one round - graph.py loops up to 3 times, so calling
    it fresh from each of the 3 nodes meant up to 9 redundant BFS runs per round. Presence of
    "mrx_zone_context" on state (not just truthiness - it's legitimately None pre-reveal) is
    what marks it as already computed this round; build_next_round_state omits the key so each
    new round starts with a cache miss.

    Returns (zone_context, state_update): callers must merge state_update into their own
    returned dict so later nodes/loops in the same round see the cached value instead of
    recomputing it.
    """
    if "mrx_zone_context" in state:
        return state["mrx_zone_context"], {}
    zone_context = compute_mrx_zone_context(state)
    return zone_context, {"mrx_zone_context": zone_context}

def format_mrx_zone_block(zone_context: Optional[dict]) -> str:
    """
    Renders compute_mrx_zone_context()'s result into the fixed prompt block shared by
    propose_node/debate_node/vote_node. Below MRX_ZONE_LIST_THRESHOLD nodes, the zone is small
    enough to be a useful, specific hint, so it's listed outright; above it, only the count is
    shown - past that size the zone covers most of the board anyway, and enumerating it would
    just be token cost with no real narrowing-down value.
    """
    if zone_context is None:
        return "Mr. X has not surfaced yet this game - no location data is available yet."

    zone_nodes = zone_context["zone_nodes"]
    if len(zone_nodes) <= MRX_ZONE_LIST_THRESHOLD:
        location_line = f"Possible locations right now ({len(zone_nodes)} nodes): {zone_nodes}"
    else:
        location_line = (
            f"Possible locations right now: {len(zone_nodes)} nodes (too many to list - his "
            "position is broadly uncertain right now)"
        )
    return (
        f"Last seen: Node {zone_context['last_known_node']} (Round {zone_context['last_known_round']}). "
        f"Turns elapsed since then: {zone_context['turns_since_surfacing']}.\n"
        f"        {location_line}"
    )

def zone_distances_for_moves(moves_by_detective: dict, distances_to_zone: dict) -> dict:
    """{detective_id: hops_to_nearest_possible_mr_x_location} for a {detective_id: node_id} map."""
    return {det_id: distances_to_zone.get(node_id) for det_id, node_id in moves_by_detective.items()}

def build_annotated_proposals(
    state: ScotlandYardState, distances_to_zone: dict, pending_targets: Optional[list] = None
) -> dict:
    """
    {proposer_id: {"proposed_board_moves", "distance_to_mrx_zone_per_move", "rationale"}} for
    every proposer in state["proposed_strategies"]. Shared by debate_node and vote_node
    (ISSUE-004) so both phases see an identical projection of the same underlying proposals,
    rather than two independently hand-built views that can silently drift apart.

    When pending_targets is given, each proposer's proposed_board_moves (and its distance
    annotation) is filtered down to just those targets - locked targets are already shown
    separately wherever this is used (the "Already Locked Moves" line), so including them here
    too would just be duplicated noise scaled by every proposer's full 5-target board.
    """
    result = {}
    for proposer_id, strategy in state["proposed_strategies"].items():
        moves = strategy.get("proposed_board_moves", {})
        if pending_targets is not None:
            moves = {det_id: moves[det_id] for det_id in pending_targets if det_id in moves}
        result[proposer_id] = {
            "proposed_board_moves": moves,
            "distance_to_mrx_zone_per_move": zone_distances_for_moves(moves, distances_to_zone),
            "rationale": strategy.get("rationale"),
        }
    return result

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
                f"{AGENT_DISPLAY_NAMES[target_id]}: proposed Node {node} is not one of its legal "
                f"moves {sorted(legal_for_target)}"
            )
        elif node in claimed:
            conflicts.append(
                f"{AGENT_DISPLAY_NAMES[target_id]}: proposed Node {node} duplicates "
                f"{AGENT_DISPLAY_NAMES[claimed[node]]}'s destination - every detective in this "
                f"proposal needs a distinct node"
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
    2. 2ND IMPORTANT: YOU ({AGENT_DISPLAY_NAMES[det_id]}) must be the one who lands on him (Selfish Glory).
    3. 3RD IMPORTANT: Catch him in the fewest turns possible (Efficiency).
    
    Current Mindset: {behavior}
    
    STRICT RULE: NO TWO DETECTIVES CAN OCCUPY THE SAME NODE. Never propose or vote for a node
    another detective is already using or already locked into this round.

    STRICT RULE: Use ONLY the data given to you in this prompt (board state, legal moves, Mr.
    X's possible zone, distances). Do not attempt to call any tool, browse, or otherwise seek
    outside information about Scotland Yard, the board, or Mr. X - everything you need has
    already been provided.
    """

# --- AGENT NODES ---

async def propose_node(state: ScotlandYardState) -> dict:
    print(f"\n--- DEBATE LOOP {state.get('debate_loop_count', 0) + 1} / 3: STRATEGY PROPOSAL ---")

    locked = state.get("locked_moves", {})
    pending_targets = [d for d in DETECTIVE_IDS if d not in locked]

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
            return d_id, parse_valid_moves(moves)

        fetch_results = await asyncio.gather(*[fetch_moves(d_id) for d_id in pending_targets])
        for d_id, moves in fetch_results:
            legal_moves_context[d_id] = moves
            legal_move_sets[d_id] = {m["target_node"] for m in moves if "target_node" in m}

    # Mr. X possible-zone context (ISSUE-005): annotate each legal-move candidate with its own
    # hop-distance to the nearest node he could plausibly be standing on, and give each
    # still-undecided detective's CURRENT position the same, as a baseline for comparison.
    zone_context, zone_state_update = get_mrx_zone_context(state)
    zone_block = format_mrx_zone_block(zone_context)
    distances_to_zone = zone_context["distances_to_zone"] if zone_context else {}
    for moves in legal_moves_context.values():
        for move in moves:
            move["distance_to_mrx_zone"] = distances_to_zone.get(move["target_node"])
    current_distances_to_zone = {
        d_id: distances_to_zone.get(state["detectives"][d_id]["node_id"])
        for d_id in pending_targets
    }

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
            f"- Node {node}: reachable by {agent_names(dets)} this turn - assign AT MOST ONE of them here"
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
        You are {AGENT_DISPLAY_NAMES[det_id]}.
        {get_psychology_prompt(state['round_number'], det_id)}

        Board State: {board_state}
        Mr. X's Ticket Log (transport types used so far - NOT his location): {state['mr_x']['transport_history']}

        Mr. X's Possible Zone:
        {zone_block}
        Each still-undecided detective's CURRENT distance (in hops) to the nearest node in that
        zone, for comparison: {json.dumps(current_distances_to_zone)}

        Already Locked Moves This Round (FINAL - these detectives are done, do not send anyone
        else to their nodes): {json.dumps(locked, indent=2)}

        The only detectives who still need a move decided this round are: {agent_names(pending_targets)}.
        CRITICAL: Here are the ONLY legal target nodes each of them can reach this turn (already
        excludes nodes currently occupied by other detectives and nodes already locked as
        someone else's destination this round). Each option's "distance_to_mrx_zone" is its own
        hop-distance to the nearest node in Mr. X's possible zone above - 0 means that
        destination IS one of his possible current locations; lower is generally better if you
        want the team closing in on him, compare it against the CURRENT distances above to see
        whether a move is actually progress: {json.dumps(legal_moves_context, indent=2)}{contested_note}

        Task: Propose a target node ONLY for the still-undecided detectives listed above
        ({agent_names(pending_targets)}). YOU MUST ONLY SELECT FROM THE LEGAL MOVES PROVIDED ABOVE.
        CRITICAL: Every destination you propose must be a DIFFERENT node from every other
        detective's destination in this same response - two detectives can never be sent to
        the same node.
        """
        try:
            strategy_obj = await structured_llm.ainvoke([HumanMessage(content=prompt)])
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

        Provide a corrected proposal for ALL still-undecided detectives ({agent_names(pending_targets)})
        that resolves every conflict above, still only using each detective's legal moves listed
        earlier, with no two detectives sharing a destination.
        """
            try:
                strategy_obj = await structured_llm.ainvoke([HumanMessage(content=retry_prompt)])
            except Exception:
                pass  # Keep the pre-retry proposal; deterministic backup will fix what's left.

        return det_id, strategy_obj, None

    # Signal the stage as actually starting only now - right as the first LLM calls are about
    # to fire - not earlier (pending-target computation, MCP legal-move lookups, and zone-context
    # prep above are bookkeeping, not "creating a proposal" from the frontend's point of view).
    get_stream_writer()({"stage": "proposal"})
    proposal_results = await asyncio.gather(*[get_proposal(det_id) for det_id in DETECTIVE_IDS])

    # Process in fixed DETECTIVE_IDS order (asyncio.gather preserves input order in its
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
        # DETECTIVE_IDS order so conflicts resolve in favor of the earlier detective:
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

    return {"proposed_strategies": strategies, "messages": [], **zone_state_update}


async def debate_node(state: ScotlandYardState) -> dict:
    print("\n--- SEQUENTIAL DEBATE ---")
    # No tools bound here (ISSUE-003, fixed): this is a text-only pitch task with no tool-
    # execution loop, so a tool-bound LLM could return an empty .content when it chose to call
    # a tool instead of answering in prose.
    llm = await get_debate_llm()

    transcript = []
    locked = state.get("locked_moves", {})
    pending_targets = [d for d in DETECTIVE_IDS if d not in locked]

    # Mr. X possible-zone context (ISSUE-005): annotate each proposer's already-proposed
    # destinations with their hop-distance to Mr. X's possible zone, so debaters can argue
    # about whether a plan actually closes in on him, not just where it sends people.
    zone_context, zone_state_update = get_mrx_zone_context(state)
    zone_block = format_mrx_zone_block(zone_context)
    distances_to_zone = zone_context["distances_to_zone"] if zone_context else {}
    current_distances_to_zone = {
        d_id: distances_to_zone.get(d_info["node_id"])
        for d_id, d_info in state["detectives"].items()
    }
    # ISSUE-004: scoped to pending_targets only - locked targets are already shown via
    # "Already Locked Moves" below, so a proposer's full 5-target board would just duplicate them.
    annotated_proposals = build_annotated_proposals(state, distances_to_zone, pending_targets)
    proposals_context = json.dumps(annotated_proposals, indent=2)

    # ISSUE-004: every speaker's single call now also yields a structured "position" per
    # pending target (their own current preferred node, given the debate so far) alongside the
    # free-text pitch - no extra call cost, since with_structured_output still runs in one shot.
    # This is the actual post-debate signal vote_node needs; the pitch alone is a lossy
    # paraphrase of it. pending_targets is guaranteed non-empty here (the graph's router sends
    # an all-locked round to "finalize", never back to "propose"/"debate") - the empty-list
    # fallback below exists only for symmetry with vote_node's own such guard.
    structured_llm = llm.with_structured_output(build_debate_position_schema(pending_targets)) \
        if pending_targets else None
    debate_positions = {}

    # Debate is sequential (Agent Red speaks first), so this genuinely marks "the first
    # detective starts their debate" - unlike propose/vote's concurrent gather, there's no
    # earlier moment where multiple calls could already be in flight.
    get_stream_writer()({"stage": "debate"})
    for det_id in DETECTIVE_IDS:
        transcript_history = "\n".join(transcript) if transcript else "No one has spoken yet."

        prompt = f"""
        You are {AGENT_DISPLAY_NAMES[det_id]}.
        {get_psychology_prompt(state['round_number'], det_id)}

        Mr. X's Possible Zone:
        {zone_block}
        Each detective's CURRENT distance (in hops) to the nearest node in that zone, for
        comparison: {json.dumps(current_distances_to_zone)}

        Initial Proposals (pending targets only - "distance_to_mrx_zone_per_move" is that
        destination's hop-distance to the nearest node in Mr. X's possible zone above - 0 means
        it IS one of his possible current locations): {proposals_context}
        Already Locked Moves: {locked}

        Debate Transcript so far:
        {transcript_history}

        Task: If no one has spoken yet, pitch your plan aggressively. Otherwise, DO NOT just
        agree. Point out why the previous speakers' plans are bad for YOU. Counter-propose your
        own plan and demand votes. Keep your pitch to 2-3 sentences. Also state your current
        preferred node for each still-undecided detective ({agent_names(pending_targets)}), given
        everything argued so far.
        """

        if structured_llm is not None:
            try:
                position_obj = await structured_llm.ainvoke([HumanMessage(content=prompt)])
                pitch = position_obj.pitch
                debate_positions[det_id] = {
                    target_id: getattr(position_obj, f"{target_id}_position")
                    for target_id in pending_targets
                }
            except Exception as e:
                pitch = ""
                print(f"[{det_id.upper()}] Failed to produce a structured debate turn: {e}")
        else:
            response = await llm.ainvoke([HumanMessage(content=prompt)])
            pitch = response.content

        print(f"[{det_id.upper()}]: {pitch}")
        # This is what the frontend's Chat Log actually renders (agents.py streams the raw
        # transcript text verbatim), so the display name - not the internal id - belongs here.
        transcript.append(f"[{AGENT_DISPLAY_NAMES[det_id]}]: {pitch}")

    return {
        "messages": [AIMessage(content="\n".join(transcript))],
        "debate_positions": debate_positions,
        **zone_state_update,
    }

async def vote_node(state: ScotlandYardState) -> dict:
    print("\n--- VOTING PHASE ---")

    current_locked = state.get("locked_moves", {})
    pending_targets = [d for d in DETECTIVE_IDS if d not in current_locked]

    if not pending_targets:
        # Nothing left to vote on this round.
        current_loop = state.get("debate_loop_count", 0) + 1
        return {"locked_moves": {}, "debate_loop_count": current_loop}

    llm, tools = await get_detective_llm()
    # Ballot only asks about still-undecided targets - locked detectives are never
    # re-voted on, which is what actually saves tokens/call complexity each loop.
    structured_llm = llm.with_structured_output(build_ballot_schema(pending_targets))

    # Recompute legal targets (with occupancy) for still-undecided detectives so we can
    # discard any illegal vote before it reaches the tally, mirroring propose_node. Also build
    # a displayable version (legal_moves_context) so voters can see the actual candidates and
    # their distance to Mr. X's possible zone - propose_node already shows this; ballots did
    # not until now.
    valid_moves_tool = next((t for t in tools if t.name == "get_valid_moves"), None)
    legal_moves_context = {}
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
            return d_id, parse_valid_moves(moves)

        fetch_results = await asyncio.gather(*[fetch_moves(d_id) for d_id in pending_targets])
        for d_id, moves in fetch_results:
            legal_moves_context[d_id] = moves
            legal_move_sets[d_id] = {m["target_node"] for m in moves if "target_node" in m}

    # Mr. X possible-zone context (ISSUE-005): same annotation propose_node applies.
    zone_context, zone_state_update = get_mrx_zone_context(state)
    zone_block = format_mrx_zone_block(zone_context)
    distances_to_zone = zone_context["distances_to_zone"] if zone_context else {}
    for moves in legal_moves_context.values():
        for move in moves:
            move["distance_to_mrx_zone"] = distances_to_zone.get(move["target_node"])
    current_distances_to_zone = {
        d_id: distances_to_zone.get(state["detectives"][d_id]["node_id"])
        for d_id in pending_targets
    }

    # ISSUE-004: ballots previously only saw the (lossy, prose) debate transcript, never the
    # structured proposal data propose_node's and debate_node's own prompts already include.
    # Both pieces below are scoped to pending_targets, mirroring legal_moves_context above.
    annotated_proposals = build_annotated_proposals(state, distances_to_zone, pending_targets)
    proposals_context = json.dumps(annotated_proposals, indent=2)
    # Each debate speaker's structured post-debate stance (agents.py:debate_node) - the actual
    # "what did everyone land on after arguing" signal, distinct from their frozen pre-debate
    # proposal above and from the free-text transcript below.
    debate_positions_context = json.dumps(state.get("debate_positions", {}), indent=2)

    debate_transcript = state["messages"][-1].content if state["messages"] else ""

    # 1. Collect Votes - every detective votes, including ones already locked, since they
    # still have a stake in where the remaining, still-undecided detectives end up. Ballots
    # are cast simultaneously/independently against the same finished debate transcript (no
    # voter sees another's ballot), so cast all 5 concurrently instead of sequentially.
    async def cast_ballot(det_id):
        psychology_context = get_psychology_prompt(state['round_number'], det_id)

        prompt = f"""
        You are {AGENT_DISPLAY_NAMES[det_id]}.

        {psychology_context}

        Based on the debate:
        {debate_transcript}

        Structured Proposals going into this debate (pending targets only - "rationale" is the
        proposer's own reasoning, "distance_to_mrx_zone_per_move" is each destination's
        hop-distance to Mr. X's possible zone): {proposals_context}

        Each Detective's Stated Position AFTER Debate (pending targets only - what they said
        they now favor, having heard everyone's arguments): {debate_positions_context}

        Mr. X's Possible Zone:
        {zone_block}
        Each still-undecided detective's CURRENT distance (in hops) to the nearest node in that
        zone, for comparison: {json.dumps(current_distances_to_zone)}

        The only detectives who still need a vote this round are: {agent_names(pending_targets)}.
        Each candidate's legal target nodes, with "distance_to_mrx_zone" (0 means that node IS
        one of Mr. X's possible current locations; lower is generally better if you want the
        team closing in on him): {json.dumps(legal_moves_context, indent=2)}

        Task: Cast your final vote ONLY for the exact node each of those still-undecided
        detectives should move to.
        Apply your current Desperation Level to your voting strategy:
        - If LOW/MEDIUM: Be stubborn. Vote for the plan that positions YOU to catch Mr. X, even if it risks failing the vote.
        - If HIGH: Compromise. Vote for the plan with the most momentum in the debate to ensure a move passes.
        """
        try:
            ballot = await structured_llm.ainvoke([HumanMessage(content=prompt)])
            votes = {target_id: getattr(ballot, f"{target_id}_vote") for target_id in pending_targets}
            return det_id, votes, None
        except Exception as e:
            return det_id, None, e

    # Same rationale as propose_node: signal only once the actual voting LLM calls are about
    # to fire, not during the legal-move/zone-context prep above.
    get_stream_writer()({"stage": "vote"})
    ballot_results = await asyncio.gather(*[cast_ballot(det_id) for det_id in DETECTIVE_IDS])

    # Process in fixed DETECTIVE_IDS order (asyncio.gather preserves input order in its
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
    # pending_targets, i.e. DETECTIVE_IDS order), so within one voter's ballot the
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
    return {"locked_moves": newly_locked, "debate_loop_count": current_loop, **zone_state_update}
