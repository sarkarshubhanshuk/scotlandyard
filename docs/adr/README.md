# Architecture Decision Records

Each file here records one load-bearing decision: what was chosen, what else was considered,
and what it costs. They exist because this project's rationale was previously discoverable
only by reading the implementation — excellent comments, but in the wrong place for someone
asking "why OpenRouter?" or "why isn't this FastAPI?" before they know which file to open.

Most of these were written by **relocating** rationale that already existed in a code comment
or a `game_mechanics.md` section, not by inventing it after the fact. Where that is the case,
the original now carries a one-line pointer here instead of the full argument.

## Format

Lightweight [MADR](https://adr.github.io/madr/): Status, Context, Decision, Alternatives
considered, Consequences. A decision that is later reversed gets its Status flipped to
`Superseded by ADR-NNNN` — records are never deleted or rewritten, so the history stays true
(the same discipline `docs/issues/known_issues.md` applies to issue entries).

## Index

| ADR | Title | Status |
|---|---|---|
| [0001](0001-mcp-vs-in-process-rule-enforcement.md) | Rule enforcement: in-process validation, not MCP tool calls | Accepted (supersedes the original MCP framing) |
| [0002](0002-starlette-over-fastapi.md) | Starlette over FastAPI for the API layer | Accepted |
| [0003](0003-human-mr-x-llm-detectives.md) | Mr. X is human; only the detectives are agents | Accepted |
| [0004](0004-openrouter-deepseek-llm-provider.md) | OpenRouter + DeepSeek v4 Flash, with reasoning disabled | Accepted |
| [0005](0005-in-memory-session-store.md) | In-memory game sessions with TTL eviction, no database | Accepted |
| [0006](0006-sequential-debate-and-majority-vote.md) | Sequential debate, 3/5 majority vote, 3-loop cap | Superseded by ADR-0009 |
| [0007](0007-exposing-mr-x-position-to-the-client.md) | Mr. X's true position is serialized to the client | Accepted |
| [0008](0008-no-global-state-library-on-the-frontend.md) | No global state library on the frontend | Accepted |
| [0009](0009-turn-wise-detective-play.md) | Turn-wise detective play: propose, respond, commit | Accepted (supersedes ADR-0006) |
| [0010](0010-per-turn-move-application-and-pawn-handshake.md) | Moves apply per turn, and the board paces the round | Accepted (refines ADR-0009) |
| [0011](0011-turn-halo-on-the-board.md) | A shared "whose turn is it" halo, cycling across every pawn | Accepted |
