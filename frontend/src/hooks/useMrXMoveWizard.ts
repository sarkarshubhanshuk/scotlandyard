import { useCallback, useEffect, useMemo, useState } from "react";
import { ApiError, getMrXLegalMoves, submitMrXMove } from "../api/client";
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
 * hop-2), and submits the finished move. Board clicks and ticket-choice buttons both funnel
 * through this one state machine so BoardCanvas and MoveSelector never disagree about what's
 * currently selectable.
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
  const selectedNodeId = pendingTarget?.target ?? hop1?.target ?? null;

  const setDoubleMode = useCallback(
    (value: boolean) => {
      if (hop1 !== null) return; // Can't switch modes mid-double-move; cancel hop-1 first.
      setDoubleModeState(value);
      setPendingTarget(null);
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
      try {
        const newState = await submitMrXMove(gameId, {
          move_type: "single",
          target_node: target,
          ticket_type_spent: ticket,
        });
        onMoved(newState);
      } catch (err) {
        setError(err instanceof ApiError ? err.message : String(err));
      } finally {
        setSubmitting(false);
      }
    },
    [gameId, onMoved],
  );

  const submitDouble = useCallback(
    async (h1: Hop, h2: Hop) => {
      setSubmitting(true);
      setError(null);
      try {
        const newState = await submitMrXMove(gameId, {
          move_type: "double",
          hop1: { target_node: h1.target, ticket_type_spent: h1.ticket },
          hop2: { target_node: h2.target, ticket_type_spent: h2.ticket },
        });
        onMoved(newState);
      } catch (err) {
        setError(err instanceof ApiError ? err.message : String(err));
      } finally {
        setSubmitting(false);
      }
    },
    [gameId, onMoved],
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
    selectedNodeId,
    submitting,
    error,
    handleNodeClick,
    chooseTicket,
    cancelPendingTarget,
    cancelHop1,
  };
}

export type MrXMoveWizard = ReturnType<typeof useMrXMoveWizard>;
