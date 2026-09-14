# ADR-0004 — OpenRouter + DeepSeek v4 Flash, with reasoning disabled

- **Status:** Accepted (2026-09-13)
- **Area:** `backend/scotland_yard/llm_client.py`

## Context

Every round costs roughly 15 LLM calls (5 detectives x propose/debate/vote), and a round can
loop up to three times. The provider choice is therefore dominated by **rate limits and
concurrency behaviour**, not by raw model quality.

The project started on Gemini's free tier. Its 15 requests/minute cap could not sustain a
single propose/debate/vote loop, which meant votes could *never* pass — the loop ran out of
quota before a majority could form. That is not a tuning problem; it made the core mechanic
non-functional.

A second, subtler problem emerged after the move: the model's reasoning-token budget was
uncapped, and a real failure (`llm_io_log_full_round_e2e.txt` CALL #13) showed it burning the
entire ~32768-token output ceiling on hidden reasoning and never emitting the structured
answer at all — a `LengthFinishReasonError` (ISSUE-006/007).

## Decision

**Serve the detectives via OpenRouter using `deepseek/deepseek-v4-flash-0731`, through
`langchain-openai`'s `ChatOpenAI` against OpenRouter's OpenAI-compatible endpoint, with
`reasoning.enabled=False`.**

Using the OpenAI-compatible client rather than a provider SDK is deliberate: swapping the
underlying model is a one-line change to `OPENROUTER_MODEL`.

## Alternatives considered — reasoning budget

Four configurations were tested against real traffic (one full propose/debate/vote loop each,
15 calls per run) before landing on the last. **Every attempt to *bound* the reasoning budget
failed identically; only fully disabling it worked:**

| Configuration | Failure rate | Observed reasoning tokens | Loop latency |
|---|---|---|---|
| `reasoning.max_tokens=2000` | 10/30 (33%) | 3355-4000 — cap ignored | — |
| `reasoning.effort="low"` | 3/15 (20%) | 3996-4000 — cap ignored | ~361s |
| `reasoning.effort="minimal"` | 2/15 (13%) | 4000 — cap ignored | ~557s |
| **`reasoning.enabled=False`** | **0/15 (0%)** | **confirmed 0** | **~30s** |

The route's self-hosted vLLM backends (`system_fingerprint` values `vllm-dev-ep-4997cd02` and
`vllm-0.26.0-dp4-ep-86dc62bb` were observed) do not honour any budget-shaped reasoning
parameter — exact token cap or qualitative effort level alike. Only the boolean takes effect,
presumably because it skips the reasoning code path entirely rather than trying to constrain it.

`reasoning` is an OpenRouter-specific extension, not part of the standard OpenAI schema, so it
is passed via `extra_body` rather than a typed field.

## Alternatives considered — provider

**Gemini free tier.** Rejected as above: 15 RPM cannot sustain the loop.
**Groq.** Evaluated (`langchain-groq` is still an unused leftover in the historical venv).
**A direct provider SDK.** Rejected: it would tie model choice to client code.

Candidate replacement models, if this one degrades, are tracked in ISSUE-008.

## Consequences

- `timeout=45` is **not optional**. Under this app's concurrent load (5 detectives firing at
  once via `asyncio.gather`), OpenRouter has been observed to leave one request in a batch
  hanging indefinitely — 4/5 returned in 3-36s, the 5th never returned even after 75s. With no
  client-side timeout that hang never raises, so neither the SDK's retry nor `agents.py`'s
  per-call `try/except` fallback ever runs; the coroutine blocks forever. ISSUE-009 notes this
  value still does not bound observed latency in all cases.
- `max_tokens=4000` is kept as an independent guard regardless of the reasoning setting.
- Disabling reasoning may make the agents' strategic play weaker than the model is capable of.
  Accepted: a 33% failure rate makes the loop unusable, and ISSUE-002 (chain-of-thought leaking
  into the `rationale` field) is believed to share this root cause.
- An `OPENROUTER_API_KEY` is required to play at all. The API starts and Mr. X can move without
  it; the detective loop fails on its first call.
