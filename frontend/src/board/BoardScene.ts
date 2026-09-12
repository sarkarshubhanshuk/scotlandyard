import Phaser from "phaser";
import { DETECTIVE_IDS, type MapData, type PublicGameState } from "../types";

// board.svg's own viewBox (docs/map/board.svg) - node_positions.json's x/y coordinates are
// already expressed in this same pixel space, not percentages, so no rescaling is needed beyond
// what Phaser.Scale.FIT does for the canvas itself.
export const BOARD_WIDTH = 600;
export const BOARD_HEIGHT = 450;

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
    this.load.image("board", "/board/board.svg");
  }

  create() {
    this.add.image(0, 0, "board").setOrigin(0, 0).setDisplaySize(BOARD_WIDTH, BOARD_HEIGHT);

    for (const node of this.mapData.nodes) {
      const pos = this.mapData.positions[String(node.id)];
      if (!pos) continue; // Defensive only - every node.json entry has a matching position entry.

      const hitCircle = this.add.circle(pos.x, pos.y, NODE_RADIUS, 0xffffff, 0);
      hitCircle.setStrokeStyle(1, 0x888888, 0.4);
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
      const pawn = this.add.circle(pos.x, pos.y, PAWN_RADIUS, DETECTIVE_COLORS[detId]);
      pawn.setStrokeStyle(1, 0xffffff, 1);
      this.pawns.set(detId, pawn);
    }

    const mrXNode = this.gameState.mr_x.last_known_node;
    if (mrXNode != null) {
      const pos = this.mapData.positions[String(mrXNode)];
      if (pos) {
        const pawn = this.add.circle(pos.x, pos.y, PAWN_RADIUS, MR_X_COLOR);
        pawn.setStrokeStyle(2, 0xffffff, 1);
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
      const ring = this.add.circle(pos.x, pos.y, HIGHLIGHT_RADIUS, 0, 0);
      ring.setStrokeStyle(2, isSelected ? SELECTED_TARGET_COLOR : LEGAL_TARGET_COLOR, 1);
      this.highlights.set(nodeId, ring);
    }
  }
}
