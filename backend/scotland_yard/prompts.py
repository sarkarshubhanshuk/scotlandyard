"""
Everything that turns game state into the text a detective actually reads.

Two things live here, and they are together because they are read together: the Mr. X
possible-zone intel (computed from his public travel log, then rendered), and the psychology /
guidance blocks that tell a detective what to do with it.

Kept apart from `candidates.py`, which computes the per-candidate numbers these prompts refer
to, and from `llm_calls.py`, which sends them. Prompt wording is the most frequently retuned
part of this system and the cheapest to get subtly wrong; it should not share a file with
ticket arithmetic or state mutation.
"""
import json
import logging
from typing import Optional

from .board import (
    compute_distances_to_zone,
    compute_mrx_zone,
    compute_mrx_zone_from_tickets,
)
from .rules_constants import (
    AGENT_DISPLAY_NAMES,
    COLLABORATION_TIERS,
    MAX_ROUND,
    SURFACING_ROUNDS,
)
from .state import ScotlandYardState
from .travel_log import hops_since_surfacing

logger = logging.getLogger(__name__)


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
