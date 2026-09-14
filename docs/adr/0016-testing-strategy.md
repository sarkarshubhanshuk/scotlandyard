# ADR-0016 — Deterministic tests only in CI; real LLM calls are opt-in

- **Status:** Accepted (2026-09-14). Records a policy asserted in three places
  (`backend/pyproject.toml`, `.github/workflows/ci.yml`, `README.md`) but never justified
  against its alternatives.
- **Area:** `backend/pyproject.toml`, `backend/tests/`, `frontend/vite.config.ts`,
  `.github/workflows/ci.yml`

## Context

The system's most complex function is `agents.py:turn_node`: six sequential LLM calls, a
deterministic enforcement pass over each one, a move application, and a client handshake. It is
also the function that spends money — roughly 30 calls per round, 720 for a complete game.

CI runs on every push and pull request. Any test that calls a real model would put an
`OPENROUTER_API_KEY` in the repository's secrets, make every CI run cost money, and make the
build's outcome depend on a third-party service and a non-deterministic model.

## Decision

**CI runs only tests that are deterministic, offline and free.** `pytest` defaults to
`-m "not llm"` (`addopts` in `pyproject.toml`), so the LLM-dependent tests are deselected by
default — locally and in CI alike, via the same mechanism rather than a CI-only flag.

Tests that call a real model are marked `llm` and run **only** on request, with
`pytest -m llm`. There is no `OPENROUTER_API_KEY` in CI and there should not be one.

Everything else is covered deterministically:

- **Board, rules and state** — pure functions, tested directly (`test_board_graph.py`,
  `test_travel_log.py`, `test_transport_and_session.py`, `test_round_resolution.py`).
- **The turn's own logic** — `fetch_legal_moves`, every annotation, `_enforce_legal_node`,
  `apply_detective_move`, the router's capture short-circuit, and the prompt tier ladders are all
  tested without a model, because none of them *is* the model (`test_move_consensus.py`).
- **The graph end to end** — `test_detectives_after_the_captor_never_take_a_turn` drives the real
  compiled graph with a stubbed LLM, which is what proves the router actually skips turns.
- **The API layer** — every path that rejects or resolves a request before a model is involved,
  including the stream's locking, its failure handling, and ownership (`test_api.py`,
  `test_round_stream_concurrency.py`, `test_round_stream_failure.py`).
- **The frontend hooks** — Vitest, jsdom, with a fake `EventSource` (see `frontend/README.md`).

## Alternatives considered

**Recorded cassettes (VCR-style).** Record real responses once, replay them in CI. Gives genuine
end-to-end coverage of `turn_node` at no ongoing cost. Rejected for now: the recordings would
pin themselves to one model's exact structured output, and this project has already changed
provider once (ADR-0004) and reshaped its call structure twice (ADR-0006 → ADR-0009). A cassette
suite would have had to be re-recorded at each of those, and a stale cassette passes while
describing a system that no longer exists — the failure mode is silence.

**A cheap model in CI.** Keeps the calls real. Rejected: it still needs a key in CI secrets and
still makes the build non-deterministic, while testing a model the application does not use.

**A deterministic fake LLM, run in CI.** A stub honouring `with_structured_output` that returns a
scripted node per call, letting the whole six-call turn run offline. **This is the gap.** The
machinery already exists — `test_move_consensus.py`'s `_FakeLLM` does exactly this for the
capture test, and `graph.build_detective_graph()` is a factory specifically so a variant can be
built for tests. It is not adopted more widely here only because the per-phase logic is already
covered directly, so the marginal gain is the wiring *between* the phases. Worth doing; see
Consequences.

## Consequences

**`turn_node`'s full six-call sequence has no CI coverage.** The pieces are each tested and the
graph is exercised with a stub, but the assembled turn — proposal, four responses fed a growing
transcript, decision, application, handshake — is verified only by the `llm`-marked tests, which
nobody is obliged to run. A regression in how those phases hand off to each other would reach
`main`. The fake-LLM option above is the natural closure and is deliberately left open.

**The `llm` tests drift.** Not being run by any gate, they can fall behind the code they cover
without anybody learning until someone runs them. ISSUE-013 and ISSUE-017 are both instances of
exactly this — tests that had never successfully run.

**In exchange:** CI is free, fast (~4s for 203 backend tests), fully deterministic, and safe to
run on a fork's pull request, since there is no secret for it to leak. The repository can be
public without the build being an attack surface on the OpenRouter key, which — given ADR-0014
puts the whole thing behind a public link — is worth more than the coverage given up.

**A quality gate backs this up.** Because the tests deliberately do not reach the model, the
non-test checks carry more weight than usual: `ruff`, a scoped `mypy`, and a coverage floor run
before the suite (see `pyproject.toml`). mypy has already caught a defect the tests did not —
`create_game` building a total `TypedDict` with a required key missing.
