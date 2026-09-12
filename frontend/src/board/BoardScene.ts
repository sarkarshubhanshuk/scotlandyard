import Phaser from "phaser";
import { AGENT_COLORS } from "../labels";
import { DETECTIVE_IDS, type MapData, type PublicGameState } from "../types";
import { BOARD_HEIGHT, BOARD_WIDTH } from "./boardDimensions";

// Re-exported so BoardCanvas.tsx's existing `from "./BoardScene"` import keeps working - moved to
// boardDimensions.ts (a Phaser-free module) so GameLayout.tsx can use these for its CSS
// aspect-ratio without pulling Phaser into the main bundle; see that file's own comment. Every
// drawn position below multiplies these logical coordinates by RENDER_SCALE (see its own comment)
// to place things in the actual, larger framebuffer pixel space.
export { BOARD_WIDTH, BOARD_HEIGHT };

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
// board.svg's own node markers are drawn at r=12.5 in this same logical space (docs/map/board.svg)
// - PAWN_SIZE is the full pawn.svg texture's display footprint (its 100x100 viewBox, most of
// which is transparent padding around the actual silhouette), sized so the silhouette itself
// (which occupies roughly the middle 60x83 of that viewBox) comfortably fits inside that r=12.5
// node circle rather than overlapping the board's connection lines.
const PAWN_SIZE = 24;
const HIGHLIGHT_RADIUS = 12;

// pawn.svg's fill is `var(--pawn-color, #ffffff)` - CSS custom properties don't apply to a
// texture rasterized once at load time, so instead the texture is loaded with its default white
// fill and recolored per instance via Phaser's setTint (a multiplicative tint): white * color =
// color exactly, while the SVG's separately-drawn black outline stays black regardless of tint
// (black * anything = black) - this is what keeps every pawn's border black while only the fill
// varies. Per-agent colors live in labels.ts (AGENT_COLORS) so TicketInventory/ChatLog can use
// the exact same values for their text coloring.
const MR_X_COLOR = 0x000000; // black
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
  private pawns = new Map<string, Phaser.GameObjects.Image>();
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
    // pawn.svg has no width/height attribute of its own (only a 100x100 viewBox), so an explicit
    // target size is required here rather than relying on the SVG's own declared size like board
    // does - rasterized well above its ~24-logical-unit display footprint (see PAWN_SIZE) so it
    // stays crisp after RENDER_SCALE and Phaser.Scale.FIT both magnify it further.
    this.load.svg("pawn", "/pawn/pawn.svg", { width: 200, height: 200 });
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
      const pawn = this.add
        .image(pos.x * RENDER_SCALE, pos.y * RENDER_SCALE, "pawn")
        .setDisplaySize(PAWN_SIZE * RENDER_SCALE, PAWN_SIZE * RENDER_SCALE)
        .setTint(AGENT_COLORS[detId]);
      this.pawns.set(detId, pawn);
    }

    const mrXNode = this.gameState.mr_x.last_known_node;
    if (mrXNode != null) {
      const pos = this.mapData.positions[String(mrXNode)];
      if (pos) {
        const pawn = this.add
          .image(pos.x * RENDER_SCALE, pos.y * RENDER_SCALE, "pawn")
          .setDisplaySize(PAWN_SIZE * RENDER_SCALE, PAWN_SIZE * RENDER_SCALE)
          .setTint(MR_X_COLOR);
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
