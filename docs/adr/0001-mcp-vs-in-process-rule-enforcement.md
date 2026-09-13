# ADR-0001 — Rule enforcement: in-process validation, not MCP tool calls

- **Status:** Accepted (2026-09-13). Supersedes the project's original "MCP prevents
  hallucination" framing, which had stopped being true in the code well before this was written.
- **Area:** `backend/scotland_yard/game_master.py`, `agents.py`, `llm_client.py`

## Context

The project was originally built around a claim stated in both `README.md` and `CLAUDE.md`:
an MCP (Model Context Protocol) server holds the board and exposes `get_valid_moves`, and the
agents must query it, so they *cannot* hallucinate an illegal move.

That was the design intent. It is not what the code converged on. By the time this was
audited, four separate changes had each independently moved enforcement out of MCP:

1. `propose_node` and `vote_node` wrap the model in `with_structured_output(...)`, which
   constrains the response to a Pydantic schema. A model producing a structured answer is not
   taking a tool-calling path.
2. `get_psychology_prompt` explicitly instructs the model: *"Do not attempt to call any tool,
   browse, or otherwise seek outside information."*
3. `debate_node` was deliberately given a tool-*less* client (ISSUE-003), because a tool-bound
   one sometimes returned an empty `.content` after choosing to call a tool instead of
   answering in prose.
4. The `get_valid_moves` calls that remained were made by **this application's own Python**,
   before any prompt was built, and injected into the prompt as text. They were pre-fetches on
   the agents' behalf — not model-initiated tool calls.

So every propose and vote phase paid five concurrent stdio round-trips to a Python subprocess
(which re-read and re-parsed `map.json` on each boot) to compute something the calling process
could compute synchronously — and the thing genuinely preventing illegal moves was somewhere
else entirely: `find_proposal_conflicts`, `propose_node`'s deterministic backup enforcement,
`vote_node`'s tally validation, `finalize_round_node`'s de-duplication, and `resolve_round`'s
independent re-derivation. All plain Python. All in-process.

The stated architecture and the real one had diverged, and the real one was the better of the
two. The danger of leaving it was not performance — it was that anyone reasoning about the
safety property would look in the wrong place.

## Decision

**Name deterministic in-process validation as the hallucination guard, and use MCP only for
what it is actually good for.**

- `agents.py` calls `game_master.compute_valid_moves` directly, via one shared
  `fetch_legal_moves` helper.
- No LLM client binds tools any more. `llm_client.py` (renamed from `mcp_client.py`, which no
  longer described its contents) holds two plain cached `ChatOpenAI` instances.
- `game_master.py` keeps its `@mcp.tool()` wrappers and its `python -m scotland_yard.game_master`
  entrypoint. These serve **external** MCP clients — `.cursor/mcp.json` wires the server up for
  an IDE assistant, which is a real and continuing use.
- The `langchain-mcp-adapters` dependency is dropped; `fastmcp` remains for the server.

## Alternatives considered

**Restore the original intent.** Remove the "do not call tools" instruction, drop
`with_structured_output` in favour of a genuine tool-calling loop, and let MCP do the job it
was designed for. Rejected: it is a significant re-architecture of the hot path, it would
reintroduce the ISSUE-003 empty-content failure mode that the tool-less debate client was
added to fix, and it would trade a deterministic guarantee for a probabilistic one. The
current enforcement is *stronger* than tool-mediated enforcement, because it does not depend
on the model choosing to call the tool.

**Leave the code as-is and only fix the documentation.** Rejected: it keeps paying subprocess
round-trips for pre-fetches, and leaves a tool-binding code path that no longer has a purpose
but still looks load-bearing to a reader.

## Consequences

- **Faster and simpler.** Ten stdio subprocess round-trips per debate loop (5 in propose,
  5 in vote) become ten in-process function calls. This matters against the latency history in
  ISSUE-006/007/009.
- **The safety property is now stated where it lives.** Anyone auditing "can an agent make an
  illegal move?" is pointed at the validation functions rather than at a protocol boundary.
- **`agents.py` no longer needs `parse_valid_moves`.** That function existed solely to unwrap
  MCP content blocks, and its absence removes the entire class of bug ISSUE-001 documented —
  a wrapper-parsing mistake that silently emptied every legal-move set end-to-end.
- **One fewer drift risk between propose and vote.** They previously had two hand-maintained
  copies of the same fetch logic, which had already drifted (ISSUE-025). They now share one.
- **MCP is no longer exercised by the application itself**, so a regression in the MCP surface
  would not be caught by playing a game. It is covered by `tests/test_board_graph.py` testing
  the underlying functions, but the `@mcp.tool()` wrappers themselves are thin and untested.
  Accepted: they are one-line delegations.
