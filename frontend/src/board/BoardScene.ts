import Phaser from "phaser";
import { AGENT_COLORS, DETECTIVE_LABELS } from "../labels";
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
// Semi-transparent on a non-surfacing round - a reminder to the human Mr. X player that
// detectives don't currently know this position, not an actual information-hiding mechanism
// (see renderPawns()'s own comment on why exposing current_node here is safe at all).
const MR_X_HIDDEN_ALPHA = 0.4;
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
  private pawnTooltip: Phaser.GameObjects.Container | null = null;
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
    // The pawn objects a stale tooltip's hover handlers referred to no longer exist once this
    // runs (a fresh game snapshot recreates every pawn from scratch) - drop any open tooltip
    // rather than leaving it pointing at a destroyed pawn.
    this.hidePawnTooltip();

    for (const detId of DETECTIVE_IDS) {
      const detective = this.gameState.detectives[detId];
      if (!detective) continue;
      const pos = this.mapData.positions[String(detective.node_id)];
      if (!pos) continue;
      const pawn = this.add
        .image(pos.x * RENDER_SCALE, pos.y * RENDER_SCALE, "pawn")
        .setDisplaySize(PAWN_SIZE * RENDER_SCALE, PAWN_SIZE * RENDER_SCALE)
        .setTint(AGENT_COLORS[detId]);
      this.makePawnHoverable(pawn, DETECTIVE_LABELS[detId], detective.node_id);
      this.pawns.set(detId, pawn);
    }

    // Mr. X's pawn is always rendered at his real current_node now (see serializers.py's own
    // note on why exposing this is safe: the only human-facing client is played BY Mr. X, and
    // detectives are backend-only agents with no client access at all). Alpha is a visual
    // reminder for the human of whether detectives ALSO currently know this position - opaque
    // only on the exact round he's surfaced (last_known_round stays stuck on a past round
    // forever after, per build_next_round_state, so this must compare against the CURRENT round,
    // not just check non-null), semi-transparent every other round - not an information-hiding
    // mechanism, since nothing here is ever hidden from the one person who can see this canvas.
    const mrX = this.gameState.mr_x;
    const pos = this.mapData.positions[String(mrX.current_node)];
    if (pos) {
      const isSurfacingRound = mrX.last_known_round === this.gameState.round_number;
      const pawn = this.add
        .image(pos.x * RENDER_SCALE, pos.y * RENDER_SCALE, "pawn")
        .setDisplaySize(PAWN_SIZE * RENDER_SCALE, PAWN_SIZE * RENDER_SCALE)
        .setTint(MR_X_COLOR)
        .setAlpha(isSurfacingRound ? 1 : MR_X_HIDDEN_ALPHA);
      this.makePawnHoverable(pawn, "Mr. X", mrX.current_node, [
        isSurfacingRound ? "Visible to Detectives" : "Invisible to Detectives",
      ]);
      this.pawns.set("mr_x", pawn);
    }
  }

  // A pawn's own display bounds (its interactive hit area, since setInteractive() is called with
  // no explicit shape) are used as the hover target rather than a separate invisible hit circle
  // like the node markers get - the pawn image itself is the only thing a player would expect to
  // hover to inspect it.
  private makePawnHoverable(pawn: Phaser.GameObjects.Image, label: string, node: number, extraLines: string[] = []) {
    pawn.setInteractive({ useHandCursor: true });
    pawn.on("pointerover", () => this.showPawnTooltip(pawn, [label, `Current Node ${node}`, ...extraLines]));
    pawn.on("pointerout", () => this.hidePawnTooltip());
  }

  // lines[0] renders bold (the pawn's name); every other line renders as plain text below it -
  // a detective's tooltip has 2 lines (name, node), Mr. X's has a 3rd (surfaced status).
  private showPawnTooltip(pawn: Phaser.GameObjects.Image, lines: string[]) {
    this.hidePawnTooltip();

    const fontSize = 13 * RENDER_SCALE;
    const lineGap = 2 * RENDER_SCALE;
    const paddingX = 8 * RENDER_SCALE;
    const paddingY = 6 * RENDER_SCALE;

    // All lines share x=0 with origin (0.5, 0) so each is independently centered horizontally,
    // rather than centering the box around their (possibly different) text widths by hand.
    const textObjects = lines.map((line, i) =>
      this.add
        .text(0, 0, line, { fontSize: `${fontSize}px`, color: "#ffffff", fontStyle: i === 0 ? "bold" : "normal" })
        .setOrigin(0.5, 0),
    );

    const boxWidth = Math.max(...textObjects.map((t) => t.width)) + paddingX * 2;
    const contentHeight =
      textObjects.reduce((sum, t) => sum + t.height, 0) + lineGap * (textObjects.length - 1);
    const boxHeight = contentHeight + paddingY * 2;
    const gapAbovePawn = 6 * RENDER_SCALE;

    // Pawns near the board's edges (e.g. a top-row detective, or one close to the left/right
    // border) would otherwise have the popup drawn partly off-canvas - Phaser doesn't reflow
    // it back into view the way a browser tooltip would, it just silently clips at the canvas
    // boundary. Clamp horizontally, and flip to below the pawn instead of above when there
    // isn't room above, so the whole box always stays fully on-canvas.
    const preferredY = pawn.y - pawn.displayHeight / 2 - gapAbovePawn;
    const fitsAbove = preferredY - boxHeight >= 0;
    const containerX = Phaser.Math.Clamp(pawn.x, boxWidth / 2, BOARD_WIDTH * RENDER_SCALE - boxWidth / 2);
    const containerY = fitsAbove ? preferredY : pawn.y + pawn.displayHeight / 2 + gapAbovePawn;

    // origin (0.5, 1) anchors the box's bottom-center at the container's local (0,0) so it grows
    // UPWARD from that point (the normal case, above the pawn); flipped to (0.5, 0) - growing
    // DOWNWARD instead - when placed below the pawn.
    const background = this.add.rectangle(0, 0, boxWidth, boxHeight, 0x222222, 0.9).setOrigin(0.5, fitsAbove ? 1 : 0);
    background.setStrokeStyle(RENDER_SCALE, 0xffffff, 0.5);

    let cursorY = fitsAbove ? -boxHeight + paddingY : paddingY;
    for (const textObj of textObjects) {
      textObj.setY(cursorY);
      cursorY += textObj.height + lineGap;
    }

    this.pawnTooltip = this.add.container(containerX, containerY, [background, ...textObjects]).setDepth(1000);
  }

  private hidePawnTooltip() {
    this.pawnTooltip?.destroy();
    this.pawnTooltip = null;
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
