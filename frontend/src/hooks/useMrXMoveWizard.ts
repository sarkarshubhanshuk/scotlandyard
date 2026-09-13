import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError, getMrXLegalMoves, submitMrXMove } from "../api/client";
import { PAWN_MOVE_DURATION_MS } from "../board/boardDimensions";
import type { LegalMove, PublicGameState, TicketType } from "../types";

interface PendingTarget {
  target: number;
  ticketOptions: TicketType[];
}

interface Hop {
  target: number;
  ticket: TicketType;
}

/**
 * Drives Mr. X's single/double-move flow end to end: fetches legal targets whenever it becomes
 * his turn, tracks the in-progress pick (target -> ticket type, and for a double-move, hop-1 ->
 * hop-2), and submits the finished move. Board clicks (which set pendingTarget) and the
 * ticket-choice popup BoardCanvas renders near that node both funnel through this one state
 * machine, so there's a single source of truth for what's currently selectable.
 */
export function useMrXMoveWizard(
  gameId: string,
  gameState: PublicGameState,
  onMoved: (newState: PublicGameState) => void,
) {
  const isMrXTurn = gameState.status === "awaiting_mr_x_move";
  const canDoubleMove = gameState.mr_x.double_tickets > 0;

  const [doubleMode, setDoubleModeState] = useState(false);
  const [legalMoves, setLegalMoves] = useState<LegalMove[]>([]);
  const [hop1, setHop1] = useState<Hop | null>(null);
  const [hop2Options, setHop2Options] = useState<LegalMove[] | null>(null);
  const [pendingTarget, setPendingTarget] = useState<PendingTarget | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Always the latest gameState, readable from inside submitSingle/submitDouble regardless of
  // when those memoized callbacks were last recreated (their own deps don't include gameState,
  // so a plain closure over the hook's parameter would go stale the moment another render happens
  // without those deps changing) - needed to read Mr. X's node just before HE moves, i.e. the
  // origin of whichever hop is about to animate. Kept current via an effect rather than a direct
  // render-phase assignment, since React disallows writing to a ref during render.
  const gameStateRef = useRef(gameState);
  useEffect(() => {
    gameStateRef.current = gameState;
  });

  // Holds the timer id for submitDouble's intermediate-hop pause (see its own comment) so it can
  // be cleared if the component unmounts mid-animation - e.g. navigating away from the game the
  // instant a double-move lands.
  const pendingHopTimerRef = useRef<number | null>(null);

  // Which hop of Mr. X's own move is currently animating on the board - null once it's finished
  // (or before any move has been submitted this turn). GameScreen reads this to compose the
  // sidebar's "Mr. X's turn - ..." label; see beginMovingHop's own comment for why it needs its
  // own independent timer rather than piggybacking on `submitting`.
  const [movingHop, setMovingHop] = useState<{ from: number; to: number } | null>(null);
  const movingHopClearTimerRef = useRef<number | null>(null);

  /**
   * Marks one hop as currently animating on the board and schedules it to clear again after
   * PAWN_MOVE_DURATION_MS - the same duration BoardScene's own tween runs for, so the sidebar
   * label keeps saying "is moving" for exactly as long as the pawn is actually sliding. Kept
   * independent of `submitting` (which flips false as soon as the network call resolves): a
   * double-move's second hop starts animating well after that point, and tying the label to
   * `submitting` would either cut it short or hold the "Submitting move..." board overlay up
   * for a move that already landed.
   */
  const beginMovingHop = useCallback((from: number, to: number) => {
    if (movingHopClearTimerRef.current !== null) window.clearTimeout(movingHopClearTimerRef.current);
    setMovingHop({ from, to });
    movingHopClearTimerRef.current = window.setTimeout(() => {
      movingHopClearTimerRef.current = null;
      setMovingHop(null);
    }, PAWN_MOVE_DURATION_MS);
  }, []);

  useEffect(() => {
    return () => {
      if (pendingHopTimerRef.current !== null) window.clearTimeout(pendingHopTimerRef.current);
      if (movingHopClearTimerRef.current !== null) window.clearTimeout(movingHopClearTimerRef.current);
    };
  }, []);

  // Reset the in-progress pick whenever a new Mr. X turn starts (round_number just advanced).
  // Adjusts state directly during render rather than in an effect - the pattern React's own docs
  // recommend for "reset state when a prop changes" (avoids the extra commit + cascading-render
  // an effect-based reset would cause); see https://react.dev/learn/you-might-not-need-an-effect.
  const [lastSyncedRound, setLastSyncedRound] = useState(gameState.round_number);
  if (isMrXTurn && gameState.round_number !== lastSyncedRound) {
    setLastSyncedRound(gameState.round_number);
    setDoubleModeState(false);
    setHop1(null);
    setHop2Options(null);
    setPendingTarget(null);
    setError(null);
    setLegalMoves([]);
  }

  // Fetch a fresh legal-move list every time it becomes Mr. X's turn (a new round, or right
  // after mount on an in-progress game) - never reused across turns, since tickets/position change.
  useEffect(() => {
    if (!isMrXTurn) return;
    let cancelled = false;
    getMrXLegalMoves(gameId)
      .then((moves) => {
        if (!cancelled) setLegalMoves(moves);
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      });
    return () => {
      cancelled = true;
    };
  }, [gameId, isMrXTurn, gameState.round_number]);

  const currentLegalMoves = useMemo(() => {
    if (!isMrXTurn) return [];
    return hop1 === null ? legalMoves : (hop2Options ?? []);
  }, [isMrXTurn, hop1, legalMoves, hop2Options]);
  const highlightedNodeIds = useMemo(() => currentLegalMoves.map((m) => m.target_node), [currentLegalMoves]);

  const setDoubleMode = useCallback(
    (value: boolean) => {
      if (hop1 !== null) return; // Can't switch modes mid-double-move; cancel hop-1 first.
      setDoubleModeState(value);
      // Deliberately leaves pendingTarget alone - the double-move checkbox lives inside the same
      // ticket-choice popup as the pending pick (BoardCanvas), so clearing it here would dismiss
      // that popup the instant the box is checked, before the player ever gets to pick a ticket.
    },
    [hop1],
  );

  const handleNodeClick = useCallback(
    (nodeId: number) => {
      if (!isMrXTurn || submitting) return;
      const match = currentLegalMoves.find((m) => m.target_node === nodeId);
      if (!match) return; // Not a legal target for the current pick - ignore.
      setError(null);
      setPendingTarget({ target: nodeId, ticketOptions: match.ticket_options });
    },
    [isMrXTurn, submitting, currentLegalMoves],
  );

  const submitSingle = useCallback(
    async (target: number, ticket: TicketType) => {
      setSubmitting(true);
      setError(null);
      const origin = gameStateRef.current.mr_x.current_node;
      try {
        const newState = await submitMrXMove(gameId, {
          move_type: "single",
          target_node: target,
          ticket_type_spent: ticket,
        });
        onMoved(newState);
        beginMovingHop(origin, target);
      } catch (err) {
        setError(err instanceof ApiError ? err.message : String(err));
      } finally {
        setSubmitting(false);
      }
    },
    [gameId, onMoved, beginMovingHop],
  );

  const submitDouble = useCallback(
    async (h1: Hop, h2: Hop) => {
      setSubmitting(true);
      setError(null);
      const origin = gameStateRef.current.mr_x.current_node;
      try {
        const newState = await submitMrXMove(gameId, {
          move_type: "double",
          hop1: { target_node: h1.target, ticket_type_spent: h1.ticket },
          hop2: { target_node: h2.target, ticket_type_spent: h2.ticket },
        });

        // A double-move played on a surfacing round reveals only the intermediate hop
        // (game_mechanics.md's mrx_turn) - the server's response already reflects that
        // (last_known_node is h1's target; current_node is the still-hidden final destination).
        // Applied as a single state update, BoardScene would tween the pawn straight to the final
        // node and render it fully opaque (its last_known_round already matches this round), even
        // though detectives were only ever shown the intermediate stop. Splitting the update in
        // two lets the pawn actually visit the intermediate node at full opacity first, then fade
        // to hidden for the real second leg - one BoardScene tween per leg, so the pause between
        // updates matches its own per-hop animation duration.
        const revealsIntermediateOnly =
          newState.mr_x.last_known_round === newState.round_number &&
          newState.mr_x.last_known_node === h1.target;

        if (revealsIntermediateOnly) {
          // status stays "awaiting_mr_x_move" for this intermediate update (rather than newState's
          // own post-move status) so useRoundStream doesn't open the detective loop's stream until
          // the FINAL onMoved call below flips it - matching ADR-0010's pacing for a single-hop
          // move, just started one hop later so the total wait covers both legs' tweens.
          onMoved({
            ...newState,
            status: "awaiting_mr_x_move",
            mr_x: { ...newState.mr_x, current_node: h1.target },
          });
          beginMovingHop(origin, h1.target);
          await new Promise<void>((resolve) => {
            pendingHopTimerRef.current = window.setTimeout(() => {
              pendingHopTimerRef.current = null;
              resolve();
            }, PAWN_MOVE_DURATION_MS);
          });
          onMoved(newState);
          beginMovingHop(h1.target, h2.target);
        } else {
          // Not a surfacing reveal - the board only ever shows one straight tween from the origin
          // to the final destination (see renderPawn/BoardScene), so the label covers that same
          // single hop rather than naming an intermediate node nothing on screen visits.
          onMoved(newState);
          beginMovingHop(origin, h2.target);
        }
      } catch (err) {
        setError(err instanceof ApiError ? err.message : String(err));
      } finally {
        setSubmitting(false);
      }
    },
    [gameId, onMoved, beginMovingHop],
  );

  const chooseTicket = useCallback(
    async (ticket: TicketType) => {
      if (!pendingTarget) return;
      const target = pendingTarget.target;

      if (hop1 === null) {
        if (!doubleMode) {
          setPendingTarget(null);
          await submitSingle(target, ticket);
          return;
        }
        // Hop-1 of a double-move: lock it in, then fetch hop-2 options from this hypothetical position.
        setHop1({ target, ticket });
        setPendingTarget(null);
        try {
          const options = await getMrXLegalMoves(gameId, { fromNode: target, ticketTypeSpent: ticket });
          setHop2Options(options);
        } catch (err) {
          setError(err instanceof ApiError ? err.message : String(err));
          setHop1(null); // Roll back - hop-1 was never actually submitted to the server.
        }
        return;
      }

      // Hop-2 of a double-move.
      setPendingTarget(null);
      await submitDouble(hop1, { target, ticket });
    },
    [pendingTarget, hop1, doubleMode, gameId, submitSingle, submitDouble],
  );

  const cancelPendingTarget = useCallback(() => setPendingTarget(null), []);
  const cancelHop1 = useCallback(() => {
    setHop1(null);
    setHop2Options(null);
    setPendingTarget(null);
  }, []);

  return {
    isMrXTurn,
    canDoubleMove,
    doubleMode,
    setDoubleMode,
    legalMoves,
    hop1,
    hop2Options,
    pendingTarget,
    highlightedNodeIds,
    submitting,
    movingHop,
    error,
    handleNodeClick,
    chooseTicket,
    cancelPendingTarget,
    cancelHop1,
  };
}

export type MrXMoveWizard = ReturnType<typeof useMrXMoveWizard>;
