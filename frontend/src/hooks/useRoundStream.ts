import { useCallback, useEffect, useState } from "react";
import { openRoundStream } from "../api/client";
import { DETECTIVE_LABELS, TICKET_LABELS } from "../labels";
import type {
  DetectiveId,
  PublicGameState,
  RoundFinalizedEvent,
  RoundResultEvent,
  TurnDecisionEvent,
  TurnProposalEvent,
  TurnResponseEvent,
  TurnStartedEvent,
} from "../types";

export type ChatLogKind = "proposal" | "response" | "decision" | "round_finalized" | "round_result";

export interface ChatLogEntry {
  id: string;
  round: number;
  /**
   * Whose turn this message belongs to - the mover, not necessarily the speaker. A response
   * from Agent Blue during Agent Red's turn carries turn "agent_red" and speaker "agent_blue".
   * ChatLog groups consecutive entries by this, which is what makes the round read as five
   * turns rather than one undifferentiated stream. Null for end-of-round entries.
   */
  turn: DetectiveId | null;
  speaker: DetectiveId | null;
  kind: ChatLogKind;
  lines: string[];
}

function label(detId: string): string {
  // detId comes from JSON payload fields, typed as plain string - always one of the 5 known
  // DetectiveId values per the backend's own contract.
  return DETECTIVE_LABELS[detId as DetectiveId] ?? detId;
}

/**
 * Opens the SSE round/stream as soon as the game enters "detective_loop_running" (right after
 * Mr. X's move is submitted), appends one ChatLogEntry per streamed LLM call, and on the
 * terminal round_result event closes the stream and reports the fresh game snapshot back up via
 * onRoundResult - the same way useMrXMoveWizard reports a finished move.
 *
 * Every event here is one completed LLM call rather than a whole finished stage (see
 * backend/agents.py:turn_node), so entries arrive at roughly reading pace instead of five at a
 * time.
 */
export function useRoundStream(
  gameId: string,
  gameState: PublicGameState,
  onRoundResult: (newState: PublicGameState) => void,
) {
  const [entries, setEntries] = useState<ChatLogEntry[]>([]);
  const [connectionError, setConnectionError] = useState<string | null>(null);
  // Bumped by retry() to force the effect below to reopen the stream even though gameId/status/
  // round_number haven't changed - the server keeps running its detective loop regardless of
  // whether a client is listening, so a dropped connection just needs a fresh EventSource, not a
  // new round.
  const [retryToken, setRetryToken] = useState(0);
  const retry = useCallback(() => {
    setConnectionError(null);
    setRetryToken((t) => t + 1);
  }, []);

  // The round always opens with Agent Red's turn (graph.py starts at turn_index 0), so that's
  // the correct label both before the first event arrives and immediately after each new
  // round's stream (re)opens. Reset during render rather than in the effect below (React's
  // documented "adjust state during render" pattern, used elsewhere in this codebase - see
  // useMrXMoveWizard/GameScreen) so the reset doesn't cost an extra render.
  const [status, setStatus] = useState(`${DETECTIVE_LABELS.agent_red} is taking their turn`);
  const streamKey = `${gameState.round_number}:${gameState.status}:${retryToken}`;
  const [lastStreamKey, setLastStreamKey] = useState(streamKey);
  if (streamKey !== lastStreamKey) {
    setLastStreamKey(streamKey);
    if (gameState.status === "detective_loop_running") {
      setStatus(`${DETECTIVE_LABELS.agent_red} is taking their turn`);
    }
  }

  useEffect(() => {
    if (gameState.status !== "detective_loop_running") return;

    const round = gameState.round_number;
    const source = openRoundStream(gameId);
    let seq = 0;
    const nextId = () => `${round}-${seq++}`;
    const append = (entry: Omit<ChatLogEntry, "id" | "round">) =>
      setEntries((prev) => [...prev, { id: nextId(), round, ...entry }]);

    const on = <T,>(name: string, handler: (data: T) => void) =>
      source.addEventListener(name, (event) => handler(JSON.parse(event.data as string) as T));

    // turn_started carries no message of its own - it only moves the header, so the sidebar
    // says whose turn it is before that detective's first call has come back.
    on<TurnStartedEvent>("turn_started", (data) => {
      setStatus(`${label(data.detective)} is taking their turn`);
    });

    on<TurnProposalEvent>("turn_proposal", (data) => {
      append({
        turn: data.detective,
        speaker: data.detective,
        kind: "proposal",
        lines: [`${label(data.detective)} proposes Node ${data.target_node}: ${data.rationale}`],
      });
      setStatus(`The team is responding to ${label(data.detective)}'s proposal`);
    });

    on<TurnResponseEvent>("turn_response", (data) => {
      // preferred_node is advisory and may be absent - see the type's own note. Appending it to
      // the line rather than giving it its own entry keeps one message per LLM call.
      const intent =
        data.preferred_node != null ? ` (would take Node ${data.preferred_node} itself)` : "";
      append({
        turn: data.responding_to,
        speaker: data.detective,
        kind: "response",
        lines: [`${label(data.detective)}: ${data.response}${intent}`],
      });
      setStatus(`The team is responding to ${label(data.responding_to)}'s proposal`);
    });

    on<TurnDecisionEvent>("turn_decision", (data) => {
      const via = data.transport != null ? ` via ${TICKET_LABELS[data.transport]}` : "";
      const move =
        data.target_node === data.from_node
          ? `${label(data.detective)} stays at Node ${data.from_node}`
          : `${label(data.detective)} commits: Node ${data.from_node} -> Node ${data.target_node}${via}`;
      append({
        turn: data.detective,
        speaker: data.detective,
        kind: "decision",
        lines: [`${move}. ${data.rationale}`],
      });
    });

    on<RoundFinalizedEvent>("round_finalized", (data) => {
      append({
        turn: null,
        speaker: null,
        kind: "round_finalized",
        lines: [
          "Final moves:",
          ...Object.entries(data.final_moves).map(([detId, move]) =>
            move.transport != null
              ? `${label(detId)} moves from Node ${move.from_node} to Node ${move.to_node} via ${TICKET_LABELS[move.transport]}`
              : `${label(detId)} stays at Node ${move.from_node}`,
          ),
        ],
      });
      setStatus("Resolving the round");
    });

    on<RoundResultEvent>("round_result", (data) => {
      // A round continuing normally (winner === null) adds nothing here - the "Final Moves"
      // entry above already shows what happened, and the next round's own header is the signal
      // that play continued. Only a genuine game-over is worth its own Chat Log entry.
      if (data.winner != null) {
        append({
          turn: null,
          speaker: null,
          kind: "round_result",
          lines: [`Game over - ${data.winner === "mr_x" ? "Mr. X" : "the detectives"} win!`],
        });
      }
      source.close();
      onRoundResult(data.state);
    });

    source.onerror = () => {
      setConnectionError("Lost connection to the detective loop stream.");
    };

    return () => source.close();
  }, [gameId, gameState.status, gameState.round_number, onRoundResult, retryToken]);

  return { entries, connectionError, retry, stageLabel: status };
}
