"""
Scotland Yard backend: a multi-agent LangGraph system playing 5 detectives against a
human Mr. X, over an HTTP + SSE API.

Module map (see docs/mechanics/game_mechanics.md for how the mechanics actually work):

  rules_constants  Every value transcribed from docs/rules/rules.md - the single Python
                   home for board/ticket/round constants. Imports nothing else here.
  board            The board graph, move legality, and BFS topology helpers - the game's
                   factual core, and what actually enforces legality (ADR-0001). Stdlib only.
  game_master      A thin MCP adapter exposing a few of board's functions as tools, for an
                   external client (`python -m scotland_yard.game_master`). Nothing in the
                   running application imports it.
  state            The LangGraph ScotlandYardState TypedDict and its reducers.
  session          GameSession + the in-memory game store.
  transport        Which ticket a given detective move spends.
  travel_log       Reading Mr. X's public ticket log back out, round by round.
  llm_client       The cached OpenRouter chat clients the agents call.
  candidates       A detective's legal destinations and the deterministic numbers scoring
                   them (zone distance, onward mobility, containment, revisit history).
  prompts          Mr. X's possible-zone intel, and every block of text a detective reads.
  llm_calls        Response schemas, the deadline-bounded call, and legality enforcement.
  agents           The per-detective turn node - orchestration only: propose, respond,
                   commit, apply. Assembled from candidates/prompts/llm_calls above.
  graph            The LangGraph state machine wiring those nodes together.
  mrx_turn         Validation and application of a human Mr. X move.
  round_resolver   Drives a full round and applies its outcome to the board.
  serializers      The single choke point for outward-facing JSON payloads.
  requests         Pydantic request models for the HTTP API.
  logging_config   Logging setup for the application.
  server           The Starlette app and its routes.
"""
