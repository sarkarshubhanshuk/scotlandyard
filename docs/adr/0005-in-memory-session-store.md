# ADR-0005 — In-memory game sessions with TTL eviction, no database

- **Status:** Accepted (2026-09-13)
- **Area:** `backend/scotland_yard/session.py`

## Context

A game is a `ScotlandYardState` plus orchestration bookkeeping (whose turn it is, whether the
game ended, a lock serialising submissions). It needs to survive across the several HTTP
requests that make up a round, but the product itself is a single-player-versus-agents game
played in one sitting.

## Decision

**Store games in a plain module-level `dict`, keyed by a UUID4 game id. No database, no
serialisation, no cross-restart durability. Evict games untouched for longer than
`SESSION_TTL_SECONDS` (2 hours), swept inline on each `create_game`.**

## Alternatives considered

**A database or Redis.** Rejected: it introduces a service dependency, a schema, and a
serialisation boundary for `ScotlandYardState` (which holds LangChain message objects) — all to
support a durability guarantee the product does not make. "Refreshing the page discards the
game" was an accepted UI behaviour from the start.

**Pickling to disk.** Rejected for the same reason, plus the usual pickle hazards.

**No eviction at all** (the original state). Rejected once examined. *No persistence* and *no
eviction* are separate decisions, and only the first was deliberate: without a TTL, every
abandoned game — including one created by a page refresh and never played — leaked for the
lifetime of the process.

**A background sweeper task.** Rejected: `create_game` is the only way the store can grow, so
it is the only moment eviction can possibly be needed. Sweeping inline avoids a background-task
lifecycle to start, stop, and test.

## Consequences

- Restarting the backend invalidates every in-progress game. The frontend handles this
  explicitly: a 404 renders "it may have ended when the backend process restarted, or been
  swept after sitting idle," with a link to start a new game.
- The store is bounded by TTL but not by count — a burst of game creations within the window
  still grows it without limit. Acceptable for a locally-run, single-user application; an LRU
  cap would be the fix if this ever ran multi-user.
- **The game id is the only access control.** It is an unguessable UUID4, but any holder of it
  can move Mr. X. This is why `server.py` binds to `127.0.0.1` and restricts CORS to the known
  frontend origin by default — the session model has no authentication to fall back on.
- `GameSession.touch()` must be called on every request that reads a game, or an active game
  could be swept mid-play. `server.py:_get_session` is the single choke point that does this.
