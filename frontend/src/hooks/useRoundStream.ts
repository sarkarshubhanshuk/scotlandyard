import { useCallback, useEffect, useRef, useState } from "react";
import { openRoundStream, postTurnAck } from "../api/client";
import { PAWN_MOVE_DURATION_MS } from "../board/boardDimensions";
import { DETECTIVE_LABELS, DETECTIVE_SHORT_LABELS, TICKET_LABELS } from "../labels";
import {
  DETECTIVE_IDS,
  type DetectiveId,
  type PublicGameState,
  type RoundFinalizedEvent,
  type RoundResultEvent,
  type TurnDecisionEvent,
  type TurnProposalEvent,
  type TurnResponseEvent,
  type TurnStartedEvent,
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

/** Accepts a plain snapshot or a React-style updater, so handlers can build on the latest state. */
type GameStateSetter = (
  update: PublicGameState | ((prev: PublicGameState) => PublicGameState),
) => void;

function label(detId: string): string {
  // detId comes from JSON payload fields, typed as plain string - always one of the 5 known
  // DetectiveId values per the backend's own contract.
  return DETECTIVE_LABELS[detId as DetectiveId] ?? detId;
}

// The sidebar's "Ongoing actions" line names a detective by its short callsign ("Red") rather
// than the full display name ("Agent Red") DETECTIVE_LABELS/ChatLog use everywhere else - a
// deliberate one-off distinction for this specific, more frequently-glanced-at label.
function shortLabel(detId: string): string {
  return DETECTIVE_SHORT_LABELS[detId as DetectiveId] ?? detId;
}

// Every response event belongs to one of the four detectives who are NOT this turn's mover
// (agents.py:turn_node) - once that many have answered, the mover itself is the only one left
// to hear from, i.e. its own final decision call is next.
const RESPONSES_PER_TURN = DETECTIVE_IDS.length - 1;

// The "is proposing" phase covers the mover's opening LLM call, before turn_proposal has told us
// what it's actually proposing - so the only thing worth naming here is where it currently
// stands. node is undefined only defensively (every detective always has a node_id); omitted
// rather than shown as "at undefined" in that case.
function proposingLabel(detId: DetectiveId, node: number | undefined): string {
  const name = shortLabel(detId);
  return `${name}'s turn - ${name} is proposing${node != null ? `, currently at ${node}` : ""}`;
}

// The mover's decision call, in flight once every teammate has answered - it hasn't actually
// moved yet (that only happens at turn_decision), so "from" names the same node it's still
// standing on rather than anywhere it might end up.
function finalizingLabel(detId: DetectiveId, node: number | undefined): string {
  const name = shortLabel(detId);
  return `${name}'s turn - ${name} is finalizing their move${node != null ? ` from ${node}` : ""}`;
}

/**
 * Mirrors one already-applied detective move into the local game state, so the board animates
 * that pawn and the ticket counts stay live mid-round.
 *
 * The server has already done exactly this (agents.py:apply_detective_move) - this is the client
 * keeping up, not the client deciding anything. Any drift is bounded and self-correcting:
 * round_result carries an authoritative snapshot at the end of every round.
 *
 * A detective never spends a black ticket (rules.md: black is Mr. X's alone), so the transfer
 * only has to handle the three types PublicDetective actually tracks.
 */
function applyTurnDecision(prev: PublicGameState, event: TurnDecisionEvent): PublicGameState {
  const detective = prev.detectives[event.detective];
  if (!detective) return prev;

  const moved = { ...detective, node_id: event.target_node };
  const mrX = { ...prev.mr_x };
  if (event.transport && event.transport !== "black") {
    const key = `${event.transport}_tickets` as const;
    moved[key] -= 1;
    mrX[key] += 1; // Rules: a detective's spent ticket transfers to Mr. X.
  }

  return {
    ...prev,
    mr_x: mrX,
    detectives: { ...prev.detectives, [event.detective]: moved },
  };
}

/**
 * Opens the SSE round/stream once Mr. X's own pawn has finished moving, appends one ChatLogEntry
 * per streamed LLM call, mirrors each detective's move into game state as it happens, and on the
 * terminal round_result event closes the stream and reports the fresh snapshot up.
 *
 * Every event here is one completed LLM call rather than a whole finished stage (see
 * backend/agents.py:turn_node), so entries arrive at roughly reading pace instead of five at a
 * time - and each detective's move is now its own visible pawn animation rather than five pawns
 * jumping at once when the round resolves (ADR-0010).
 */
export function useRoundStream(
  gameId: string,
  gameState: PublicGameState,
  onGameStateChange: GameStateSetter,
) {
  // Always the latest gameState, readable from inside the SSE handlers below regardless of how
  // long the stream's own effect has been open - a detective's current node (used by the
  // "is proposing, currently at N" label) changes as earlier turns in the same round land, and a
  // plain closure over this render's gameState would go stale for every turn after the first.
  // Kept current via an effect rather than a direct render-phase assignment, since React
  // disallows writing to a ref during render.
  const gameStateRef = useRef(gameState);
  useEffect(() => {
    gameStateRef.current = gameState;
  });

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
  const [status, setStatus] = useState(() => proposingLabel("agent_red", gameState.detectives.agent_red?.node_id));
  // Which detective currently has the board's "active turn" halo - see BoardScene.ts's
  // renderTurnHalo. Only meaningful while gameState.status === "detective_loop_running";
  // GameScreen combines this with gameState.status itself to decide whether the halo actually
  // belongs on this detective, on Mr. X, or on nobody (see its own activeTurnPawnId comment).
  const [activeTurnDetective, setActiveTurnDetective] = useState<DetectiveId | null>(null);
  // How many of the mover's four teammates have responded to the current proposal - reset at
  // every turn_started/turn_proposal, and read on turn_response to decide when the "team is
  // responding" phase gives way to "is finalizing move" (the mover's own decision call, which
  // fires no event of its own until it completes as turn_decision).
  const responseCountRef = useRef(0);
  const streamKey = `${gameState.round_number}:${gameState.status}:${retryToken}`;
  const [lastStreamKey, setLastStreamKey] = useState(streamKey);
  if (streamKey !== lastStreamKey) {
    setLastStreamKey(streamKey);
    if (gameState.status === "detective_loop_running") {
      setStatus(proposingLabel("agent_red", gameState.detectives.agent_red?.node_id));
    }
    // Reset at every round/status boundary, not just detective_loop_running ones: a fresh
    // round's first turn_started hasn't fired yet, and leaving a stale detective here would
    // otherwise be masked only by GameScreen's own status gating rather than being genuinely
    // cleared.
    setActiveTurnDetective(null);
  }
  // responseCountRef is a plain ref (not state), so it can't be reset from the render-phase
  // block above (React disallows writing to a ref during render) - this effect mirrors that same
  // streamKey-change condition instead. Still lands well before any turn_started could possibly
  // fire, since the stream doesn't even open until PAWN_MOVE_DURATION_MS after this commits.
  useEffect(() => {
    responseCountRef.current = 0;
  }, [streamKey]);

  // Which pawn move the next ack belongs to, recorded when a turn_decision arrives and read back
  // when the board reports that pawn has settled. A ref, not state: the board's callback must
  // see the latest value without the stream effect tearing down and reopening.
  const pendingAckRef = useRef<{ round: number; detective: DetectiveId } | null>(null);

  /**
   * The board telling us a pawn finished animating. For a detective that is the signal the
   * backend is waiting on before letting the next detective start deliberating (ADR-0010).
   * Mr. X's own pawn needs no ack - his move is gated by delaying the stream open instead.
   */
  const handlePawnSettled = useCallback(
    (pawnId: string) => {
      const pending = pendingAckRef.current;
      if (!pending || pending.detective !== pawnId) return;
      pendingAckRef.current = null;
      // Fire-and-forget: the backend continues on its own timeout regardless, so a failed ack
      // costs a little pacing and nothing else.
      void postTurnAck(gameId, pending.round, pending.detective).catch(() => {});
    },
    [gameId],
  );

  useEffect(() => {
    if (gameState.status !== "detective_loop_running") return;

    const round = gameState.round_number;
    let source: EventSource | null = null;

    const openStream = () => {
      const es = openRoundStream(gameId);
      source = es;

      let seq = 0;
      const nextId = () => `${round}-${seq++}`;
      const append = (entry: Omit<ChatLogEntry, "id" | "round">) =>
        setEntries((prev) => [...prev, { id: nextId(), round, ...entry }]);

      const on = <T,>(name: string, handler: (data: T) => void) =>
        es.addEventListener(name, (event) => handler(JSON.parse(event.data as string) as T));

      // turn_started carries no message of its own - it only moves the header, so the sidebar
      // says whose turn it is before that detective's first call has come back. Its node comes
      // from gameStateRef rather than this effect's own gameState closure, since earlier turns
      // this same round may already have moved other detectives around the board.
      on<TurnStartedEvent>("turn_started", (data) => {
        responseCountRef.current = 0;
        setStatus(proposingLabel(data.detective, gameStateRef.current.detectives[data.detective]?.node_id));
        setActiveTurnDetective(data.detective);
      });

      on<TurnProposalEvent>("turn_proposal", (data) => {
        append({
          turn: data.detective,
          speaker: data.detective,
          kind: "proposal",
          lines: [`${label(data.detective)} proposes Node ${data.target_node}: ${data.rationale}`],
        });
        responseCountRef.current = 0;
        setStatus(`${shortLabel(data.detective)}'s turn - Team is responding to ${shortLabel(data.detective)}'s proposal`);
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

        // Once every teammate has answered, the mover is the only one left to hear from - its
        // own decision call is already in flight, with no event of its own until turn_decision.
        responseCountRef.current += 1;
        if (responseCountRef.current >= RESPONSES_PER_TURN) {
          const node = gameStateRef.current.detectives[data.responding_to]?.node_id;
          setStatus(finalizingLabel(data.responding_to, node));
        } else {
          const mover = shortLabel(data.responding_to);
          setStatus(`${mover}'s turn - Team is responding to ${mover}'s proposal`);
        }
      });

      on<TurnDecisionEvent>("turn_decision", (data) => {
        // The turn ends here - the halo comes off this detective the same instant its own move
        // starts animating (both driven by this one event), never mid-flight. See ADR-0011.
        setActiveTurnDetective(null);

        const mover = shortLabel(data.detective);
        const via = data.transport != null ? ` via ${TICKET_LABELS[data.transport]}` : "";
        const stayedPut = data.target_node === data.from_node;
        const move = stayedPut
          ? `${label(data.detective)} stays at Node ${data.from_node}`
          : `${label(data.detective)} moves: Node ${data.from_node} -> Node ${data.target_node}${via}`;
        append({
          turn: data.detective,
          speaker: data.detective,
          kind: "decision",
          lines: [
            `${move}. ${data.rationale}`,
            ...(data.captured ? [`${label(data.detective)} lands on Mr. X!`] : []),
          ],
        });

        // A detective that could not move never animates, so nothing would ever report it as
        // settled - ack straight away rather than making the backend wait out its full timeout.
        if (stayedPut) {
          setStatus(`${mover}'s turn - ${mover} stays at ${data.from_node}`);
          void postTurnAck(gameId, round, data.detective).catch(() => {});
        } else {
          pendingAckRef.current = { round, detective: data.detective };
          setStatus(`${mover}'s turn - ${mover} is moving from ${data.from_node} to ${data.target_node}`);
        }

        // Mirror the move the server has already applied - this is what starts the pawn's
        // animation and keeps the ticket counts live. The functional form matters: this handler
        // closes over the gameState of whichever render opened the stream, and up to five turns
        // land against it.
        onGameStateChange((prev) => applyTurnDecision(prev, data));
      });

      on<RoundFinalizedEvent>("round_finalized", (data) => {
        append({
          turn: null,
          speaker: null,
          kind: "round_finalized",
          lines: [
            "Moves this round:",
            ...Object.entries(data.final_moves).map(([detId, move]) =>
              move.transport != null
                ? `${label(detId)} moved from Node ${move.from_node} to Node ${move.to_node} via ${TICKET_LABELS[move.transport]}`
                : `${label(detId)} stayed at Node ${move.from_node}`,
            ),
          ],
        });
        setStatus("Resolving the round");
      });

      on<RoundResultEvent>("round_result", (data) => {
        // A round continuing normally (winner === null) adds nothing here - the round's own
        // recap above already shows what happened, and the next round's header is the signal
        // that play continued. Only a genuine game-over is worth its own Chat Log entry.
        if (data.winner != null) {
          append({
            turn: null,
            speaker: null,
            kind: "round_result",
            lines: [`Game over - ${data.winner === "mr_x" ? "Mr. X" : "the detectives"} win!`],
          });
        }
        es.close();
        onGameStateChange(data.state);
      });

      es.onerror = () => {
        setConnectionError("Lost connection to the detective loop stream.");
      };
    };

    // Mr. X's pawn starts animating the instant his move lands in game state - the same instant
    // this effect runs. Holding the stream closed for exactly that long is what keeps Agent Red
    // from deliberating over a board that is still visibly rearranging itself (ADR-0010). It
    // needs no ack: the animation and this timer share one trigger and one clock, so there is
    // nothing to synchronise across - and the backend does not start the round until the stream
    // is actually opened, so the wait is genuinely a wait, not a delay on top of work already
    // running.
    const openTimer = window.setTimeout(openStream, PAWN_MOVE_DURATION_MS);

    return () => {
      window.clearTimeout(openTimer);
      source?.close();
    };
  }, [gameId, gameState.status, gameState.round_number, onGameStateChange, retryToken]);

  return { entries, connectionError, retry, stageLabel: status, handlePawnSettled, activeTurnDetective };
}
