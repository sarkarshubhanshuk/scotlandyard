import Phaser from "phaser";
import { useEffect, useRef } from "react";
import type { MapData, PublicGameState } from "../types";
import { BOARD_HEIGHT, BOARD_WIDTH, BoardScene, RENDER_SCALE } from "./BoardScene";

interface BoardCanvasProps {
  mapData: MapData;
  gameState: PublicGameState;
  onNodeClick?: (nodeId: number) => void;
  highlightedNodeIds?: number[];
  selectedNodeId?: number | null;
}

// Phaser owns this canvas imperatively once created - React never re-renders into it. The game
// instance is created exactly once per mount (empty dependency array); later prop changes are
// pushed into the already-running scene via BoardScene's update* methods rather than tearing
// down and rebuilding the whole Phaser.Game.
export function BoardCanvas({ mapData, gameState, onNodeClick, highlightedNodeIds = [], selectedNodeId = null }: BoardCanvasProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const gameRef = useRef<Phaser.Game | null>(null);
  const isFirstStateSync = useRef(true);

  // onNodeClick's identity changes every render (it closes over the move wizard's live state) -
  // the Phaser scene is only ever wired up once, so it must always call through to whichever
  // handler is current rather than the stale one captured at scene-creation time. Synced via
  // effect (not assigned directly in the render body) per react-hooks/refs.
  const onNodeClickRef = useRef(onNodeClick);
  useEffect(() => {
    onNodeClickRef.current = onNodeClick;
  }, [onNodeClick]);

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

  return <div ref={containerRef} style={{ width: "100%", height: "100%" }} />;
}
