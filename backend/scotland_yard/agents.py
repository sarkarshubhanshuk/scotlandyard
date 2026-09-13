"""
One detective's turn, as driven by graph.py's `turn` node.

Play is turn-wise (ADR-0009): Mr. X moves, then the five detectives take their turns one at a
time in DETECTIVE_IDS order. A single turn is three phases and six LLM calls:

    1. The mover proposes a destination for itself and broadcasts it.          (1 call)
    2. Every other detective responds once, sequentially, in cyclic order      (4 calls)
       starting from the mover's immediate successor. Each response is
       ADVISORY - a responder states its own current preference but reserves
       nothing and is free to decide differently on its own turn.
    3. The mover, having heard all four, commits its final destination.        (1 call)

Nothing here trusts the model's output: every node the LLM names is re-checked against a
legal-move set this module computed itself, and a failed check is resolved deterministically
rather than retried indefinitely.
"""
import asyncio
import json
import logging
from typing import Optional

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.config import get_stream_writer
from pydantic import BaseModel, Field

from .game_master import compute_distances_to_zone, compute_mrx_zone, compute_valid_moves
from .llm_client import get_debate_llm, get_detective_llm
from .rules_constants import (
    AGENT_DISPLAY_NAMES,
    COLLABORATION_TIERS,
    DETECTIVE_IDS,
    LLM_CALL_DEADLINE_SECONDS,
    MAX_ROUND,
    NUM_DETECTIVES,
)
from .state import ScotlandYardState
from .transport import determine_move_transport

logger = logging.getLogger(__name__)


def agent_names(det_ids) -> str:
    """Comma-separated display names for a list of detective ids, for prompt-text readability."""
    return ", ".join(AGENT_DISPLAY_NAMES[d] for d in det_ids)


def responders_for(mover_id: str) -> list[str]:
    """
    The order the other four detectives respond in during `mover_id`'s turn: cyclic
    DETECTIVE_IDS order starting from the mover's immediate successor, so Agent Green's turn is
    answered by Yellow, Purple, Red, Blue. Every non-mover responds exactly once.
    """
    start = DETECTIVE_IDS.index(mover_id)
    return [DETECTIVE_IDS[(start + offset) % NUM_DETECTIVES] for offset in range(1, NUM_DETECTIVES)]


def fetch_legal_moves(state: ScotlandYardState, det_ids: list, reserved_nodes: set) -> tuple[dict, dict]:
    """
    The legal target nodes each detective in `det_ids` can reach this turn, as both a
    displayable list (for prompt text) and a set (for validating LLM output against).

    `reserved_nodes` is the set of destinations already committed by detectives who have taken
    their turn earlier this round. A committed detective is still physically standing on its
    OLD node until the round is finalized, so its destination has to be excluded explicitly -
    it does not show up in the occupied-node set. Under turn-wise play this single exclusion is
    what makes two detectives sharing a destination structurally impossible; it is not a check
    applied after the fact, so there is no second code path that can drift out of sync with it
    (the failure mode that was ISSUE-025 under the old propose/vote design).

    Board legality is computed in-process via game_master.compute_valid_moves rather than
    over MCP: these are lookups this code makes on the agents' behalf, not model-initiated
    tool calls, so the stdio round-trip bought nothing. See ADR-0001.
    """
    all_detective_nodes = {d["node_id"] for d in state["detectives"].values()}
    legal_moves_context = {}
    legal_move_sets = {}
    for det_id in det_ids:
        d_info = state["detectives"][det_id]
        occupied = list((all_detective_nodes - {d_info["node_id"]}) | reserved_nodes)
        moves = compute_valid_moves(
            d_info["node_id"], d_info["taxi_tickets"], d_info["bus_tickets"],
            d_info["metro_tickets"], black_tickets=0, occupied_nodes=occupied,
        )
        legal_moves_context[det_id] = moves
        legal_move_sets[det_id] = {m["target_node"] for m in moves if "target_node" in m}
    return legal_moves_context, legal_move_sets


def annotate_zone_distances(legal_moves_context: dict, distances_to_zone: dict) -> None:
    """Adds each candidate move's hop-distance to Mr. X's possible zone, in place."""
    for moves in legal_moves_context.values():
        for move in moves:
            move["distance_to_mrx_zone"] = distances_to_zone.get(move["target_node"])


def projected_occupied_nodes(state: ScotlandYardState, exclude_det_id: str, committed: dict) -> list:
    """
    Where the other four detectives will be standing once this round is applied: their committed
    destination if they have already taken their turn, otherwise their current node.

    This is the best available projection of next round's board. Detectives do not physically
    move until resolve_round, so state["detectives"] alone would understate the constraint for
    anyone who has already committed.
    """
    return [
        committed.get(det_id, info["node_id"])
        for det_id, info in state["detectives"].items()
        if det_id != exclude_det_id
    ]


def annotate_onward_options(
    state: ScotlandYardState, det_id: str, legal_moves_context: dict, committed: dict
) -> None:
    """
    Adds "onward_moves_after" to every candidate: how many distinct nodes `det_id` would still
    be able to reach NEXT round from that destination, with the ticket it would have spent
    getting there already deducted.

    This is what keeps a detective from stranding itself - a candidate whose
    onward_moves_after is 0 is a dead end, and one that costs the last metro ticket to reach a
    metro-only junction shows up as a sharply lower number than its neighbours. Computed
    deterministically here rather than left to the model to reason out, for the same reason
    distance_to_mrx_zone is (ISSUE-005): it is a cheap board lookup, and asking a small model
    to simulate ticket arithmetic is exactly the kind of thing it gets quietly wrong.

    Which ticket a move spends is resolved by transport.determine_move_transport - the same
    helper resolve_round uses to actually deduct it - so this can never disagree with what the
    move will really cost. Costs one integer per candidate in prompt text.
    """
    detective = state["detectives"][det_id]
    occupied_now = projected_occupied_nodes(state, det_id, committed)
    onward_cache: dict[int, Optional[int]] = {}

    for move in legal_moves_context.get(det_id, []):
        target = move.get("target_node")
        if target is None:
            continue
        if target not in onward_cache:
            transport = determine_move_transport(detective, target, occupied_now)
            if transport is None:
                onward_cache[target] = None
            else:
                remaining = dict(detective)
                remaining[f"{transport}_tickets"] -= 1
                onward = compute_valid_moves(
                    target, remaining["taxi_tickets"], remaining["bus_tickets"],
                    remaining["metro_tickets"], black_tickets=0,
                    # This detective has vacated its old node by then, so only the other four
                    # constrain it - projected forward to where they will actually be.
                    occupied_nodes=occupied_now,
                )
                onward_cache[target] = len({m["target_node"] for m in onward if "target_node" in m})
        move["onward_moves_after"] = onward_cache[target]


# --- STRUCTURED LLM OUTPUT SCHEMAS ---
# Fixed, not built per call. Under the old simultaneous design every call had to name a node
# for all five detectives, so the schemas were generated dynamically from whichever ones were
# still undecided. Turn-wise play means each call decides exactly one node, which makes these
# static - and makes each structured response small enough that the reasoning-budget
# truncation failure documented in ISSUE-006/007 has far less room to occur.
class MoveChoice(BaseModel):
    """The mover's own destination, used for both its opening proposal and its final decision."""
    target_node: int = Field(description="The node YOU will move to. Must be one of your legal moves.")
    rationale: str = Field(description="Crisp rationale - 2-3 sentences - for this destination")


class TurnResponseChoice(BaseModel):
    """One non-mover's advisory answer to the mover's proposal."""
    response: str = Field(
        description="Your 2-3 sentence response to the proposal on the table - agree, or say why it is wrong"
    )
    preferred_node: int = Field(
        description="The node YOU would take on your own turn, given everything argued so far. Must be one of YOUR legal moves."
    )


# --- MR. X POSSIBLE-ZONE CONTEXT (docs/issues/known_issues.md ISSUE-005) ---
# Server-side board-topology computation injected directly as prompt text - no extra
# LLM/tool round-trip, which matters more than ever now that every call in a round is
# sequential and pays its latency directly (ISSUE-009, ADR-0009).
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

    Still ticket-blind - the zone is a strict superset of where Mr. X could really be, because
    the BFS does not check that his remaining inventory could actually pay for a given path.
    See ISSUE-015; narrowing this using his travel log is deliberately a separate change.
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
    Memoizes compute_mrx_zone_context() for the current round.

    Its inputs (last_known_node/round, round_number, the detectives' occupied nodes) are
    identical for all 30 of a round's LLM calls: detectives commit destinations during their
    turns but do not physically move until resolve_round, so state["detectives"] is frozen for
    the whole round. Without this, each of the five turns would re-run the same pair of BFS
    passes. Presence of "mrx_zone_context" on state (not just truthiness - it's legitimately
    None pre-reveal) is what marks it as already computed this round; build_next_round_state
    omits the key so each new round starts with a cache miss.

    Returns (zone_context, state_update): callers must merge state_update into their own
    returned dict so later turns in the same round see the cached value instead of recomputing.
    """
    if "mrx_zone_context" in state:
        return state["mrx_zone_context"], {}
    zone_context = compute_mrx_zone_context(state)
    return zone_context, {"mrx_zone_context": zone_context}

def format_mrx_zone_block(zone_context: Optional[dict]) -> str:
    """
    Renders compute_mrx_zone_context()'s result into the fixed prompt block every call in a turn
    shares. Below MRX_ZONE_LIST_THRESHOLD nodes, the zone is small enough to be a useful,
    specific hint, so it's listed outright; above it, only the count is shown - past that size
    the zone covers most of the board anyway, and enumerating it would just be token cost with
    no real narrowing-down value.
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


# --- PSYCHOLOGY & GOALS INJECTOR ---
# The three motivations are unchanged from the simultaneous-debate design; what changed is the
# ladder underneath them. It used to be a 3-step DESPERATION scale whose job was to make the
# vote converge before the loop cap. There is no vote and no loop cap any more, so the ladder
# now expresses COLLABORATION TENDENCY instead: how much weight a detective gives a teammate's
# argument relative to its own Selfish Glory goal. See ADR-0009.
COLLABORATION_BEHAVIOR = {
    "MINIMUM": (
        "You are arrogant and certain, and it is far too early to share the glory. Treat a "
        "teammate's argument as noise unless it proves your own plan is illegal or suicidal."
    ),
    "LOW": (
        "You are still chasing personal glory. Push back hard on any plan that does not set "
        "YOU up for the catch, and say so plainly."
    ),
    "MEDIUM": (
        "The net is tightening. Genuinely weigh a teammate's argument against your own read, "
        "and coordinate where doing so costs you little."
    ),
    "HIGH": (
        "Time is running short. Defer to a teammate's plan unless you have a concrete, "
        "board-based reason it fails."
    ),
    "MAXIMUM": (
        "Panic. Deprioritize your selfish goal almost entirely and back whatever plan best "
        "guarantees Mr. X is trapped, whoever gets to land on him."
    ),
}


def get_collaboration_tier(round_number: int) -> tuple[int, str]:
    """
    (percentage, label) for the given round, from rules_constants.COLLABORATION_TIERS.

    Rounds past MAX_ROUND cannot occur in a real game (resolve_round ends it at 24), but the
    final tier is returned for any such value rather than raising - a prompt helper is the
    wrong place to enforce a rules invariant.
    """
    for upper_bound, percentage, label in COLLABORATION_TIERS:
        if round_number <= upper_bound:
            return percentage, label
    return COLLABORATION_TIERS[-1][1], COLLABORATION_TIERS[-1][2]


def get_psychology_prompt(round_number: int, det_id: str) -> str:
    """Injects the 3 goals plus the round's collaboration tendency, stated as a literal number."""
    percentage, label = get_collaboration_tier(round_number)

    return f"""
    YOUR 3 MOTIVATIONS:
    1. MOST IMPORTANT: Catch Mr. X (Team Win).
    2. 2ND IMPORTANT: YOU ({AGENT_DISPLAY_NAMES[det_id]}) must be the one who lands on him (Selfish Glory).
    3. 3RD IMPORTANT: Catch him in the fewest turns possible (Efficiency).

    COLLABORATION TENDENCY: {label} ({percentage}%). Round {round_number} of {MAX_ROUND}.
    {COLLABORATION_BEHAVIOR[label]}
    Weigh a teammate's argument at roughly {percentage}% against your own read of the board.

    STRICT RULE: NO TWO DETECTIVES CAN OCCUPY THE SAME NODE. Never name a node another
    detective is already standing on or has already committed to this round.

    STRICT RULE: Use ONLY the data given to you in this prompt (board state, legal moves, Mr.
    X's possible zone, distances). Do not attempt to call any tool, browse, or otherwise seek
    outside information about Scotland Yard, the board, or Mr. X - everything you need has
    already been provided.
    """


# --- SHARED PROMPT BLOCKS ---

def format_board_block(state: ScotlandYardState, zone_block: str, committed: dict) -> str:
    """
    The situation block every one of a turn's six calls opens with, so the mover and the four
    responders are demonstrably reasoning about the same board rather than two hand-built views
    that can drift apart.
    """
    return f"""
        Board State: {json.dumps(state["detectives"], indent=2)}
        Mr. X's Ticket Log (transport types used so far - NOT his location): {state['mr_x']['transport_history']}

        Mr. X's Possible Zone:
        {zone_block}

        Moves Already Committed This Round (FINAL - those detectives have taken their turn; no
        one else may be sent to their destinations): {json.dumps(committed, indent=2)}
    """


def format_options_block(det_id: str, state: ScotlandYardState, legal_moves_context: dict,
                         distances_to_zone: dict) -> str:
    """
    One detective's own position, tickets, and annotated candidate destinations.

    Each candidate carries two deterministic annotations the model is told how to read:
    "distance_to_mrx_zone" (0 means the node IS one of Mr. X's possible current locations) and
    "onward_moves_after" (how many moves would remain from there next round, after paying for
    this one - a 0 is a dead end).
    """
    detective = state["detectives"][det_id]
    tickets = {k: v for k, v in detective.items() if k.endswith("_tickets")}
    return f"""
        {AGENT_DISPLAY_NAMES[det_id]} is standing on Node {detective['node_id']} \
({distances_to_zone.get(detective['node_id'])} hops from the nearest node in Mr. X's zone).
        {AGENT_DISPLAY_NAMES[det_id]}'s remaining tickets: {json.dumps(tickets)}
        {AGENT_DISPLAY_NAMES[det_id]}'s ONLY legal destinations this turn (this list already
        excludes nodes occupied by another detective and nodes committed earlier this round):
        {json.dumps(legal_moves_context.get(det_id, []), indent=2)}
    """


# --- LLM CALL PLUMBING ---

async def _invoke(structured_llm, prompt: str, label: str):
    """
    One structured LLM call, bounded by a real wall-clock deadline, returning None on any
    failure so the caller can resolve the outcome deterministically.

    llm_client.py's `timeout=45` is enforced by the HTTP client as an IDLE-GAP timeout, reset
    by every streamed chunk - it kills a genuinely stuck call but not a merely slow one, and
    calls lasting many minutes have been observed to complete without tripping it (ISSUE-009).
    That was tolerable while propose/vote fired five calls concurrently and a straggler
    overlapped its siblings. Turn-wise play makes all 30 of a round's calls sequential, so one
    slow call adds its full duration to the round. asyncio.wait_for is the wall-clock cap the
    idle-gap timeout never was.

    Timeouts and errors are logged at WARNING, not raised: a round must always produce a legal
    move for every detective, and every caller here has a deterministic fallback.
    """
    try:
        return await asyncio.wait_for(
            structured_llm.ainvoke([HumanMessage(content=prompt)]),
            timeout=LLM_CALL_DEADLINE_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.warning("[TIMEOUT] %s exceeded the %ds per-call deadline - falling back.",
                       label, LLM_CALL_DEADLINE_SECONDS)
        return None
    except Exception as e:
        logger.warning("[LLM ERROR] %s failed: %s", label, e)
        return None


def _enforce_legal_node(proposed_node: Optional[int], det_id: str, legal_set: set,
                        state: ScotlandYardState, context: str) -> int:
    """
    Deterministic backup enforcement: the node this detective will actually be recorded as
    choosing, whatever the model said.

    Never trusts the LLM's raw output - a small model can still ignore a prompt instruction,
    and under turn-wise play an out-of-bounds node would otherwise flow straight into
    committed_moves with no vote tally left to discard it. `legal_set` already excludes every
    destination committed earlier this round, so the occupancy rule is enforced here too.

    Falls back to the detective's own lowest-numbered legal move rather than leaving it
    stationary: a detective must move whenever a legal move is available to it. Staying put is
    only correct when it genuinely has none.
    """
    if proposed_node is not None and proposed_node in legal_set:
        return proposed_node

    remaining = sorted(legal_set)
    if remaining:
        logger.warning(
            "[VALIDATION] %s's %s (Node %s) is not one of its legal moves %s. Reassigned to Node %s.",
            det_id, context, proposed_node, remaining, remaining[0])
        return remaining[0]

    current_node = state["detectives"][det_id]["node_id"]
    logger.warning(
        "[VALIDATION] %s's %s (Node %s) is illegal and it has no free legal move to fall back "
        "to. Staying at Node %s.", det_id, context, proposed_node, current_node)
    return current_node


async def _choose_move(structured_llm, prompt: str, det_id: str, legal_set: set,
                       state: ScotlandYardState, phase: str) -> tuple[int, str]:
    """
    A mover's node choice (proposal or final decision), with one self-correction retry before
    deterministic enforcement takes over.

    The retry is best-effort and only reduces how often `_enforce_legal_node` has to intervene;
    it is not what guarantees correctness. It costs one extra sequential call when it fires,
    which is why it only fires on an actually-illegal answer.
    """
    label = f"{det_id} {phase}"
    choice = await _invoke(structured_llm, prompt, label)

    if choice is not None and choice.target_node not in legal_set and legal_set:
        retry_prompt = prompt + f"""

        YOUR PREVIOUS ANSWER (Node {choice.target_node}) IS NOT A LEGAL MOVE FOR YOU.
        Choose again, using ONLY the legal destinations listed above: {sorted(legal_set)}.
        """
        retried = await _invoke(structured_llm, retry_prompt, f"{label} (retry)")
        if retried is not None:
            choice = retried

    node = _enforce_legal_node(
        choice.target_node if choice is not None else None, det_id, legal_set, state, phase,
    )
    rationale = choice.rationale if choice is not None else "No rationale (the call failed)."
    return node, rationale


# --- THE TURN NODE ---

async def turn_node(state: ScotlandYardState) -> dict:
    """
    Runs the whole turn for whichever detective `state["turn_index"]` points at, and commits
    its move.

    Emits a custom stream event per LLM call rather than one per node. Under the old design a
    stage's five calls landed on the client as a single blob once the last of them finished;
    here each of the six calls is surfaced the moment it completes, which is what lets the
    Chat Log read as a conversation unfolding rather than three bursts per loop.
    """
    mover = DETECTIVE_IDS[state["turn_index"]]
    mover_name = AGENT_DISPLAY_NAMES[mover]
    committed = dict(state.get("committed_moves", {}))
    round_number = state["round_number"]
    logger.info("--- ROUND %d: %s'S TURN (%d/%d) ---",
                round_number, mover_name.upper(), state["turn_index"] + 1, NUM_DETECTIVES)

    writer = get_stream_writer()
    reserved = set(committed.values())

    zone_context, zone_state_update = get_mrx_zone_context(state)
    zone_block = format_mrx_zone_block(zone_context)
    distances_to_zone = zone_context["distances_to_zone"] if zone_context else {}

    # Options are needed for the mover AND for every responder that has not moved yet, since a
    # responder argues about its own next move as well as the mover's. Detectives that already
    # committed are excluded: they have no move left to make this round, so computing options
    # for them would be both wasted work and actively misleading to put in front of them.
    # One pass, so all six calls see the same board.
    still_to_move = [det_id for det_id in DETECTIVE_IDS if det_id not in committed]
    legal_moves_context, legal_move_sets = fetch_legal_moves(state, still_to_move, reserved)
    annotate_zone_distances(legal_moves_context, distances_to_zone)
    for det_id in still_to_move:
        annotate_onward_options(state, det_id, legal_moves_context, committed)

    board_block = format_board_block(state, zone_block, committed)
    mover_options = format_options_block(mover, state, legal_moves_context, distances_to_zone)

    # --- PHASE 1: the mover proposes, and broadcasts it ---
    writer({"event": "turn_started", "detective": mover})
    detective_llm = await get_detective_llm()
    move_llm = detective_llm.with_structured_output(MoveChoice)

    proposal_prompt = f"""
        You are {mover_name}. It is YOUR turn to move this round.
        {get_psychology_prompt(round_number, mover)}
        {board_block}
        {mover_options}

        Task: Choose the destination YOU will move to, and explain why in 2-3 sentences. This
        is a PROPOSAL you are broadcasting to the rest of the team - they will each respond to
        it, and you will get to revise it afterwards. Prefer destinations that close the
        distance to Mr. X's possible zone ("distance_to_mrx_zone"; 0 means that node IS one of
        his possible locations) without stranding yourself ("onward_moves_after" is how many
        moves you would have left next round - never pick 0 if you have an alternative).
        YOU MUST CHOOSE FROM THE LEGAL DESTINATIONS LISTED ABOVE.
    """
    proposed_node, proposal_rationale = await _choose_move(
        move_llm, proposal_prompt, mover, legal_move_sets[mover], state, "proposal")
    logger.info("[%s PROPOSAL] Node %s - %s", mover.upper(), proposed_node, proposal_rationale)
    writer({
        "event": "turn_proposal", "detective": mover,
        "target_node": proposed_node, "rationale": proposal_rationale,
    })

    # --- PHASE 2: the other four respond, sequentially ---
    debate_llm = await get_debate_llm()
    response_llm = debate_llm.with_structured_output(TurnResponseChoice)
    responses = []
    transcript = []

    for responder in responders_for(mover):
        responder_name = AGENT_DISPLAY_NAMES[responder]
        # A responder that already took its turn this round has nothing left to decide. Showing
        # it a menu of destinations and asking what it "would take on its own turn" would be
        # straightforwardly false - its turn is over and its node is locked in. It still gets a
        # voice on where the mover goes, which is the point of it being asked at all.
        has_moved = responder in committed
        if has_moved:
            own_position_block = (
                f"        You have ALREADY taken your turn this round and committed to Node "
                f"{committed[responder]}. That is final - you cannot change it, and you have no "
                f"move left to make. Argue from where you will actually be standing."
            )
            preference_task = (
                f"Answer with Node {committed[responder]} - your own committed node - as your "
                "preferred_node, since your move is already settled."
            )
        else:
            own_position_block = format_options_block(
                responder, state, legal_moves_context, distances_to_zone)
            preference_task = (
                "Then state which node YOU would take on your own turn, chosen from YOUR legal "
                "destinations above. Your answer is advisory: it reserves nothing and you may "
                "decide differently when your own turn comes."
            )
        # Only what was said EARLIER IN THIS TURN (ADR-0009): each turn's transcript starts
        # fresh, so the last responder's prompt is no larger than the first's and per-round
        # token cost stays flat across all five turns. What earlier turns decided is still
        # visible - as committed moves on the board, which is the part that actually binds.
        transcript_so_far = "\n".join(transcript) if transcript else "You are the first to respond."

        response_prompt = f"""
        You are {responder_name}. It is {mover_name}'s turn to move, not yours - you are
        responding to their proposal.
        {get_psychology_prompt(round_number, responder)}
        {board_block}

        {mover_name.upper()}'S PROPOSAL: move to Node {proposed_node}.
        Their reasoning: {proposal_rationale}
        The full set of destinations {mover_name} could have chosen from:
        {json.dumps(legal_moves_context.get(mover, []), indent=2)}

        YOUR OWN POSITION (for deciding what YOU would do next):
        {own_position_block}

        Responses already given to {mover_name} this turn:
        {transcript_so_far}

        Task: Respond to {mover_name}'s proposal in 2-3 sentences - back it, or say concretely
        why a different destination from their list would serve the team (or you) better. Do
        not simply repeat what an earlier responder said. {preference_task}
        """

        answer = await _invoke(response_llm, response_prompt, f"{responder} response to {mover}")
        if answer is None:
            text = "(no response - the call failed)"
            preferred = None
        else:
            text = answer.response
            preferred = answer.preferred_node
            if has_moved:
                # Nothing for it to state - its move is already settled. Its own committed node
                # is the truth regardless of what it answered, so use that rather than trusting
                # the model to echo it back correctly.
                preferred = committed[responder]
            elif preferred not in legal_move_sets.get(responder, set()):
                # An advisory preference is discarded rather than reassigned when it is illegal.
                # The old vote tally took the same line with a bad ballot entry: rewriting it to
                # the lowest-numbered legal node would manufacture an opinion the detective never
                # held, and that invented node would then feed into the mover's decision prompt.
                logger.warning(
                    "[VALIDATION] %s's stated preference (Node %s) is not one of its legal "
                    "moves and was dropped from the transcript.", responder, preferred)
                preferred = None

        responses.append({"responder": responder, "response": text, "preferred_node": preferred})
        if preferred is None:
            intent = ""
        elif has_moved:
            intent = f" (already committed to Node {preferred})"
        else:
            intent = f" (would take Node {preferred} itself)"
        transcript.append(f"[{responder_name}]: {text}{intent}")
        logger.info("[%s -> %s] %s%s", responder.upper(), mover.upper(), text, intent)
        writer({
            "event": "turn_response", "detective": responder, "responding_to": mover,
            "response": text, "preferred_node": preferred,
        })

    # --- PHASE 3: the mover decides ---
    decision_prompt = f"""
        You are {mover_name}. It is YOUR turn to move, and you must now commit.
        {get_psychology_prompt(round_number, mover)}
        {board_block}
        {mover_options}

        YOUR OWN PROPOSAL WAS: Node {proposed_node} - {proposal_rationale}

        WHAT THE REST OF THE TEAM SAID ABOUT IT:
        {chr(10).join(transcript)}

        Task: Commit to the destination you will actually move to, and explain why in 2-3
        sentences. Keep your original proposal or change it - apply your COLLABORATION
        TENDENCY above when deciding how much weight the responses deserve against your own
        read. This decision is FINAL and takes effect immediately; the detectives who have not
        moved yet will have to work around it.
        YOU MUST CHOOSE FROM THE LEGAL DESTINATIONS LISTED ABOVE.
    """
    committed_node, decision_rationale = await _choose_move(
        move_llm, decision_prompt, mover, legal_move_sets[mover], state, "final decision")

    occupied = projected_occupied_nodes(state, mover, committed)
    transport = determine_move_transport(state["detectives"][mover], committed_node, occupied)
    logger.info("[%s COMMITTED] Node %s via %s - %s",
                mover.upper(), committed_node, transport, decision_rationale)
    writer({
        "event": "turn_decision", "detective": mover,
        "from_node": state["detectives"][mover]["node_id"], "target_node": committed_node,
        "transport": transport, "rationale": decision_rationale,
    })

    turn_record = {
        "proposed_node": proposed_node,
        "proposal_rationale": proposal_rationale,
        "responses": responses,
        "committed_node": committed_node,
        "decision_rationale": decision_rationale,
    }
    turn_summary = "\n".join(
        [f"[{mover_name} proposes Node {proposed_node}]: {proposal_rationale}"]
        + transcript
        + [f"[{mover_name} commits to Node {committed_node}]: {decision_rationale}"]
    )

    return {
        "committed_moves": {mover: committed_node},
        "turn_records": {mover: turn_record},
        "turn_index": state["turn_index"] + 1,
        "messages": [AIMessage(content=turn_summary)],
        **zone_state_update,
    }
