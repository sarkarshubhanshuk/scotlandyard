import { useCallback, useEffect, useState } from "react";
import { openRoundStream } from "../api/client";
import { DETECTIVE_LABELS } from "../labels";
import type {
  DebateEvent,
  DetectiveId,
  ProposalEvent,
  PublicGameState,
  RoundFinalizedEvent,
  RoundResultEvent,
  StageStartedEvent,
  VoteTallyEvent,
} from "../types";

export interface ChatLogEntry {
  id: string;
  round: number;
  kind: "proposal" | "debate" | "vote_tally" | "round_finalized" | "round_result";
  lines: string[];
}

// Mirrors graph.py's node order (propose -> debate -> vote, looping back to propose up to 3
// times) - the stage the sidebar header shows while status is "detective_loop_running".
type LoopStage = "proposal" | "debate" | "vote";

const LOOP_STAGE_LABELS: Record<LoopStage, string> = {
  proposal: "Detectives are creating proposals",
  debate: "Detectives are debating on proposals",
  vote: "Detectives are voting on the next moves",
};

function label(detId: string): string {
  // detId comes from JSON object keys (proposed_strategies/locked_moves/final_moves), typed as
  // plain string - always one of the 5 known DetectiveId values per the backend's own contract.
  return DETECTIVE_LABELS[detId as DetectiveId] ?? detId;
}

/**
 * Opens the SSE round/stream as soon as the game enters "detective_loop_running" (right after
 * Mr. X's move is submitted), appends one ChatLogEntry per event, and on the terminal
 * round_result event closes the stream and reports the fresh game snapshot back up via
 * onRoundResult - the same way useMrXMoveWizard reports a finished move.
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

  // The loop always starts with a proposal round (see graph.py), so that's the correct label
  // both before the first event arrives and immediately after each new round's stream (re)opens.
  // Reset during render rather than in the effect below (React's documented "adjust state during
  // render" pattern, used elsewhere in this codebase - see useMrXMoveWizard/GameScreen) so the
  // reset doesn't cost an extra render.
  const [stage, setStage] = useState<LoopStage>("proposal");
  const streamKey = `${gameState.round_number}:${gameState.status}:${retryToken}`;
  const [lastStreamKey, setLastStreamKey] = useState(streamKey);
  if (streamKey !== lastStreamKey) {
    setLastStreamKey(streamKey);
    if (gameState.status === "detective_loop_running") {
      setStage("proposal");
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

    // Fires as soon as a stage's first LLM call actually goes out (see agents.py's
    // get_stream_writer() calls) - this, not the proposal/debate/vote_tally events below, is
    // what should move the header label, since those only arrive once the WHOLE stage (all 5
    // detectives) has already finished.
    source.addEventListener("stage_started", (event) => {
      const data = JSON.parse(event.data as string) as StageStartedEvent;
      setStage(data.stage);
    });

    source.addEventListener("proposal", (event) => {
      const data = JSON.parse(event.data as string) as ProposalEvent;
      const lines = Object.entries(data.proposed_strategies).map(
        ([detId, strategy]) => `${label(detId)}: ${strategy.rationale}`,
      );
      append({ kind: "proposal", lines });
    });

    source.addEventListener("debate", (event) => {
      const data = JSON.parse(event.data as string) as DebateEvent;
      append({ kind: "debate", lines: data.transcript.split("\n").filter(Boolean) });
    });

    source.addEventListener("vote_tally", (event) => {
      const data = JSON.parse(event.data as string) as VoteTallyEvent;
      const locked = Object.entries(data.locked_moves);
      const lines = [
        `Vote (loop ${data.loop_number}):`,
        ...(locked.length
          ? locked.map(([detId, node]) => `${label(detId)} locked in -> node ${node}`)
          : ["No moves locked this loop."]),
      ];
      append({ kind: "vote_tally", lines });
    });

    source.addEventListener("round_finalized", (event) => {
      const data = JSON.parse(event.data as string) as RoundFinalizedEvent;
      const lines = [
        "Final moves:",
        ...Object.entries(data.final_moves).map(([detId, node]) => `${label(detId)} -> node ${node}`),
      ];
      append({ kind: "round_finalized", lines });
    });

    source.addEventListener("round_result", (event) => {
      const data = JSON.parse(event.data as string) as RoundResultEvent;
      const lines =
        data.winner != null
          ? [`Game over - ${data.winner === "mr_x" ? "Mr. X" : "the detectives"} win!`]
          : [`Round ${round} complete.`];
      append({ kind: "round_result", lines });
      source.close();
      onRoundResult(data.state);
    });

    source.onerror = () => {
      setConnectionError("Lost connection to the detective loop stream.");
    };

    return () => source.close();
  }, [gameId, gameState.status, gameState.round_number, onRoundResult, retryToken]);

  return { entries, connectionError, retry, stageLabel: LOOP_STAGE_LABELS[stage] };
}
