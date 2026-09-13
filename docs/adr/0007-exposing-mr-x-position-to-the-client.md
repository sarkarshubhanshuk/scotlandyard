# ADR-0007 — Mr. X's true position is serialized to the client

- **Status:** Accepted
- **Area:** `backend/scotland_yard/serializers.py`, `state.py`, `frontend/src/board/BoardScene.ts`

## Context

`state["mr_x"]["current_node"]` is Mr. X's real, secret location. `state.py` originally
described it as a field that must never be read by any detective-facing code path, and
`serialize_public_state` excluded it from every outward-facing payload.

Then the board gained an always-visible Mr. X pawn, which needs exactly that field.

## Decision

**Include `current_node` in `serialize_public_state`.**

The board renders Mr. X's pawn at his real position, tinted black and alpha-toggled: fully
opaque on the exact round he surfaced (`last_known_round === round_number`), semi-transparent
otherwise.

## Why this is not a leak

The reasoning turns entirely on ADR-0003. "Never leak to a client" was never really about
hiding this from the **opponent** — *the opponent has no client*. The detectives are
backend-only LangGraph agents; they never receive an HTTP response of any kind, only prompt
text assembled server-side by `agents.py`. The one human-facing client that exists is played
**by Mr. X**, who already knows where he is.

So the original rule was hiding a player's own position from that player's own board, for no
security benefit.

The alpha toggle is therefore a **UI affordance, not an information-hiding mechanism**: it
reminds the human player whether the detectives currently know where they are.

## Alternatives considered

**Keep it server-side and have the client infer the pawn's position from `last_known_node`.**
Rejected: that only records where he was at the most recent surfacing round, so the pawn would
be wrong or absent for most of the game — which was exactly ISSUE-020.

**Send it only on surfacing rounds.** Rejected: the pawn would appear and vanish, and the
player would lose track of their own position between reveals.

## Consequences

- **This decision is conditional, and the condition must be re-checked if the premise changes.**
  If a detective-facing client is ever added — a spectator mode, or detective-controlled play —
  `current_node` must be excluded from whatever serialization *that* client receives.
  `serializers.py` carries this warning inline, at the point of risk rather than only here.
- `state.py`'s field comment can no longer say "never exposed"; it points here instead. Two
  descriptions of the same disclosure boundary disagreeing is worse than either one alone.
- `GameOverBanner` deliberately does **not** use `current_node` to distinguish an actual
  capture from the "Mr. X has no legal move" win condition, even though it now could. Both
  serialize identically as `winner: "detectives"`, and re-deriving which occurred client-side
  would duplicate logic `resolve_round` already owns. The wording is generic for that reason —
  not because the data is missing.
