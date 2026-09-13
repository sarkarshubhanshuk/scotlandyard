# ADR-0008 — No global state library on the frontend

- **Status:** Accepted
- **Area:** `frontend/src/screens/GameScreen.tsx`, `frontend/src/hooks/`

## Context

The frontend has one meaningful piece of shared state — the current `PublicGameState` — plus
two hooks that both read it and can both replace it: `useMrXMoveWizard` (after Mr. X submits a
move) and `useRoundStream` (when the round resolves). Zustand was considered up front, before
the shape of that coordination was known.

## Decision

**No global state library. `GameScreen`/`LoadedGame` own the single `PublicGameState` and pass
it down, along with an `onGameStateChange` callback that both hooks call.**

## Alternatives considered

**Zustand (or Redux / Jotai).** Rejected once the actual shape became clear: there is one
store, one owner, and a two-level component tree. A store would add indirection without
removing a single prop.

**React Context.** Rejected for the same reason, plus it re-renders every consumer on each
game-state change — and the expensive consumer (the Phaser board) deliberately does *not*
re-render; it is updated imperatively.

## Consequences

- The two hooks coordinate through nothing but that shared callback, and it turns out to be
  sufficient: `useRoundStream` reporting a fresh state is *also* what makes the move wizard
  reset for the next round, with no coordination code between them. Needing that coordination
  was the main thing a store was expected to be for.
- `key={gameId}` on `LoadedGame` gives both hooks a fresh start per game for free — something a
  global store would have required explicit teardown to match.
- The board sits outside React's render path entirely (one Phaser `Scene`, mounted once and
  updated via `updateGameState`/`updateHighlights`), so the "a state library makes re-renders
  cheap" argument does not apply to the most expensive thing on the page.
- **Worth revisiting if a third consumer of game state appears**, or if any state needs to
  outlive the `GameScreen` route.
