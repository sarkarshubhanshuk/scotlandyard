import Phaser from "phaser";
import { useEffect, useRef } from "react";
import type { MrXMoveWizard } from "../hooks/useMrXMoveWizard";
import { TICKET_LABELS } from "../labels";
import type { MapData, PublicGameState } from "../types";
import { BOARD_HEIGHT, BOARD_WIDTH, BoardScene, RENDER_SCALE } from "./BoardScene";

interface BoardCanvasProps {
  mapData: MapData;
  gameState: PublicGameState;
  wizard: MrXMoveWizard;
}

// Phaser owns the canvas imperatively once created - React never re-renders into it. The game
// instance is created exactly once per mount (empty dependency array); later prop changes are
// pushed into the already-running scene via BoardScene's update* methods rather than tearing
// down and rebuilding the whole Phaser.Game. The ticket-choice popup below is a plain HTML
// overlay (not part of the Phaser scene) so it can hold real form controls (buttons, a checkbox)
// - it's positioned with percentages of BOARD_WIDTH/BOARD_HEIGHT rather than a pixel calculation,
// which lines up exactly with the node's on-screen position because GameLayout's board pane has
// no letterboxing (its own aspect-ratio already matches BOARD_WIDTH:BOARD_HEIGHT - see that
// file's comment) - the percentage box below scales with it identically.
export function BoardCanvas({ mapData, gameState, wizard }: BoardCanvasProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const popupRef = useRef<HTMLDivElement>(null);
  const gameRef = useRef<Phaser.Game | null>(null);
  const isFirstStateSync = useRef(true);

  const { handleNodeClick, highlightedNodeIds, pendingTarget } = wizard;
  const selectedNodeId = pendingTarget?.target ?? null;

  // handleNodeClick's identity changes every render (it closes over the move wizard's live
  // state) - the Phaser scene is only ever wired up once, so it must always call through to
  // whichever handler is current rather than the stale one captured at scene-creation time.
  // Synced via effect (not assigned directly in the render body) per react-hooks/refs.
  const onNodeClickRef = useRef(handleNodeClick);
  useEffect(() => {
    onNodeClickRef.current = handleNodeClick;
  }, [handleNodeClick]);

  useEffect(() => {
    if (!containerRef.current) return;

    const game = new Phaser.Game({
      type: Phaser.AUTO,
      // Rendered at RENDER_SCALE x the logical board size - Phaser.Scale.FIT still stretches this
      // to the same CSS box (same aspect ratio), but the framebuffer now has enough source pixels
      // for that stretch to no longer look blurred. See BoardScene.ts's RENDER_SCALE comment.
      width: BOARD_WIDTH * RENDER_SCALE,
      height: BOARD_HEIGHT * RENDER_SCALE,
      parent: containerRef.current,
      backgroundColor: "#f4f1ea",
      scale: {
        mode: Phaser.Scale.FIT,
        autoCenter: Phaser.Scale.CENTER_BOTH,
      },
    });
    game.scene.add("BoardScene", BoardScene, true, {
      mapData,
      gameState,
      onNodeClick: (nodeId: number) => onNodeClickRef.current?.(nodeId),
      highlightedNodeIds,
      selectedNodeId,
    });
    gameRef.current = game;

    return () => {
      game.destroy(true);
      gameRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- intentionally mount-once; see comment above.
  }, []);

  useEffect(() => {
    if (isFirstStateSync.current) {
      isFirstStateSync.current = false;
      return;
    }
    const scene = gameRef.current?.scene.getScene("BoardScene") as BoardScene | undefined;
    scene?.updateGameState(gameState);
  }, [gameState]);

  useEffect(() => {
    // Unlike gameState (fully populated before this component ever mounts), the legal-move
    // fetch that produces highlightedNodeIds is still in flight at mount time, so its first
    // real value only ever arrives via this effect - no "skip the first sync" guard here.
    const scene = gameRef.current?.scene.getScene("BoardScene") as BoardScene | undefined;
    scene?.updateHighlights(highlightedNodeIds, selectedNodeId);
  }, [highlightedNodeIds, selectedNodeId]);

  // Clicking anywhere that isn't the popup itself deselects the pending target and closes it -
  // this also covers clicking a non-legal node or pawn (BoardScene never calls handleNodeClick
  // for those, so nothing else would clear pendingTarget on its own). Capture phase so this runs
  // before Phaser's own canvas listener: if the click actually landed on a different legal node,
  // BoardScene's onNodeClick fires right after and sets a fresh pendingTarget, which simply
  // overwrites the null this handler just set - so switching the selection straight to a new
  // node still works in one click.
  useEffect(() => {
    if (!pendingTarget) return;
    function handlePointerDown(event: PointerEvent) {
      if (popupRef.current && !popupRef.current.contains(event.target as Node)) {
        wizard.cancelPendingTarget();
      }
    }
    document.addEventListener("pointerdown", handlePointerDown, true);
    return () => document.removeEventListener("pointerdown", handlePointerDown, true);
  }, [pendingTarget, wizard.cancelPendingTarget]);

  const pendingPos = pendingTarget ? mapData.positions[String(pendingTarget.target)] : null;
  // Popups near the board's top edge would otherwise render partly above the canvas (clipped by
  // the board pane) - flip to below the node instead of above when there isn't clearly enough
  // room. The popup's height varies (the double-move checkbox only shows for hop 1, ticket
  // buttons vary in count), so 100 is a deliberately generous logical-unit threshold rather than
  // a measured exact fit.
  const flipBelow = pendingPos !== null && pendingPos.y < 100;

  // Now that the popup replaced the always-visible Move Selector sidebar section, this is the
  // only place left that surfaces a submit failure or an empty legal-move list to the player -
  // without it those states would fail (or stall) silently once the popup itself has closed.
  const statusMessage = wizard.error
    ? wizard.error
    : wizard.submitting
      ? "Submitting move..."
      : wizard.isMrXTurn && !pendingTarget && wizard.legalMoves.length === 0
        ? "No legal moves available."
        : null;

  return (
    <div style={{ position: "relative", width: "100%", height: "100%" }}>
      <div ref={containerRef} style={{ width: "100%", height: "100%" }} />

      {/* Once hop 1 is locked in, its own popup is long gone (replaced by hop 2's) - this is the
          only remaining way to back out of a double-move already in progress rather than being
          forced to finish it. Hidden while hop 2's own popup is open to avoid overlapping it. */}
      {wizard.hop1 && !pendingTarget && (
        <div
          style={{
            position: "absolute",
            top: 12,
            left: 12,
            right: 12,
            background: "#222",
            color: "#fff",
            padding: "6px 10px",
            borderRadius: 4,
            fontSize: 12,
            zIndex: 25,
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            gap: 8,
          }}
        >
          <span>
            Hop 1: node {wizard.hop1.target} via {TICKET_LABELS[wizard.hop1.ticket]} - pick hop 2&apos;s destination.
          </span>
          <button onClick={wizard.cancelHop1} disabled={wizard.submitting}>
            Cancel double move
          </button>
        </div>
      )}

      {statusMessage && (
        <div
          style={{
            position: "absolute",
            bottom: 12,
            left: 12,
            right: 12,
            background: wizard.error ? "#b00020" : "#222",
            color: "#fff",
            padding: "6px 10px",
            borderRadius: 4,
            fontSize: 12,
            zIndex: 25,
          }}
        >
          {statusMessage}
        </div>
      )}

      {pendingTarget && pendingPos && (
        <div
          ref={popupRef}
          style={{
            position: "absolute",
            left: `${(pendingPos.x / BOARD_WIDTH) * 100}%`,
            top: `${(pendingPos.y / BOARD_HEIGHT) * 100}%`,
            transform: flipBelow ? "translate(-50%, 16px)" : "translate(-50%, calc(-100% - 16px))",
            background: "#222",
            color: "#fff",
            borderRadius: 6,
            padding: "10px 12px",
            minWidth: 170,
            boxShadow: "0 2px 10px rgba(0, 0, 0, 0.4)",
            zIndex: 30,
            fontSize: 13,
          }}
        >
          <p style={{ margin: "0 0 8px", fontWeight: 600 }}>
            {wizard.hop1 ? "Hop 2: move" : "Move"} to node {pendingTarget.target} via:
          </p>
          <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginBottom: 8 }}>
            {pendingTarget.ticketOptions.map((ticket) => (
              <button key={ticket} onClick={() => void wizard.chooseTicket(ticket)} disabled={wizard.submitting}>
                {TICKET_LABELS[ticket]}
              </button>
            ))}
          </div>

          {/* Only offered while picking hop 1 - once hop1 is locked in, this same popup is
              reused for hop 2's own destination pick, where the choice no longer applies. */}
          {wizard.hop1 === null && (
            <label style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 8, fontSize: 12 }}>
              <input
                type="checkbox"
                checked={wizard.doubleMode}
                disabled={!wizard.canDoubleMove || wizard.submitting}
                onChange={(e) => wizard.setDoubleMode(e.target.checked)}
              />
              Double move {!wizard.canDoubleMove && "(no double tickets left)"}
            </label>
          )}

          <button onClick={wizard.cancelPendingTarget} disabled={wizard.submitting} style={{ width: "100%" }}>
            Cancel
          </button>
        </div>
      )}
    </div>
  );
}
