# ADR-0002 — Starlette over FastAPI for the API layer

- **Status:** Accepted (2026-09-13)
- **Area:** `backend/scotland_yard/server.py`, `requests.py`

## Context

The backend needs a small HTTP API (six routes) plus a Server-Sent Events stream for the
live detective debate. FastAPI is the default reach in this ecosystem and would supply
request validation and an OpenAPI schema for free.

Relocated from `docs/mechanics/game_mechanics.md` §2's Design Rationale, which recorded the
original reasoning: `starlette`, `uvicorn`, `sse-starlette` and `websockets` were already
present as transitive installs of the LangChain/MCP stack; FastAPI was not. At the time there
was no dependency manifest at all, so "one more dependency" had no place to be recorded and
no way to be pinned.

## Decision

**Use Starlette directly, and validate request shapes explicitly.**

Route handlers are plain `async def (request) -> Response`. Request bodies and query params
are validated against Pydantic models in `requests.py` at the top of each handler, rather than
being inferred from a typed route signature.

## Alternatives considered

**FastAPI.** Would remove `requests.py`'s explicit validation calls and generate an OpenAPI
schema, which would in turn let `frontend/src/types.ts` be generated rather than hand-written.
Rejected for the route count involved — but see Consequences, because this is the ADR most
likely to be revisited.

**Starlette with no validation layer** (the original state). Rejected outright: it produced
opaque 500s with HTML stack traces for ordinary malformed input, because `KeyError` and
`ValueError` escaped the handlers with nothing to catch them.

## Consequences

- Pydantic was already a hard dependency (LangChain pulls it in), so explicit validation cost
  nothing in dependency terms while giving field-level 400 messages the client renders directly.
- **No OpenAPI schema.** `frontend/src/types.ts` is therefore hand-maintained and mirrors
  `serializers.py` by convention, not by generation. `types.ts` says so at the top. This is the
  single largest ongoing cost of this decision: a backend payload change that isn't mirrored
  is caught by nothing but manual review.
- SSE via `sse-starlette` is used directly, without the extra layer FastAPI would interpose —
  which made the `round/stream` locking fix (ISSUE-027) straightforward to express, since the
  handler owns its own async generator outright.
- If a second client or a public API ever appears, the OpenAPI gap becomes the deciding factor
  and this should be reopened.
