"""
Scotland Yard backend: a multi-agent LangGraph system playing 5 detectives against a
human Mr. X, over an HTTP + SSE API.

Module map (see docs/mechanics/game_mechanics.md for how the mechanics actually work):

  rules_constants  Every value transcribed from docs/rules/rules.md - the single Python
                   home for board/ticket/round constants. Imports nothing else here.
  game_master      Board graph loading, move legality, and BFS topology helpers. Also an
                   MCP server entrypoint (`python -m scotland_yard.game_master`).
  state            The LangGraph ScotlandYardState TypedDict and its reducers.
  session          GameSession + the in-memory game store.
  transport        Which ticket a given detective move spends.
  llm_client       The cached OpenRouter chat clients the agents call.
  agents           propose/debate/vote LangGraph nodes and their prompts.
  graph            The LangGraph state machine wiring those nodes together.
  mrx_turn         Validation and application of a human Mr. X move.
  round_resolver   Drives a full round and applies its outcome to the board.
  serializers      The single choke point for outward-facing JSON payloads.
  requests         Pydantic request models for the HTTP API.
  logging_config   Logging setup for the application.
  server           The Starlette app and its routes.
"""
