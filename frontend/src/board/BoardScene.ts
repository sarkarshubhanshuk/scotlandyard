import Phaser from "phaser";
import { DETECTIVE_IDS, type MapData, type PublicGameState } from "../types";

// board.svg's own viewBox (docs/map/board.svg) - node_positions.json's x/y coordinates are
// already expressed in this same 600x450 pixel space, not percentages. Every drawn position
// below multiplies these logical coordinates by RENDER_SCALE (see its own comment) to place
// things in the actual, larger framebuffer pixel space.
export const BOARD_WIDTH = 600;
export const BOARD_HEIGHT = 450;

// Phaser.Scale.FIT stretches the game's base width/height (the actual WebGL framebuffer
// resolution) up to fill its CSS container - at the plain 600x450 base resolution, that
// container is typically ~950-1200 CSS px wide, plus devicePixelRatio on top, so the fixed
// 600x450 framebuffer was being magnified roughly 2x and appearing blurred. BoardCanvas.tsx sets
// the Phaser.Game config's actual width/height to BOARD_WIDTH/HEIGHT * RENDER_SCALE so the
// framebuffer has enough source pixels that the same CSS-size stretch no longer needs to upscale
// it (the same trick a "supersampled"/Retina-aware canvas uses) - every position and radius drawn
// below is multiplied by this same RENDER_SCALE to match, since board.svg's own load.svg call is
// rasterized at this scale too and every other GameObject needs to line up with it in the same
// pixel space. (Deliberately not done via camera zoom - Phaser's Scale Manager/FIT-mode
// interaction with a manually zoomed main camera turned out to not frame the world as expected;
// plain coordinate multiplication has no such ambiguity.)
export const RENDER_SCALE = 3;

// NODE_RADIUS doubles as both the drawn (near-invisible) marker and its click/tap hit area -
// kept generous rather than pixel-tight, since a 600x450 canvas scaled up to fill the board pane
// still only gives each of 199 nodes a few CSS px of hit area even at a fairly wide window size.
const NODE_RADIUS = 8;
const PAWN_RADIUS = 7;
const HIGHLIGHT_RADIUS = 12;

const DETECTIVE_COLORS: Record<string, number> = {
  detective_1: 0xe63946,
  detective_2: 0x2a9d8f,
  detective_3: 0xf4a261,
  detective_4: 0x457b9d,
  detective_5: 0x8338ec,
};
const MR_X_COLOR = 0x111111;
const LEGAL_TARGET_COLOR = 0xffd60a;
const SELECTED_TARGET_COLOR = 0xfb5607;

export interface BoardSceneData {
  mapData: MapData;
  gameState: PublicGameState;
  onNodeClick?: (nodeId: number) => void;
  highlightedNodeIds?: number[];
  selectedNodeId?: number | null;
}

export class BoardScene extends Phaser.Scene {
  private mapData!: MapData;
  private gameState!: PublicGameState;
  private onNodeClick?: (nodeId: number) => void;
  private pawns = new Map<string, Phaser.GameObjects.Arc>();
  private highlights = new Map<number, Phaser.GameObjects.Arc>();
  // The legal-move fetch that drives highlights resolves asynchronously and can arrive before
  // Phaser's own async preload/create has finished booting the scene - updateHighlights records
  // its latest request here unconditionally, and create() paints from it once actually ready,
  // so no update is ever silently lost to that race.
  private latestHighlightedNodeIds: number[] = [];
  private latestSelectedNodeId: number | null = null;
  private created = false;

  constructor() {
    super("BoardScene");
  }

  init(data: BoardSceneData) {
    this.mapData = data.mapData;
    this.gameState = data.gameState;
    this.onNodeClick = data.onNodeClick;
    this.latestHighlightedNodeIds = data.highlightedNodeIds ?? [];
    this.latestSelectedNodeId = data.selectedNodeId ?? null;
  }

  preload() {
    // load.svg (not the generic load.image) rasterizes the vector source at a target resolution
    // rather than the browser's default of the SVG's own declared intrinsic size (600x450) -
    // that default was too low-res for 199 small numbered labels + thin line art on its own, even
    // before the framebuffer-level magnification RENDER_SCALE (above) addresses.
    this.load.svg("board", "/board/board.svg", { scale: RENDER_SCALE });
  }

  create() {
    this.add
      .image(0, 0, "board")
      .setOrigin(0, 0)
      .setDisplaySize(BOARD_WIDTH * RENDER_SCALE, BOARD_HEIGHT * RENDER_SCALE);

    for (const node of this.mapData.nodes) {
      const pos = this.mapData.positions[String(node.id)];
      if (!pos) continue; // Defensive only - every node.json entry has a matching position entry.

      const hitCircle = this.add.circle(pos.x * RENDER_SCALE, pos.y * RENDER_SCALE, NODE_RADIUS * RENDER_SCALE, 0xffffff, 0);
      hitCircle.setStrokeStyle(1 * RENDER_SCALE, 0x888888, 0.4);
      hitCircle.setInteractive({ useHandCursor: true });
      hitCircle.on("pointerdown", () => this.onNodeClick?.(node.id));
    }

    this.renderPawns();
    this.created = true;
    this.paintHighlights();
  }

  private renderPawns() {
    for (const pawn of this.pawns.values()) pawn.destroy();
    this.pawns.clear();

    for (const detId of DETECTIVE_IDS) {
      const detective = this.gameState.detectives[detId];
      if (!detective) continue;
      const pos = this.mapData.positions[String(detective.node_id)];
      if (!pos) continue;
      const pawn = this.add.circle(pos.x * RENDER_SCALE, pos.y * RENDER_SCALE, PAWN_RADIUS * RENDER_SCALE, DETECTIVE_COLORS[detId]);
      pawn.setStrokeStyle(1 * RENDER_SCALE, 0xffffff, 1);
      this.pawns.set(detId, pawn);
    }

    const mrXNode = this.gameState.mr_x.last_known_node;
    if (mrXNode != null) {
      const pos = this.mapData.positions[String(mrXNode)];
      if (pos) {
        const pawn = this.add.circle(pos.x * RENDER_SCALE, pos.y * RENDER_SCALE, PAWN_RADIUS * RENDER_SCALE, MR_X_COLOR);
        pawn.setStrokeStyle(2 * RENDER_SCALE, 0xffffff, 1);
        this.pawns.set("mr_x", pawn);
      }
    }
  }

  /** Called by BoardCanvas when a fresh game snapshot arrives, to move pawns without recreating the scene. */
  updateGameState(gameState: PublicGameState) {
    this.gameState = gameState;
    this.renderPawns();
  }

  /** Called by BoardCanvas whenever the move wizard's legal-target set or pending pick changes. */
  updateHighlights(highlightedNodeIds: number[], selectedNodeId: number | null) {
    this.latestHighlightedNodeIds = highlightedNodeIds;
    this.latestSelectedNodeId = selectedNodeId;
    if (this.created) this.paintHighlights();
  }

  private paintHighlights() {
    for (const highlight of this.highlights.values()) highlight.destroy();
    this.highlights.clear();

    for (const nodeId of this.latestHighlightedNodeIds) {
      const pos = this.mapData.positions[String(nodeId)];
      if (!pos) continue;
      const isSelected = nodeId === this.latestSelectedNodeId;
      const ring = this.add.circle(pos.x * RENDER_SCALE, pos.y * RENDER_SCALE, HIGHLIGHT_RADIUS * RENDER_SCALE, 0, 0);
      ring.setStrokeStyle(2 * RENDER_SCALE, isSelected ? SELECTED_TARGET_COLOR : LEGAL_TARGET_COLOR, 1);
      this.highlights.set(nodeId, ring);
    }
  }
}
