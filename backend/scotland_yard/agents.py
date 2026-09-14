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

The move is then APPLIED immediately - the detective physically moves, its ticket transfers to
Mr. X, and capture is checked - so the next detective deliberates against a board that already
reflects it (rules.md section 2: "A full Round consists of Mr. X moving first, followed by
Detectives 1 through 5 moving in sequential order", and "At the start of each turn, the active
AI agent receives the current board state"). The turn then waits for the client to finish
animating that pawn before returning, so the next detective does not start deliberating over a
board that is still visibly rearranging itself. See ADR-0010.

Nothing here trusts the model's output: every node the LLM names is re-checked against a
legal-move set this module computed itself, and a failed check is resolved deterministically
rather than retried indefinitely.
"""
import asyncio
import json
import logging
from typing import Optional

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.config import get_stream_writer
from pydantic import BaseModel, Field

from .game_master import (
    compute_distances_to_zone,
    compute_mrx_zone,
    compute_mrx_zone_from_tickets,
    compute_valid_moves,
    project_zone_one_hop,
)
from .limits import record_llm_call
from .llm_client import get_debate_llm, get_detective_llm
from .rules_constants import (
    AGENT_DISPLAY_NAMES,
    COLLABORATION_TIERS,
    DETECTIVE_IDS,
    LLM_CALL_DEADLINE_SECONDS,
    MAX_ROUND,
    NUM_DETECTIVES,
    SURFACING_ROUNDS,
    TURN_ACK_TIMEOUT_SECONDS,
)
from .state import ScotlandYardState
from .transport import determine_move_transport
from .travel_log import hops_since_surfacing

logger = logging.getLogger(__name__)


def agent_names(det_ids) -> str:
    """Comma-separated display names for a list of detective ids, for prompt-text readability."""
    return ", ".join(AGENT_DISPLAY_NAMES[d] for d in det_ids)


def responders_for(mover_id: str) -> list[str]:
    """
    The order the other four detectives respond in during `mover_id`'s turn: cyclic
    DETECTIVE_IDS order starting from the mover's immediate successor, so Agent Green's turn is
    answered by Orange, Purple, Red, Blue. Every non-mover responds exactly once.
    """
    start = DETECTIVE_IDS.index(mover_id)
    return [DETECTIVE_IDS[(start + offset) % NUM_DETECTIVES] for offset in range(1, NUM_DETECTIVES)]


def fetch_legal_moves(state: ScotlandYardState, det_ids: list) -> tuple[dict, dict]:
    """
    The legal target nodes each detective in `det_ids` can reach right now, as both a
    displayable list (for prompt text) and a set (for validating LLM output against).

    Occupancy is simply every OTHER detective's current node. That is the whole collision
    guarantee, and it is exact rather than approximate because a detective's move is applied
    the moment its turn ends (ADR-0010): by the time the next detective asks this question, an
    earlier mover is genuinely standing on its new node and has genuinely vacated its old one.

    This used to need a `reserved_nodes` argument. Moves were committed during the turn but not
    applied until the round finalized, so a committed detective was still physically on its old
    node and its destination had to be excluded by hand - and that node stayed blocked for the
    rest of the round even though nobody was on it any more, which rules.md never asked for.
    Applying per turn removes both the extra argument and that phantom occupancy.

    Board legality is computed in-process via game_master.compute_valid_moves rather than
    over MCP: these are lookups this code makes on the agents' behalf, not model-initiated
    tool calls, so the stdio round-trip bought nothing. See ADR-0001.
    """
    all_detective_nodes = {d["node_id"] for d in state["detectives"].values()}
    legal_moves_context = {}
    legal_move_sets = {}
    for det_id in det_ids:
        d_info = state["detectives"][det_id]
        occupied = list(all_detective_nodes - {d_info["node_id"]})
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


def other_detective_nodes(state: ScotlandYardState, exclude_det_id: str) -> list:
    """
    Where the other four detectives are standing right now.

    Positions are live under per-turn application (ADR-0010), so this needs no projection: a
    detective that has already moved this round is already on its new node. It previously had
    to merge in `committed_moves` by hand, because a committed detective had not physically
    moved yet.
    """
    return [
        info["node_id"]
        for det_id, info in state["detectives"].items()
        if det_id != exclude_det_id
    ]


def annotate_onward_options(
    state: ScotlandYardState, det_id: str, legal_moves_context: dict
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
    occupied_now = other_detective_nodes(state, det_id)
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
                    # constrain it. Detectives later in the turn order will move again before
                    # this lookahead comes true, so it is a snapshot, not a guarantee.
                    occupied_nodes=occupied_now,
                )
                onward_cache[target] = len({m["target_node"] for m in onward if "target_node" in m})
        move["onward_moves_after"] = onward_cache[target]


def annotate_zone_shrink(
    state: ScotlandYardState, det_id: str, legal_moves_context: dict, zone_nodes: list
) -> None:
    """
    Adds "zone_size_after" to every candidate: how many nodes Mr. X could be on NEXT round if
    this detective takes that destination - lower means more of his escape space is closed off.

    This is the metric that actually expresses "converge on him". Distance alone does not:
    five detectives each minimising their own hop-distance all pile onto whichever side of the
    zone faces them, and he simply leaves by the other (the convergence-not-triangulation trap
    already recorded in known_issues.md ISSUE-016). Standing ON an exit is what shrinks the
    zone, and the shrink is directly measurable, so it is measured here rather than described
    to the model in words it would have to apply geometrically.

    Untyped by necessity - his next ticket has not been played yet, so there is nothing to
    filter by. See project_zone_one_hop.

    **Dropped entirely when every candidate scores the same.** That is the common case for a
    detective still several hops out, where nowhere it can stand this round touches his space
    at all, and a column of identical numbers is prompt weight that carries no decision.
    """
    if not zone_nodes:
        return

    moves = legal_moves_context.get(det_id, [])
    others = set(other_detective_nodes(state, det_id))
    shrink_cache: dict[int, int] = {}

    for move in moves:
        target = move.get("target_node")
        if target is None:
            continue
        if target not in shrink_cache:
            shrink_cache[target] = len(
                project_zone_one_hop(zone_nodes, blocked_nodes=others | {target}))
        move["zone_size_after"] = shrink_cache[target]

    if len(set(shrink_cache.values())) <= 1:
        for move in moves:
            move.pop("zone_size_after", None)


def annotate_revisits(
    state: ScotlandYardState, det_id: str, legal_moves_context: dict
) -> bool:
    """
    Flags candidates this detective has recently vacated, and reports whether any exist.

    A detective has no other memory of its own movement (state.py:recent_positions explains
    why), so without this, stepping back to the node it just left looks exactly as attractive
    as it did the first time - which is how two nodes turn into a shuttle. The flag is only set
    on the offending candidates; absence means "not a revisit", and a caller that gets False
    back leaves the whole explanation out of the prompt rather than explaining a rule that
    nothing this turn can break.
    """
    recent = set((state.get("recent_positions") or {}).get(det_id) or [])
    if not recent:
        return False

    flagged = False
    for move in legal_moves_context.get(det_id, []):
        if move.get("target_node") in recent:
            move["you_were_here_recently"] = True
            flagged = True
    return flagged


def sort_candidates(legal_moves_context: dict) -> None:
    """
    Orders every detective's candidate list best-first, in place.

    Costs nothing - no tokens, no latency, no extra computation - and small models weight what
    they read first. The list previously came out in map.json's own connection order, which
    carries no meaning at all. Ties break on node id purely so the ordering is reproducible.
    """
    for moves in legal_moves_context.values():
        moves.sort(key=lambda move: (
            # An unreachable candidate (no path to the zone) sorts last rather than crashing.
            move.get("distance_to_mrx_zone") if move.get("distance_to_mrx_zone") is not None else 1_000,
            move.get("zone_size_after", 0),          # absent (undiscriminating) == neutral
            -(move.get("onward_moves_after") or 0),  # more onward options first
            move.get("target_node", 0),
        ))


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

# Only ever applied to the untyped FALLBACK ball. The typed walk needs no cap: every one of its
# levels is filtered to the single connection type that hop's ticket actually pays for, so the
# set grows far more slowly (measured on the real board: 35 nodes at 4 hops against the untyped
# ball's 88, which is where this cap's original "~84% of the board" saturation argument came
# from). The cap cannot be applied to a typed walk anyway - "the last 4 tickets" is meaningless
# without knowing which set they were spent FROM.
UNTYPED_ZONE_HOP_CAP = 4

def compute_mrx_zone_context(state: ScotlandYardState) -> Optional[dict]:
    """
    Everywhere Mr. X could plausibly be standing right now, and the hop-distance from every
    board node to the nearest such node. Returns None during rounds 1-2, before he has ever
    surfaced (no last-known node to search from).

    Narrowed by his own travel log: the ticket spent on every hop is public and exact, so
    `compute_mrx_zone_from_tickets` walks one typed layer per hop rather than an untyped ball
    (see that function for why this is not the ISSUE-015 approach). On the surfacing round
    itself this resolves to the single node he is actually standing on, because the detectives
    move after he does and he has not moved since.

    Falls back to the untyped, hop-capped ball whenever the log cannot be reconciled with the
    round number (`hops_since_surfacing` returns None) or the typed walk dead-ends. That is
    logged at WARNING because it should never happen in a healthy game - but the fallback has
    to exist and has to be the WIDER set, since this zone is used to rule locations OUT.

    Recomputed at the start of every turn rather than memoized once per round. Its occupancy
    input is not constant across a round: detectives physically move as their turns end
    (ADR-0010), so the nodes the walk is blocked through change five times per round. A
    round-start snapshot would show a later detective paths blocked through nodes its teammates
    had since vacated. Ten graph walks per round is nothing next to 30 LLM calls.
    """
    mr_x = state["mr_x"]
    last_known_node = mr_x.get("last_known_node")
    last_known_round = mr_x.get("last_known_round")
    if last_known_node is None:
        return None

    round_number = state["round_number"]
    turns_since_surfacing = round_number - last_known_round
    occupied = {d["node_id"] for d in state["detectives"].values()}

    tickets = hops_since_surfacing(
        mr_x.get("transport_history", []), last_known_round, round_number)
    zone_nodes = (
        compute_mrx_zone_from_tickets(last_known_node, tickets, occupied_nodes=occupied)
        if tickets is not None else None
    )

    if zone_nodes is None:
        logger.warning(
            "[ZONE FALLBACK] Could not narrow Mr. X's zone from his travel log %s "
            "(last seen Node %s in round %s, now round %s) - falling back to the untyped ball.",
            mr_x.get("transport_history"), last_known_node, last_known_round, round_number)
        tickets = None
        zone_nodes = set(compute_mrx_zone(
            last_known_node,
            min(turns_since_surfacing, UNTYPED_ZONE_HOP_CAP),
            occupied_nodes=occupied,
        ).keys())

    return {
        "last_known_node": last_known_node,
        "last_known_round": last_known_round,
        "turns_since_surfacing": turns_since_surfacing,
        # The exact ticket sequence the zone was narrowed by - None when the fallback is in use.
        "tickets_since_surfacing": tickets,
        "zone_nodes": sorted(zone_nodes),
        "distances_to_zone": compute_distances_to_zone(zone_nodes),
    }

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

    # Stating what the narrowing is BASED on, not just its result: the difference between "he
    # might be in these 4 nodes" and "he spent a bus ticket, so he must be on one of the 4 bus
    # connections" is the difference between a number to trust and a number to argue with.
    tickets = zone_context["tickets_since_surfacing"]
    if tickets is None:
        ticket_line = ""
    elif not tickets:
        ticket_line = (
            "He has NOT MOVED since that sighting - the node below is his exact, confirmed "
            "current position.\n        "
        )
    else:
        ticket_line = (
            f"Tickets he has spent since then, in order: {', '.join(tickets)}. The possible "
            "locations below are already narrowed to the nodes those exact connection types "
            "can reach - you do not need to work that out yourself.\n        "
        )

    return (
        f"Last seen: Node {zone_context['last_known_node']} (Round {zone_context['last_known_round']}). "
        f"Turns elapsed since then: {zone_context['turns_since_surfacing']}.\n"
        f"        {ticket_line}{location_line}"
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


# --- INTEL FRESHNESS LADDER ---
# How much Mr. X's location is actually WORTH knowing swings on a fixed 5-round cycle, and the
# right behaviour swings with it. The two rounds before a reveal, closing distance chases an
# estimate that is about to be thrown away, so flexibility is worth more. The reveal round and
# the one after it are the opposite: the zone is at its tightest it will ever be, every round
# of hesitation widens it again, and this is the only window where converging can actually
# corner him. Everything in between is the balanced default.
#
# Deliberately prompt emphasis rather than new machinery: "onward_moves_after" and
# "zone_size_after" already measure flexibility and containment respectively, so these tiers
# only change which of the numbers already on the table a detective should be weighing.
# Gated purely on round_number (project owner's decision), not on any detective's own distance.

# Keyed by how many rounds have passed SINCE the reveal - 0 is the surfacing round itself.
SURFACING_RECENCY_GUIDANCE = {
    0: (
        'MR. X HAS JUST SURFACED. His possible zone above is his EXACT node - he has not moved '
        'since, and he moves before you next round. This is the single best chance of this '
        'whole cycle and it will not come again for five rounds. CONVERGE ON HIM: take the '
        'destination with the lowest "distance_to_mrx_zone", and where that ties, the lowest '
        '"zone_size_after" (it closes off more of his escape routes). Do NOT move away from him '
        'to reposition, and do not trade closing distance for your own flexibility this round - '
        'every node you fail to close now is a node he escapes through next round.'
    ),
    1: (
        'Mr. X surfaced only one round ago and his possible zone is still tightly narrowed, so '
        'the team is still close to him. KEEP CLOSING: prioritise the lowest '
        '"distance_to_mrx_zone" and, where it ties, the lowest "zone_size_after". His zone '
        'widens every single round from here, so ground given up now cannot be recovered - do '
        'not drift off to reposition while the trail is still this warm.'
    ),
}

# Keyed by how many rounds REMAIN until the next reveal - 1 is the round immediately before it.
SURFACING_PROXIMITY_GUIDANCE = {
    # 1 round out: the reveal lands before this detective's NEXT turn, so today's zone
    # progress is about to be moot - flexibility now matters more than closing distance.
    1: (
        'Mr. X will reveal his exact position at the end of THIS round - before your next '
        'turn. Today\'s "possible zone" estimate is about to be replaced by his real location, '
        'so closing distance to it right now is worth less than usual. Strongly prefer '
        'whichever legal destination leaves you with the highest "onward_moves_after" (the '
        'most options next round), even at some cost to "distance_to_mrx_zone", so you can '
        'react decisively the instant his real position is known.'
    ),
    # 2 rounds out: softer - still make real zone progress, but start weighing flexibility
    # too, not just as a last-resort dead-end check.
    2: (
        'Mr. X will reveal his exact position in 2 rounds. Today\'s "possible zone" estimate '
        'will soon be replaced by his real location, so start weighing a legal destination\'s '
        '"onward_moves_after" (options next round) somewhat more than usual, alongside - not '
        'instead of - closing "distance_to_mrx_zone".'
    ),
}


def get_surfacing_proximity_prompt(round_number: int) -> str:
    """
    This round's rung of the intel-freshness ladder - empty on a round that is neither just
    after a reveal nor just before one.

    Recency is checked first: a reveal that has ALREADY happened beats one that is coming,
    because acting on a known position outranks preparing for an unknown one. In practice they
    can never both match (SURFACING_ROUNDS are at least 5 rounds apart and only two rungs
    reach in each direction), so the ordering is a statement of intent rather than a tiebreak.
    """
    for rounds_since, text in SURFACING_RECENCY_GUIDANCE.items():
        if round_number - rounds_since in SURFACING_ROUNDS:
            return f"\n    {text}\n"
    for rounds_remaining, text in SURFACING_PROXIMITY_GUIDANCE.items():
        if round_number + rounds_remaining in SURFACING_ROUNDS:
            return f"\n    {text}\n"
    return ""


# The tactical objective, shared by all six calls in a turn. It used to appear only in the
# mover's PROPOSAL prompt, which meant the four responders argued with no stated objective at
# all and - worse - the mover's own final DECISION call, the one that actually commits, never
# saw it. A detective could propose a sound move and then talk itself out of it at commit time
# against nothing but its own psychology block. Stating it once here puts it in front of every
# call for about 2% more prompt tokens.
HOW_TO_READ_YOUR_OPTIONS = """
    YOUR OPTIONS ARE PRE-ANNOTATED - use these numbers, do not recompute them. Listed best-first.
    "distance_to_mrx_zone": hops to the nearest node Mr. X could be on; 0 means it IS one of
    them, lower is closing in. "onward_moves_after": moves you would have left next round; 0 is
    a dead end. "zone_size_after" (shown only when your options differ on it): nodes he could
    still reach next round if you stand there - LOWER IS BETTER, you are on an escape route.
    "you_were_here_recently": you left that node within the last two rounds."""


def get_psychology_prompt(round_number: int, det_id: str) -> str:
    """
    Injects the 3 goals, the round's collaboration tendency (stated as a literal number), how
    to read the per-candidate annotations, and this round's rung of the intel-freshness ladder.
    """
    percentage, label = get_collaboration_tier(round_number)

    return f"""
    YOUR 3 MOTIVATIONS:
    1. MOST IMPORTANT: Catch Mr. X (Team Win).
    2. 2ND IMPORTANT: YOU ({AGENT_DISPLAY_NAMES[det_id]}) must be the one who lands on him (Selfish Glory).
    3. 3RD IMPORTANT: Catch him in the fewest turns possible (Efficiency).

    COLLABORATION TENDENCY: {label} ({percentage}%). Round {round_number} of {MAX_ROUND}.
    {COLLABORATION_BEHAVIOR[label]}
    Weigh a teammate's argument at roughly {percentage}% against your own read of the board.
    {HOW_TO_READ_YOUR_OPTIONS}
    {get_surfacing_proximity_prompt(round_number)}
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

        Detectives who have ALREADY MOVED this round (their turn is over; the Board State above
        already shows them on their new nodes): {json.dumps(committed, indent=2)}
    """


def format_options_block(det_id: str, state: ScotlandYardState, legal_moves_context: dict,
                         distances_to_zone: dict, has_revisit: bool = False) -> str:
    """
    One detective's own position, tickets, and annotated candidate destinations, best-first.

    What each annotation means is explained once in get_psychology_prompt's shared block rather
    than repeated here, since both the mover and every responder read this same block.

    `has_revisit` gates the anti-doubling-back warning: it is the one piece of guidance that is
    about THIS list rather than about the game, and spelling out a rule that none of the
    candidates can break is prompt weight spent on nothing.
    """
    detective = state["detectives"][det_id]
    tickets = {k: v for k, v in detective.items() if k.endswith("_tickets")}
    revisit_warning = (
        '\n        Some destinations below are marked "you_were_here_recently" - you left that '
        "node within the last two rounds. Shuffling back and forth between two nodes burns the "
        "team's moves while Mr. X's zone widens around you. Do not pick one unless it is your "
        "only legal move, or it genuinely closes on him better than every alternative."
        if has_revisit else ""
    )
    return f"""
        {AGENT_DISPLAY_NAMES[det_id]} is standing on Node {detective['node_id']} \
({distances_to_zone.get(detective['node_id'])} hops from the nearest node in Mr. X's zone).
        {AGENT_DISPLAY_NAMES[det_id]}'s remaining tickets: {json.dumps(tickets)}
        {AGENT_DISPLAY_NAMES[det_id]}'s ONLY legal destinations this turn, BEST FIRST (this list
        already excludes nodes occupied by another detective and nodes committed earlier this
        round):
        {json.dumps(legal_moves_context.get(det_id, []), indent=2)}{revisit_warning}
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

    Every attempt is counted against the deployment's daily budget (limits.py), including ones
    that fail - a call that times out has still been paid for.
    """
    record_llm_call()
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


def apply_detective_move(state: ScotlandYardState, det_id: str, target_node: int) -> dict:
    """
    Physically moves one detective and pays for it, returning the fields to merge into state.

    Applied the moment a turn ends rather than batched at the end of the round (ADR-0010), so
    the next detective reasons about - and the board shows - a position that is already real.
    rules.md section 2 describes exactly this ordering ("Detectives 1 through 5 moving in
    sequential order"), and section 3's occupancy rule then falls out for free: the node this
    detective vacates is genuinely unoccupied for whoever moves next.

    Legality is re-derived here rather than trusted from the turn that chose it - the same
    posture resolve_round has always taken toward the graph's output. A target that is not
    actually reachable is a forfeit (rules.md section 3: no ticket spent, no ticket
    transferred), not a crash, and is logged at WARNING because it always means an upstream bug.

    Returns {"detectives", "mr_x", "from_node", "transport", "captured"}.
    """
    detective = dict(state["detectives"][det_id])
    mr_x = dict(state["mr_x"])
    from_node = detective["node_id"]

    transport = None
    if target_node != from_node:
        transport = determine_move_transport(
            detective, target_node, other_detective_nodes(state, det_id))
        if transport is None:
            logger.warning(
                "%s could not legally move from Node %s to Node %s - forfeiting the turn.",
                det_id, from_node, target_node)
        else:
            ticket_key = f"{transport}_tickets"
            detective[ticket_key] -= 1
            mr_x[ticket_key] += 1  # Rules: a detective's spent ticket transfers to Mr. X.
            detective["node_id"] = target_node

    return {
        "detectives": {**state["detectives"], det_id: detective},
        "mr_x": mr_x,
        "from_node": from_node,
        "transport": transport,
        # Checked here, the instant this detective lands, because the rules end the game at
        # that moment - the detectives still to move this round never get their turn.
        "captured": detective["node_id"] == mr_x["current_node"],
    }


# --- THE TURN NODE ---

async def turn_node(state: ScotlandYardState, config: RunnableConfig) -> dict:
    """
    Runs the whole turn for whichever detective `state["turn_index"]` points at, applies its
    move, and waits for the board to finish animating that move before returning.

    Emits a custom stream event per LLM call rather than one per node. Under the old design a
    stage's five calls landed on the client as a single blob once the last of them finished;
    here each of the six calls is surfaced the moment it completes, which is what lets the
    Chat Log read as a conversation unfolding rather than three bursts per loop.

    `config` carries the GameSession under "configurable"/"session" (round_resolver passes it),
    used only for the end-of-turn pawn-animation handshake. A session is absent whenever the
    graph is driven directly (tests, the -m llm runners), and the turn then simply does not
    wait - the handshake is presentation timing, never correctness.

    The RunnableConfig annotation is load-bearing, not decoration: LangGraph decides whether to
    hand a node the config by inspecting that annotation, and a plain `dict` hint silently gets
    nothing passed at all rather than failing.
    """
    mover = DETECTIVE_IDS[state["turn_index"]]
    mover_name = AGENT_DISPLAY_NAMES[mover]
    committed = dict(state.get("committed_moves", {}))
    round_number = state["round_number"]
    logger.info("--- ROUND %d: %s'S TURN (%d/%d) ---",
                round_number, mover_name.upper(), state["turn_index"] + 1, NUM_DETECTIVES)

    writer = get_stream_writer()
    session = (config or {}).get("configurable", {}).get("session")

    # Recomputed per turn, not memoized per round: detectives physically move as their turns
    # end, so the occupancy this BFS is blocked through genuinely differs between turns.
    zone_context = compute_mrx_zone_context(state)
    zone_block = format_mrx_zone_block(zone_context)
    distances_to_zone = zone_context["distances_to_zone"] if zone_context else {}

    # Options are needed for the mover AND for every responder that has not moved yet, since a
    # responder argues about its own next move as well as the mover's. Detectives that already
    # moved are excluded: they have no move left to make this round, so computing options for
    # them would be both wasted work and actively misleading to put in front of them.
    # One pass, so all six calls see the same board.
    still_to_move = [det_id for det_id in DETECTIVE_IDS if det_id not in committed]
    legal_moves_context, legal_move_sets = fetch_legal_moves(state, still_to_move)
    annotate_zone_distances(legal_moves_context, distances_to_zone)
    zone_nodes = zone_context["zone_nodes"] if zone_context else []
    has_revisit = {}
    for det_id in still_to_move:
        annotate_onward_options(state, det_id, legal_moves_context)
        annotate_zone_shrink(state, det_id, legal_moves_context, zone_nodes)
        has_revisit[det_id] = annotate_revisits(state, det_id, legal_moves_context)
    # Last, so it orders on the finished annotations rather than a partially-built candidate.
    sort_candidates(legal_moves_context)

    board_block = format_board_block(state, zone_block, committed)
    mover_options = format_options_block(
        mover, state, legal_moves_context, distances_to_zone, has_revisit.get(mover, False))

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
        it, and you will get to revise it afterwards. Weigh the annotations exactly as the
        HOW TO READ YOUR OPTIONS block above tells you to.
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
                f"        You have ALREADY taken your turn this round and moved to Node "
                f"{committed[responder]}. You are standing there now and have no move left to "
                f"make this round. Argue from where you actually are."
            )
            preference_task = (
                f"Answer with Node {committed[responder]} - the node you are standing on - as "
                "your preferred_node, since your move this round is already made."
            )
        else:
            own_position_block = format_options_block(
                responder, state, legal_moves_context, distances_to_zone,
                has_revisit.get(responder, False))
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

    # --- APPLY: the detective physically moves, and pays for it ---
    applied = apply_detective_move(state, mover, committed_node)
    from_node = applied["from_node"]
    transport = applied["transport"]
    final_node = applied["detectives"][mover]["node_id"]
    logger.info("[%s MOVED] Node %s -> Node %s via %s - %s",
                mover.upper(), from_node, final_node, transport, decision_rationale)
    if applied["captured"]:
        logger.info("[%s CAPTURED MR. X] at Node %s - the round ends here.", mover.upper(), final_node)

    # Arm the handshake BEFORE the client can possibly answer it: the ack is a reply to the
    # event emitted on the very next line, and arming afterwards would race a fast client.
    if session is not None:
        session.expect_pawn_ack(round_number, mover)
    writer({
        "event": "turn_decision", "detective": mover,
        "from_node": from_node, "target_node": final_node,
        "transport": transport, "rationale": decision_rationale,
        "captured": applied["captured"],
    })

    # Hold the turn open until that pawn has finished moving on the board, so the next
    # detective does not start deliberating over a board that is still rearranging itself.
    # Bounded (ADR-0010): nobody may be watching, so this must never be able to stall a round.
    if session is not None:
        acked = await session.await_pawn_settled(TURN_ACK_TIMEOUT_SECONDS)
        if not acked:
            logger.info(
                "No pawn-settled ack for %s within %.1fs - continuing without it (no client "
                "watching, or its tweens are throttled).", mover, TURN_ACK_TIMEOUT_SECONDS)

    turn_record = {
        "proposed_node": proposed_node,
        "proposal_rationale": proposal_rationale,
        "responses": responses,
        "from_node": from_node,
        "committed_node": final_node,
        "transport": transport,
        "decision_rationale": decision_rationale,
    }
    turn_summary = "\n".join(
        [f"[{mover_name} proposes Node {proposed_node}]: {proposal_rationale}"]
        + transcript
        + [f"[{mover_name} moves to Node {final_node}]: {decision_rationale}"]
    )

    # The node just vacated, kept for two rounds so annotate_revisits can see a shuttle forming.
    # Only recorded when the detective actually went somewhere: a forfeited turn leaves it
    # standing where it was, and "recently vacated" would then flag the node it is still on.
    previously_at = list((state.get("recent_positions") or {}).get(mover, []))
    if final_node != from_node:
        previously_at = (previously_at + [from_node])[-2:]

    return {
        "detectives": applied["detectives"],
        "mr_x": applied["mr_x"],
        "committed_moves": {mover: final_node},
        "recent_positions": {mover: previously_at},
        "turn_records": {mover: turn_record},
        "turn_index": state["turn_index"] + 1,
        "captured_by": mover if applied["captured"] else None,
        "messages": [AIMessage(content=turn_summary)],
    }
