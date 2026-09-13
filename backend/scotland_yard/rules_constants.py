"""
Every constant transcribed from docs/rules/rules.md, in one place.

These values previously lived scattered across session.py, round_resolver.py and agents.py -
including MAX_ROUND (round_resolver) and a dead, never-referenced MAX_ROUNDS (agents) that
held the same number under a different name. Splitting rules data across modules that also
hold behavior meant `mrx_turn` had to import a rules constant from `round_resolver`, a
resolver module, purely because that's where the constant happened to live.

This module imports nothing else in the package, so anything may import it without creating
a cycle. If a value here disagrees with docs/rules/rules.md, rules.md wins - per .cursorrules
that file is immutable truth and this is a transcription of it.
"""

# --- Players (rules.md §1) -------------------------------------------------------------
# The internal identifier for each detective, and the fixed order everything iterates in:
# debate speaking order, proposal/ballot conflict resolution (earlier id wins a tie), and
# move application in round resolution.
DETECTIVE_IDS = ["agent_red", "agent_blue", "agent_green", "agent_orange", "agent_purple"]

# Human-readable callsigns used anywhere a detective's identity appears in LLM-facing prompt
# text (so agents reason about "Agent Red", not the internal id). DETECTIVE_IDS stays the
# system identifier used for dict keys, schema field names, ticket lookups, etc.
# Mirrored by frontend/src/labels.ts:DETECTIVE_LABELS.
AGENT_DISPLAY_NAMES = {
    "agent_red": "Agent Red",
    "agent_blue": "Agent Blue",
    "agent_green": "Agent Green",
    "agent_orange": "Agent Orange",
    "agent_purple": "Agent Purple",
}

NUM_DETECTIVES = len(DETECTIVE_IDS)

# --- Setup (rules.md §1) ---------------------------------------------------------------
# The shared pool Mr. X and all 5 detectives draw unique starting nodes from.
STARTING_NODE_POOL = [
    13, 26, 29, 34, 50, 53, 91, 94, 103, 112, 117, 132, 138, 141, 155, 174, 197, 198,
]

MR_X_STARTING_TICKETS = {
    "taxi_tickets": 2,
    "bus_tickets": 5,
    "metro_tickets": 3,
    "black_tickets": 5,
    "double_tickets": 2,
}
DETECTIVE_STARTING_TICKETS = {"taxi_tickets": 11, "bus_tickets": 8, "metro_tickets": 4}

# --- Rounds and visibility (rules.md §2) -----------------------------------------------
# Mr. X wins by surviving the complete conclusion of this round.
MAX_ROUND = 24

# Rounds at the end of which Mr. X must reveal his location.
SURFACING_ROUNDS = {3, 8, 13, 18, 24}

# --- Tickets (rules.md §3) -------------------------------------------------------------
# Ticket types Mr. X may actually spend on a hop. Note "double" is deliberately absent: it
# is a travel-log sentinel (mrx_turn.py inserts it ahead of a double-move's two hop entries),
# never a ticket that pays for a move.
VALID_TICKET_TYPES = {"taxi", "bus", "metro", "black"}

# Transport types a detective can spend. Boat is excluded by rules.md: boat routes exist on
# the board but are traversable only by Mr. X, using a black ticket.
DETECTIVE_TRANSPORT_TYPES = ("taxi", "bus", "metro")

# --- Turn mechanics (NOT from rules.md - this project's own design; see ADR-0009) -------
# Detectives take their turns one at a time, in DETECTIVE_IDS order, and each commits its own
# move at the end of its own turn. Within one detective's turn, every OTHER detective responds
# to that detective's proposal once, in cyclic DETECTIVE_IDS order starting from the mover's
# immediate successor (Agent Green's turn -> Orange, Purple, Red, Blue).
CALLS_PER_TURN = 1 + (NUM_DETECTIVES - 1) + 1  # proposal + one response each + final decision

# How willing a detective is to weigh a teammate's argument above its own Selfish Glory goal,
# as (inclusive upper round bound, percentage, label). Scans in order; the last entry is the
# open-ended late-game tier, so its bound is MAX_ROUND. The literal percentage goes into the
# prompt verbatim - get_psychology_prompt states it as a number rather than only as a label,
# because "25%" is a sharper instruction to the model than "LOW" alone.
COLLABORATION_TIERS = (
    (4, 1, "MINIMUM"),
    (8, 25, "LOW"),
    (12, 50, "MEDIUM"),
    (16, 75, "HIGH"),
    (MAX_ROUND, 99, "MAXIMUM"),
)

# The tier ladder must stay contiguous and must cover every round up to MAX_ROUND, or
# get_collaboration_tier would fall off the end of the table for a legal round number.
assert COLLABORATION_TIERS[-1][0] >= MAX_ROUND, (
    f"COLLABORATION_TIERS' last tier must cover MAX_ROUND ({MAX_ROUND}); it stops at "
    f"{COLLABORATION_TIERS[-1][0]}."
)
assert all(
    earlier[0] < later[0] for earlier, later in zip(COLLABORATION_TIERS, COLLABORATION_TIERS[1:])
), f"COLLABORATION_TIERS' round bounds must be strictly increasing: {COLLABORATION_TIERS}"

# --- Pawn-animation handshake (see ADR-0010) -------------------------------------------
# A detective commits its move at the end of its turn, and the board animates the pawn from its
# old node to its new one. The next detective's turn must not begin until that animation has
# finished, so the human player sees one pawn move at a time rather than the next agent
# deliberating over a board that is still visibly rearranging itself.
#
# The board lives in the browser, so the turn loop learns about this from the client: it POSTs
# /games/{id}/turn-ack when the tween completes. This is the bound on how long the loop will
# wait for that ack before continuing anyway - nobody may be watching, the tab may be
# backgrounded with its tweens throttled, or the connection may have dropped, and a round must
# survive all three. Set comfortably above the frontend's own animation duration
# (PAWN_MOVE_DURATION_MS in BoardScene.ts, currently 1000ms) plus a round-trip.
TURN_ACK_TIMEOUT_SECONDS = 3.0

# --- Per-call LLM deadline (see ADR-0009 "Consequences", docs/issues ISSUE-009) ---------
# llm_client.py's timeout=45 is enforced by the HTTP client as an IDLE-GAP timeout - reset by
# every streamed chunk - so it reliably kills a genuinely stuck call but does not cap total
# call duration. That was survivable while propose/vote fired 5 calls concurrently and a
# straggler overlapped its siblings. Turn-wise play makes every call strictly sequential, so
# one slow call now adds directly to the round's wall-clock. agents.py wraps each call in
# asyncio.wait_for(..., LLM_CALL_DEADLINE_SECONDS) and falls back deterministically on expiry.
LLM_CALL_DEADLINE_SECONDS = 90
