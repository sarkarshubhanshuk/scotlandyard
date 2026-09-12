import Phaser from "phaser";
import { AGENT_COLORS, DETECTIVE_LABELS } from "../labels";
import { DETECTIVE_IDS, type MapData, type MapNode, type PublicGameState } from "../types";
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
// Mr. X's halo (and the legal-target halo below) is drawn with Phaser Graphics rather than an
// image asset (a prior attempt using docs/ui/halo.png looked too large and showed faint
// checkerboard patches where its matted-out background wasn't fully transparent) - a plain
// stroked circle has no such artifacts and its size is exact rather than approximated from a
// raster's own padding.
//
// board.svg draws every node as one of three concentric marker sizes, layered outer-to-inner as
// red (r=12.5, metro) -> green (r=10.5, bus) -> white-fill-black-stroke (r=7.5 fill, ~8.5 to the
// outer edge of its 2px stroke - or the same footprint with a dashed stroke for the 4 boat-only
// nodes) - each tier only drawn when the node's own allowed_transport includes it, so the
// widest/outermost tier actually visible is metro > bus > taxi. Confirmed by rasterizing
// board.svg to a canvas and radially sampling pixel color away from a sample node of each tier
// until it hit the board's own background grey. So there's no single "widest node marker"
// constant any more - nodeHaloBaseRadius() below picks the right one per node so the halo's
// inner edge sits exactly on that node's own outermost visible layer, whichever tier it is,
// with zero gap.
const NODE_HALO_BASE_RADIUS_BY_TIER = { metro: 12.5, bus: 10.5, taxi: 8.5 };
const MR_X_HALO_THICKNESS = 3;
const MR_X_HALO_COLOR = 0xbfe6ff;
const MR_X_HALO_CORE_COLOR = 0xffffff;
// A legal-target halo mirrors Mr. X's own - same MR_X_HALO_COLOR (so it reads as the same kind
// of marker rather than a differently-colored one) and same per-node base radius (so every node's
// own layers stay fully visible inside it, with zero gap, whichever tier that node is) - at 3/4
// its thickness so it reads as a lighter destination marker rather than a second "Mr. X is here"
// indicator. Alpha varies by selection state (see paintHighlights()) rather than a separate
// differently-colored "selected" style: LEGAL_TARGET_ALPHA with nothing selected, brightened to
// LEGAL_TARGET_SELECTED_ALPHA for the one node currently pending a ticket choice, and dimmed to
// LEGAL_TARGET_UNSELECTED_ALPHA for every other candidate while that pick is in progress.
const LEGAL_TARGET_HALO_THICKNESS = MR_X_HALO_THICKNESS * 0.75;
const LEGAL_TARGET_ALPHA = 0.9;
const LEGAL_TARGET_SELECTED_ALPHA = 1;
const LEGAL_TARGET_UNSELECTED_ALPHA = 0.5;

// Fixed regardless of hop distance (a 1-hop taxi ride and a cross-board double-move both take
// exactly this long to animate) - see renderMrX()'s own comment for why that's straightforward
// to guarantee with a Phaser tween. Sine.easeInOut eases into and out of the motion rather than
// moving at a constant speed.
const MR_X_MOVE_DURATION_MS = 1000;
const MR_X_MOVE_EASE = "Sine.easeInOut";

export interface BoardSceneData {
  mapData: MapData;
  gameState: PublicGameState;
  onNodeClick?: (nodeId: number) => void;
  highlightedNodeIds?: number[];
  selectedNodeId?: number | null;
}

export class BoardScene extends Phaser.Scene {
  private mapData!: MapData;
  private nodesById = new Map<number, MapNode>();
  private gameState!: PublicGameState;
  private onNodeClick?: (nodeId: number) => void;
  private pawns = new Map<string, Phaser.GameObjects.Image>();
  private mrXHalo: Phaser.GameObjects.Graphics | null = null;
  // The node Mr. X's pawn was last actually PLACED at (as opposed to gameState.mr_x.current_node,
  // which is always the latest server truth) - renderMrX() diffs against this to tell "he just
  // moved, animate it" apart from "a detective-only update arrived and he's exactly where he was".
  // null only before the very first render, so that one is never mistaken for a move.
  private lastMrXNode: number | null = null;
  private highlights = new Map<number, Phaser.GameObjects.GameObject>();
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
    this.nodesById = new Map(this.mapData.nodes.map((node) => [node.id, node]));
    this.gameState = data.gameState;
    this.onNodeClick = data.onNodeClick;
    this.latestHighlightedNodeIds = data.highlightedNodeIds ?? [];
    this.latestSelectedNodeId = data.selectedNodeId ?? null;
  }

  // Picks the base radius matching the outermost marker tier board.svg actually draws for this
  // node (see NODE_HALO_BASE_RADIUS_BY_TIER's own comment) - metro > bus > taxi, since each tier
  // is only drawn when present and layered outer-to-inner in that order. Falls back to the
  // narrowest (taxi) tier if the node is somehow missing from mapData.nodes.
  private nodeHaloBaseRadius(nodeId: number): number {
    const node = this.nodesById.get(nodeId);
    if (node?.allowed_transport.includes("metro")) return NODE_HALO_BASE_RADIUS_BY_TIER.metro;
    if (node?.allowed_transport.includes("bus")) return NODE_HALO_BASE_RADIUS_BY_TIER.bus;
    return NODE_HALO_BASE_RADIUS_BY_TIER.taxi;
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
    // Detectives are always fully recreated (no movement animation asked for them) - Mr. X's own
    // pawn/halo are handled separately by renderMrX(), which reuses rather than recreates them
    // so a move can tween smoothly instead of snapping.
    for (const [id, pawn] of this.pawns) {
      if (id === "mr_x") continue;
      pawn.destroy();
      this.pawns.delete(id);
    }
    // The pawn objects a stale tooltip's hover handlers referred to no longer exist once this
    // runs (a fresh game snapshot recreates every detective pawn from scratch) - drop any open
    // tooltip rather than leaving it pointing at a destroyed pawn.
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

    this.renderMrX();
  }

  // Mr. X's pawn is always rendered at his real current_node now (see serializers.py's own note
  // on why exposing this is safe: the only human-facing client is played BY Mr. X, and detectives
  // are backend-only agents with no client access at all). Alpha is a visual reminder for the
  // human of whether detectives ALSO currently know this position - opaque only on the exact
  // round he's surfaced (last_known_round stays stuck on a past round forever after, per
  // build_next_round_state, so this must compare against the CURRENT round, not just check
  // non-null), semi-transparent every other round - not an information-hiding mechanism, since
  // nothing here is ever hidden from the one person who can see this canvas.
  //
  // When current_node has actually changed since the last render, the existing pawn+halo are
  // reused and tweened to the new spot (a Phaser tween interpolates from whatever a target's own
  // x/y property already is, so there's no need to look up or store the OLD position here) rather
  // than destroyed and recreated at the destination - that's what turns a move into a visible
  // animation instead of an instant snap. Every other case (first render, or a same-node update
  // from a detective-only round) just places them directly, which is also what a plain
  // destroy-and-recreate already did before this existed.
  private renderMrX() {
    const mrX = this.gameState.mr_x;
    const pos = this.mapData.positions[String(mrX.current_node)];
    if (!pos) return; // Defensive only - every node has a position entry.

    const cx = pos.x * RENDER_SCALE;
    const cy = pos.y * RENDER_SCALE;
    const isSurfacingRound = mrX.last_known_round === this.gameState.round_number;
    const tooltipLines = [isSurfacingRound ? "Visible to Detectives" : "Invisible to Detectives"];
    const existingPawn = this.pawns.get("mr_x");
    const justMoved = this.lastMrXNode !== null && this.lastMrXNode !== mrX.current_node;

    if (existingPawn && this.mrXHalo && justMoved) {
      const halo = this.mrXHalo;
      this.tweens.killTweensOf(existingPawn);
      this.tweens.killTweensOf(halo);
      this.tweens.add({
        targets: [existingPawn, halo],
        x: cx,
        y: cy,
        duration: MR_X_MOVE_DURATION_MS,
        ease: MR_X_MOVE_EASE,
      });
      existingPawn.setAlpha(isSurfacingRound ? 1 : MR_X_HIDDEN_ALPHA);
      this.makePawnHoverable(existingPawn, "Mr. X", mrX.current_node, tooltipLines);
    } else {
      existingPawn?.destroy();
      this.mrXHalo?.destroy();

      // Drawn before the pawn below so it sits behind it in the display list - a ring around the
      // node, not a disc covering it, so both the pawn and the node's own marker/border remain
      // visible inside it, with zero gap (see nodeHaloBaseRadius()'s own comment). Two concentric
      // strokes at the same radius - a wider, low-alpha one for a soft glow and a thinner
      // full-alpha one for a crisp bright core - stand in for a blurred glow without an actual
      // blur filter. Circles are drawn at the graphics object's own local origin and positioned
      // via setPosition (rather than passing cx/cy straight into strokeCircle) so a later move
      // can tween its x/y like any other GameObject instead of needing a manual per-frame redraw.
      const mrXHaloRadius = this.nodeHaloBaseRadius(mrX.current_node) + MR_X_HALO_THICKNESS / 2;
      const halo = this.add.graphics();
      halo.lineStyle(MR_X_HALO_THICKNESS * 2 * RENDER_SCALE, MR_X_HALO_COLOR, 0.35);
      halo.strokeCircle(0, 0, mrXHaloRadius * RENDER_SCALE);
      halo.lineStyle(MR_X_HALO_THICKNESS * RENDER_SCALE, MR_X_HALO_CORE_COLOR, 0.9);
      halo.strokeCircle(0, 0, mrXHaloRadius * RENDER_SCALE);
      halo.setPosition(cx, cy);
      this.mrXHalo = halo;

      const pawn = this.add
        .image(cx, cy, "pawn")
        .setDisplaySize(PAWN_SIZE * RENDER_SCALE, PAWN_SIZE * RENDER_SCALE)
        .setTint(MR_X_COLOR)
        .setAlpha(isSurfacingRound ? 1 : MR_X_HIDDEN_ALPHA);
      this.makePawnHoverable(pawn, "Mr. X", mrX.current_node, tooltipLines);
      this.pawns.set("mr_x", pawn);
    }

    this.lastMrXNode = mrX.current_node;
  }

  // A pawn's own display bounds (its interactive hit area, since setInteractive() is called with
  // no explicit shape) are used as the hover target rather than a separate invisible hit circle
  // like the node markers get - the pawn image itself is the only thing a player would expect to
  // hover to inspect it. Detectives always call this on a freshly-created pawn, but Mr. X's own
  // pawn can be reused across renders (see renderMrX()) - clearing any previous listeners first
  // keeps this idempotent either way, rather than stacking a duplicate pair on every re-render.
  private makePawnHoverable(pawn: Phaser.GameObjects.Image, label: string, node: number, extraLines: string[] = []) {
    pawn.setInteractive({ useHandCursor: true });
    pawn.off("pointerover");
    pawn.off("pointerout");
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

    // Nothing pending -> every candidate reads at the same neutral alpha. Once one is pending a
    // ticket choice, it brightens to LEGAL_TARGET_SELECTED_ALPHA and every other candidate dims
    // to LEGAL_TARGET_UNSELECTED_ALPHA, so the board itself reflects the popup's current pick.
    for (const nodeId of this.latestHighlightedNodeIds) {
      const pos = this.mapData.positions[String(nodeId)];
      if (!pos) continue;
      const cx = pos.x * RENDER_SCALE;
      const cy = pos.y * RENDER_SCALE;
      const alpha =
        this.latestSelectedNodeId === null
          ? LEGAL_TARGET_ALPHA
          : nodeId === this.latestSelectedNodeId
            ? LEGAL_TARGET_SELECTED_ALPHA
            : LEGAL_TARGET_UNSELECTED_ALPHA;

      const legalTargetHaloRadius = this.nodeHaloBaseRadius(nodeId) + LEGAL_TARGET_HALO_THICKNESS / 2;
      const ring = this.add.graphics();
      ring.lineStyle(LEGAL_TARGET_HALO_THICKNESS * RENDER_SCALE, MR_X_HALO_COLOR, alpha);
      ring.strokeCircle(cx, cy, legalTargetHaloRadius * RENDER_SCALE);
      this.highlights.set(nodeId, ring);
    }
  }
}
