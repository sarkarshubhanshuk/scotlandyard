# ADR-0014 — One container, one origin, and a cookie instead of accounts

- **Status:** Accepted
- **Area:** `Dockerfile`, `backend/scotland_yard/server.py`, `limits.py`, `session.py`,
  `frontend/src/api/client.ts`

## Context

The game needed to be playable by other people from a link, for free, with no sign-up, with
concurrent independent games, without exposing the OpenRouter key, and without a shared link
handing someone else control of your game.

Three existing properties decided most of the shape before any preference did:

- Games live in process memory (**ADR-0005**) — there is no database to scale behind.
- `/turn-ack` must reach the *same coroutine* awaiting it (**ADR-0010**). It is not a stateless
  write; it releases an `asyncio.Event` held by a round in flight.
- A round is one SSE connection held open for minutes across 30 sequential LLM calls.

## Decision

**One long-running container, never scaled past a single replica, serving the SPA and the API
from the same origin.** Deployed as a Hugging Face Docker Space (free, no card, sleeps only
after long inactivity rather than after minutes).

**Ownership is a per-browser bearer token in an `HttpOnly` cookie**, minted by `POST /games`
and recorded on the session. Every game-scoped route requires it to match.

**Caps live in `limits.py`**: concurrent games, new games per IP per hour, and a rolling daily
LLM-call budget checked *before a round starts*.

## Why these, and not the obvious alternatives

**Why not serverless (Vercel/Workers/Lambda)?** Per-request instances share no memory, so
`GAMES` would not survive between calls and an ack could not reach the coroutine waiting on it.
This is not a tuning problem; the handshake is structurally incompatible with it.

**Why one origin rather than a static host plus an API host?** Because `EventSource` cannot set
headers. Any token-in-a-header scheme is unavailable to the round stream, which leaves cookies —
and cookies are far simpler same-origin. Splitting the origins would have meant
`withCredentials`, `allow_credentials`, and an explicit origin allowlist, to arrive at what one
origin gives for nothing. (Dev still *is* split, `:5173` calling `:8000`, so the client sends
credentials explicitly there — deliberately, so dev and prod exercise the same path.)

**Why a bearer token rather than a signed claim?** The cookie carries 32 random bytes and
nothing else. Forging one is exactly as hard as guessing it, so signing would add a key to
manage and rotate for no additional strength. Compared with `hmac.compare_digest`.

**Why not accounts?** Explicitly out of scope. The cost is that ownership is per-browser: you
cannot resume your own game on another device, and clearing cookies loses it. Acceptable
because games are already ephemeral (ADR-0005) — a refresh has always discarded them.

**Why check the budget before a round rather than before each call?** Refusing mid-round would
strand a game with some detectives moved and no way to finish. The granularity is a round
because the round is the unit that must complete.

## Consequences

- **Never run two replicas.** Acks would land on the wrong process and every turn would stall
  until its 3s timeout — degraded but silent, which is the worst kind of failure.
- **A completed game is ~720 LLM calls** (6 per turn × 5 detectives × up to 24 rounds). The caps
  here are a politeness layer; the actual bound is a **hard credit limit on the OpenRouter key**,
  which is the only control that survives a bug in `limits.py`.
- **Counters are process-global**, which leaks between tests — `conftest.py` resets them (and
  `GAMES`) around every test, or the suite refuses itself partway through with a 429.
- **The key never reaches a user**, and no user-supplied *text* ever reaches a prompt: Mr. X's
  input is a node id and a ticket type, Pydantic-validated at the boundary, and prompts are
  assembled server-side from board state. There is no endpoint that proxies arbitrary text to a
  model, so the exposure is quantity, not content.
- **Games are lost on restart, redeploy or sleep.** Unchanged from ADR-0005, but now visible to
  strangers rather than only to the developer.
