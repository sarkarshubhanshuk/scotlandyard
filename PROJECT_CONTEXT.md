# Project: Scotland Yard Multi-Agent System

## Overview

A digital adaptation of the board game Scotland Yard. The backend uses a multi-agent LangGraph system featuring 5 AI-driven detectives. The game implements game-theoretic behavior (selfish goals vs. team goals) via sequential debate and structured voting. The system uses the Model Context Protocol (MCP) to strictly enforce game rules and prevent LLM hallucination.

## Current Progress

- **Phase 1 & 2:** Completed (Environment, basic logic, JSON map data).

- **Phase 3:** Completed (Multi-Agent LangGraph System).

- **Phase 4:** Pending (React + Phaser.js Frontend).

## Architecture & Code Rationale

### 1. The MCP Bridge (Zero Hallucination Enforcement)

The AI agents cannot simply guess their moves. They must query a local Game Master server.

- `backend/game_master.py`: An MCP Server built with `fastmcp`. It holds the `map_graph.json` and exposes tools like `get_valid_moves`. We route its logs to `sys.stderr` to prevent standard output from corrupting the JSON-RPC data stream.

- `backend/mcp_client.py`: Uses Langchain's `MultiServerMCPClient` to manage a persistent, stateless connection to the `game_master.py` server. This ensures the connection does not drop during long LangGraph debate cycles.

### 2. The LangGraph State & Agents

- `backend/state.py`: Defines `ScotlandYardState`. Tracks Mr. X's travel log `transport_history`), ticket inventories, and handles dictionary reducers to merge move proposals without overwriting data.

- `backend/agents.py`: Contains three primary nodes `propose_node`, `debate_node`, `vote_node`). 

  - Uses `get_psychology_prompt()` to enforce 3 goals (1. Team Win, 2. Selfish Glory, 3. Efficiency). Agents become more desperate and willing to compromise as the round number approaches 24.

  - Debate is sequential (D1 speaks, D2 replies, etc.).

  - Voting requires a 3/5 threshold to lock a move.

- `backend/graph.py`: The State Machine router. It loops the propose/debate/vote cycle up to a maximum of 3 times per round. If consensus fails after 3 loops, it triggers a fallback where agents execute their self-proposed moves.

- `backend/test_phase3.py`: The async testing script used to verify round execution.

### 3. Phase 4 Target Architecture (Hybrid Frontend)

- **Tech Stack:** React (UI layer) + Phaser.js (Canvas layer).

- **Layout:** 75% Left Pane (Phaser Game Board), 25% Right Pane (React UI).

- **UI Components (React):** Chat Log (AI debate stream), Ticket Inventory, Travel Log (SVG tickets covering node IDs until reveal rounds), and Move Selector.

- **Board Component (Phaser):** Renders a static SVG graphic of the physical game board. Uses `map_graph.json` supplemented with X/Y coordinates to overlay invisible hitboxes and draw semi-transparent route highlights.

## LLM Configuration

- **Model:** `openai/gpt-oss-20b` (via Groq API).

- **Env:** API keys are managed via `.env` files (excluded via `.gitignore`).