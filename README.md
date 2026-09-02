# Scotland Yard

A digital adaptation of the board game Scotland Yard. Mr. X is played by a human; the 5
detectives are played by LLM agents that propose, debate, and vote on their moves each round
via a LangGraph multi-agent system, with a Model Context Protocol (MCP) server enforcing game
rules server-side so the agents can't hallucinate an illegal move.

## Documentation

- **`CLAUDE.md`** — project status and architecture rationale (start here for the technical
  picture).
- **`docs/rules/rules.md`** — the official game rules (source of truth for what's allowed).
- **`docs/mechanics/game_mechanics.md`** — how the system implements each mechanic.
- **`docs/issues/known_issues.md`** — the running log of known bugs and limitations.
- **`frontend/README.md`** — frontend dev environment (Vite + React) tooling reference.
