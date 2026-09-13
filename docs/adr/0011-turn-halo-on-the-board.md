# ADR-0011 — A shared "whose turn is it" halo, cycling across every pawn

- **Status:** Accepted
- **Area:** `frontend/src/board/BoardScene.ts`, `BoardCanvas.tsx`, `hooks/useRoundStream.ts`,
  `screens/GameScreen.tsx`

## Context

The board could already show a pawn's own move (ADR-0010's per-turn animation) and a set of
legal-destination halos for the human's own move wizard, but nothing marked *whose deliberation
is currently in progress*. With five detectives now visibly taking turns one at a time, the
player has no board-level cue for which one is thinking versus which one has already gone or is
still waiting - only the sidebar's text header said so.

Mr. X already had a permanent halo (`ensureMrXHalo`), drawn every render regardless of whose turn
it actually was. It was a "this is Mr. X's pawn" marker, not a turn indicator, and had no
detective equivalent.

## Decision

**One halo, generalized from Mr. X-only to "whichever pawn currently has the turn," cycling
Mr. X → Agent Red → Agent Blue → Agent Green → Agent Orange → Agent Purple → (next round) Mr. X →
…, matching `DETECTIVE_IDS`' own turn order.**

- **Same dimensions and radius rule as the halo it replaces**: `nodeHaloBaseRadius()` (outermost
  marker tier, zero gap) and the same thickness/color, renamed `MR_X_HALO_*` → `TURN_HALO_*`
  rather than duplicated, so the legal-target halo (which already mirrored it) needed no change
  of its own.
- **Ownership, not animation, drives it.** `BoardScene.activeTurnPawnId` is a plain "mr_x" |
  `DetectiveId` | `null` set from outside (`updateActiveTurn()`), computed in `GameScreen.tsx` as:
  `"mr_x"` for the whole time `gameState.status === "awaiting_mr_x_move"`, or
  `useRoundStream`'s own `activeTurnDetective` (set on `turn_started`, cleared on `turn_decision`)
  while `"detective_loop_running"`, or `null` otherwise (game over, or the brief gaps between one
  turn ending and the next one's `turn_started` arriving).
- **The halo never needs to track a moving pawn.** A pawn only starts moving the *instant* its
  own turn ends (`agents.py:apply_detective_move`) - the same instant `activeTurnPawnId` changes
  away from it. So whenever the halo is visible, the pawn under it is provably stationary, and
  `renderTurnHalo()` is a plain redraw-in-place rather than a tween target. This also let
  `renderPawn()`'s `extraTargets` parameter - the mechanism that used to carry Mr. X's halo along
  mid-tween - be deleted outright; nothing needs it any more.
- **Mr. X's turn starting/ending needs no event of its own.** It is simply true or false from
  `gameState.status`, which already flips the instant he submits his move (before any animation).

## Alternatives considered

**Keep the halo's mid-move tween mechanism and extend it to detectives.** Would have carried the
halo along a detective's own pawn as it moved to its new node, arriving with it. Rejected: it
answers a question nobody asked ("where is the mover going") when the actual ask was "who is
currently deciding" - which is exactly the pawn's *old* node, not its new one, and exactly the
window before the ticket is spent, not during.

**Drive the halo from the Chat Log's own `turn` field instead of new BoardScene state.** Rejected
- `ChatLogEntry.turn` already means something else (which turn a message belongs to, for
  grouping) and reusing it here would couple two independent rendering concerns for no reason;
  a dedicated `activeTurnPawnId` prop is one obvious source of truth for the board alone.

## Consequences

- `renderPawn()` lost its `extraTargets` parameter entirely - a straightforward simplification
  now that nothing needs to travel with a mid-flight tween.
- **The halo initially shipped invisible.** It inherited Mr. X's old `setDepth(-1)`, which sorts
  below the opaque board background and so never rendered - as had been true of Mr. X's own halo
  before it. Fixed by giving every layer an explicit, named depth (`BoardScene.ts`'s `DEPTH_*`
  ladder) instead of letting most objects rely on insertion order. See ISSUE-038.
- The halo can show briefly on nobody at all (the animation-and-ack gap between one detective's
  `turn_decision` and the next one's `turn_started`, and the finalize/`round_result` gap at a
  round's end) - accepted as a natural side effect of turns being discrete events rather than a
  gap worth papering over with an artificial delay.
- No backend change of any kind: everything this needs (`gameState.status`, `turn_started`,
  `turn_decision`) already existed on the wire from ADR-0009/ADR-0010.
