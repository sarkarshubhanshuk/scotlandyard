# ADR-0012 — A dashed "last known location" ghost for Mr. X

- **Status:** Accepted
- **Area:** `data/ui/pawn_last_known.svg` (new), `frontend/scripts/sync-assets.mjs`,
  `frontend/src/board/BoardScene.ts`

## Context

The board already shows Mr. X's real pawn at all times (ADR-0007), opaque on a surfacing round
and semi-transparent otherwise, and separately tracks `mr_x.last_known_node`/`last_known_round`
for the Chat Log and the detectives' own zone-BFS reasoning (`game_mechanics.md` §1). There was
no board-level marker for "where the detectives themselves last confirmed him" - the human player
could read it off the sidebar, but nothing on the board itself distinguished "his real, currently
hidden position" from "where he was last seen."

## Decision

**A single new pawn-shaped Image, `pawn_last_known`, shown at `mr_x.last_known_node` whenever
that is genuinely different information from what the real pawn already conveys this round.**

- **Visibility rule** (`BoardScene.ts:renderLastKnownGhost`): hidden if `last_known_node` is
  `null` (before Mr. X has ever surfaced - rounds 1-2), hidden if `last_known_round` equals the
  *current* `round_number` (a surfacing round - his real pawn is already opaque at that exact
  node this round, so a second marker on it would be pure redundancy), shown everywhere else.
- **The asset is a literal outline of the real pawn, not an approximation.** `pawn.svg` is
  genuine vector art whose stroke group and fill group already trace identical geometry (see its
  own comment). `pawn_last_known.svg` copies that stroke group verbatim - `fill:none`,
  `stroke-dasharray` added - so the ghost and the real pawn are guaranteed to always trace the
  exact same silhouette; there is no separate shape to keep in sync by hand.
- **A new SVG asset, not a Phaser Graphics shape.** `.cursorrules` only marks `data/board/*.json`
  and `docs/rules/rules.md` immutable - `data/ui/` art is not covered by that rule, and this
  project has already added art there before (`pawn.svg` itself). Graphics-drawn circles were
  chosen for the turn/legal-target halos (ADR-0011) because those are genuinely circles; a pawn's
  silhouette (a circular head plus a flared, curved body) is not a shape Phaser's Graphics API can
  stroke as one dashed path without manually re-deriving the curve data pawn.svg already has.
  Reusing the exact vector path was simpler and exact, not approximate.
- **Never tweened.** Unlike a pawn's own move (ADR-0010), this marker doesn't represent something
  walking anywhere - only a static fact that jumps straight to its new value the round it changes.
- **Depth `DEPTH_GHOST`**: under every real pawn but over the halos, so a detective standing on
  the exact node Mr. X was last seen at is unambiguously the one actually there, with the hollow
  outline still legible around it. This was originally written as a *negative* depth (`-0.5`) on
  the reasoning that "below the pawns" meant "below zero" - which silently put it below the board
  background too, so it never rendered at all. See ISSUE-038 and `BoardScene.ts`'s `DEPTH_*`
  ladder.
- Reused across renders (created once, then shown/hidden and repositioned), the same discipline
  every other pawn and halo on this board already follows.

## Alternatives considered

**Reuse the real `pawn` texture at reduced alpha instead of a new dashed-outline asset.** Would
have been zero new assets, but reads as "a faint copy of him," not "a *different kind* of
information" (a past sighting, not a present position) - and it would be visually similar to
Mr. X's own semi-transparent non-surfacing state, inviting confusion between the two.

**A plain dashed circle, matching the turn halo's own style.** Simpler, and Graphics-only (no new
asset). Rejected: the request was specifically for an outline of the *pawn shape*, and a generic
circle would read as another halo variant rather than a distinct "ghost of him" marker.

## Consequences

- One new asset (`data/ui/pawn_last_known.svg`) and one new line in `sync-assets.mjs`'s fixed
  copy list - both trivial to maintain since the geometry only exists once, in `pawn.svg`.
- No backend or type change of any kind: `last_known_node`/`last_known_round`/`round_number`
  were already on the wire.
- If `pawn.svg`'s silhouette is ever redrawn, `pawn_last_known.svg`'s copied path must be updated
  to match, or the two will visibly drift apart - noted in the new file's own header comment.
