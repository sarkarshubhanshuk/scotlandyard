# ADR-0003 — Mr. X is human; only the detectives are agents

- **Status:** Accepted
- **Area:** Whole system

## Context

Scotland Yard is asymmetric: one hidden evader against five coordinating pursuers. Either
side could have been the AI side, and the choice determines what the system is actually *for*.

## Decision

**Mr. X is played by a human through the frontend. The five detectives are LLM agents with no
client of their own.**

The detectives exist only as `propose_node`/`debate_node`/`vote_node` invocations inside the
backend process. They never receive an HTTP response, never see the frontend, and never read
game state directly — only prompt text assembled server-side by `agents.py`.

## Alternatives considered

**LLM Mr. X, human detectives.** Rejected: the interesting research content of this project is
*multi-agent coordination* — five agents with partly-conflicting incentives negotiating a joint
move. A single evader agent is an ordinary single-agent search problem, and the five human
detectives would need five human players.

**Both sides AI.** Rejected: it removes the human from the loop entirely, which makes the
debate transcript a log to read rather than something to play against.

## Consequences

- The detective side is where all the system complexity lives: the debate loop, the psychology
  prompts, the voting threshold, the deterministic validation of model output.
- **Information asymmetry is enforced by prompt construction, not by transport.** Because the
  detectives have no client, "hiding" Mr. X's position from them means simply not putting it in
  their prompt — which is what makes ADR-0007 (serializing `current_node` to the human client)
  safe.
- The game is only ever as fast as the LLM loop: a human Mr. X move takes seconds, the
  detective response takes ~30s of real model latency (ADR-0004).
- Nothing in this repo should grow "Mr. X AI" logic. If an automated Mr. X is ever wanted for
  self-play evaluation, it belongs behind an explicit flag, not in the main path.
