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
DETECTIVE_IDS = ["agent_red", "agent_blue", "agent_green", "agent_yellow", "agent_purple"]

# Human-readable callsigns used anywhere a detective's identity appears in LLM-facing prompt
# text (so agents reason about "Agent Red", not the internal id). DETECTIVE_IDS stays the
# system identifier used for dict keys, schema field names, ticket lookups, etc.
# Mirrored by frontend/src/labels.ts:DETECTIVE_LABELS.
AGENT_DISPLAY_NAMES = {
    "agent_red": "Agent Red",
    "agent_blue": "Agent Blue",
    "agent_green": "Agent Green",
    "agent_yellow": "Agent Yellow",
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

# --- Consensus mechanics (NOT from rules.md - this project's own design; see ADR-0006) --
# How many of the 5 detectives must agree on a node before that move locks for the round.
VOTE_THRESHOLD = 3

# How many propose -> debate -> vote cycles run before the round is forced to finalize with
# whatever is still unlocked falling back to each detective's own self-proposal.
MAX_DEBATE_LOOPS = 3

# A strict majority threshold is what makes it arithmetically impossible for two DIFFERENT
# detectives' independent tallies to both reach the threshold on the SAME node in one loop:
# each single ballot is already de-duplicated, so two targets both reaching VOTE_THRESHOLD on
# one node would need VOTE_THRESHOLD * 2 distinct ballots. That invariant is relied on by
# vote_node (see docs/issues/known_issues.md ISSUE-010), so assert it here rather than
# leaving it as an accident of the numbers - if either value is ever retuned, this fires
# instead of silently reintroducing cross-tally collisions.
assert VOTE_THRESHOLD * 2 > NUM_DETECTIVES, (
    f"VOTE_THRESHOLD ({VOTE_THRESHOLD}) must be a strict majority of NUM_DETECTIVES "
    f"({NUM_DETECTIVES}); otherwise two detectives' tallies can both lock the same node."
)
