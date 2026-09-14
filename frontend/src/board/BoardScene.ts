import Phaser from "phaser";
import { AGENT_COLORS, DETECTIVE_LABELS } from "../labels";
import { DETECTIVE_IDS, type DetectiveId, type MapData, type MapNode, type PublicGameState } from "../types";
import { BOARD_HEIGHT, BOARD_WIDTH, PAWN_MOVE_DURATION_MS } from "./boardDimensions";

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
const MR_X_COLOR = 0x3a3a3a;
// Semi-transparent on a non-surfacing round - a reminder to the human Mr. X player that
// detectives don't currently know this position, not an actual information-hiding mechanism
// (see renderPawns()'s own comment on why exposing current_node here is safe at all).
const MR_X_HIDDEN_ALPHA = 0.4;
// The "whose turn is it" halo (and the legal-target halo below) is drawn with Phaser Graphics
// rather than an image asset (a prior attempt using data/ui/halo.png looked too large and showed
// faint checkerboard patches where its matted-out background wasn't fully transparent) - a plain
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
// TURN_HALO_* used to be MR_X_HALO_* - Mr. X's own always-on halo, generalized (ADR-0011) into a
// marker for whichever pawn currently has the turn: Mr. X while he decides his move, then each
// detective in turn while it deliberates (agents.py:turn_node), cycling back to Mr. X once the
// round resolves. See renderTurnHalo().
const TURN_HALO_THICKNESS = 3;
const TURN_HALO_COLOR = 0xbfe6ff;
const TURN_HALO_CORE_COLOR = 0xffffff;
// The halo's idle "breathing" pulse: scales up from 1x to 1.15x and back down, looping, while
// its pawn sits stationary. TURN_HALO_PULSE_DURATION_MS is ONE direction (1 -> 1.15, or
// 1.15 -> 1) - yoyo:true (see renderTurnHalo) plays it back in reverse for the other half of
// the cycle, so a full up-and-down loop takes twice this.
const TURN_HALO_PULSE_MAX_SCALE = 1.15;
const TURN_HALO_PULSE_DURATION_MS = 900;
// A legal-target halo mirrors the turn halo - same TURN_HALO_COLOR (so it reads as the same kind
// of marker rather than a differently-colored one) and same per-node base radius (so every node's
// own layers stay fully visible inside it, with zero gap, whichever tier that node is) - at 3/4
// its thickness so it reads as a lighter destination marker rather than a "whose turn" indicator.
// Alpha varies by selection state (see paintHighlights()) rather than a separate
// differently-colored "selected" style: LEGAL_TARGET_ALPHA with nothing selected, brightened to
// LEGAL_TARGET_SELECTED_ALPHA for the one node currently pending a ticket choice, and dimmed to
// LEGAL_TARGET_UNSELECTED_ALPHA for every other candidate while that pick is in progress.
const LEGAL_TARGET_HALO_THICKNESS = TURN_HALO_THICKNESS * 0.75;
const LEGAL_TARGET_ALPHA = 0.9;
const LEGAL_TARGET_SELECTED_ALPHA = 1;
const LEGAL_TARGET_UNSELECTED_ALPHA = 0.5;

// EVERY layer's depth, explicitly - nothing here may rely on insertion order.
//
// Phaser depth-sorts the whole display list as soon as ANY object sets a depth, and only then
// falls back to insertion order within one depth. The board background is a full-bleed opaque
// image, so anything sorted below it is not merely behind the artwork - it is completely
// invisible. That is exactly what happened to the turn halo and the last-known ghost when they
// were given negative depths (-1 and -0.5) to "sit behind the pawns": negative put them behind
// the board too, and neither ever rendered. Every layer is named here so the whole stack can be
// read at a glance rather than inferred from the order calls happen to be made in.
const DEPTH_BOARD = 0; // the map artwork and the (transparent) node hit circles
const DEPTH_HALO = 1; // turn halo + legal-target rings: above the map, under the pawns
const DEPTH_GHOST = 2; // Mr. X's last-known-location outline: above the halos, under the pawns
const DEPTH_PAWN = 3; // every real pawn, so a live player always reads as on top
const DEPTH_TOOLTIP = 1000; // hover popups, above absolutely everything

// Sine.easeInOut eases into and out of the motion rather than moving at a constant speed. The
// duration itself lives in boardDimensions.ts (the Phaser-free module) because useRoundStream
// needs it too - see its comment there. Shared by Mr. X and all five detectives: every pawn
// moves the same way, so a detective's move reads as the same kind of event as Mr. X's.
const PAWN_MOVE_EASE = "Sine.easeInOut";

export interface BoardSceneData {
  mapData: MapData;
  gameState: PublicGameState;
  onNodeClick?: (nodeId: number) => void;
  // Fired when a pawn finishes animating to a new node ("mr_x" or a DetectiveId). This is what
  // releases the next detective's turn - see useRoundStream's own handler and ADR-0010. Only
  // ever fired for an actual move, never for a pawn that was simply placed.
  onPawnSettled?: (pawnId: string) => void;
  // Whose pawn the turn halo belongs on right now - "mr_x", a DetectiveId, or null between
  // turns (round just resolved, or a detective's own turn already ended but the next one's
  // turn_started hasn't fired yet). See renderTurnHalo().
  activeTurnPawnId?: string | null;
  highlightedNodeIds?: number[];
  selectedNodeId?: number | null;
}

export class BoardScene extends Phaser.Scene {
  private mapData!: MapData;
  private nodesById = new Map<number, MapNode>();
  private gameState!: PublicGameState;
  private onNodeClick?: (nodeId: number) => void;
  private onPawnSettled?: (pawnId: string) => void;
  private pawns = new Map<string, Phaser.GameObjects.Image>();
  // Mr. X's "last known location" ghost marker (ADR-0012) - a single reused Image, shown/hidden
  // and repositioned rather than destroyed and recreated, same reuse discipline as every pawn.
  private lastKnownGhost: Phaser.GameObjects.Image | null = null;
  // Whose pawn currently has the turn halo - see BoardSceneData.activeTurnPawnId.
  private activeTurnPawnId: string | null = null;
  private turnHalo: Phaser.GameObjects.Graphics | null = null;
  // "pawnId:nodeId" + the radius it was drawn at, so renderTurnHalo() only redraws when the halo
  // actually needs to move to a different pawn/node or resize for a different marker tier,
  // rather than on every render.
  private turnHaloKey: string | null = null;
  private turnHaloRadius: number | null = null;
  // The node each pawn was last actually PLACED at (as opposed to the node the latest game state
  // says it is on) - renderPawn() diffs against this to tell "it just moved, animate it" apart
  // from "an unrelated update arrived and it is exactly where it was". A pawn missing from this
  // map has never been rendered, so its first placement is never mistaken for a move.
  private lastNodes = new Map<string, number>();
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
    this.onPawnSettled = data.onPawnSettled;
    this.activeTurnPawnId = data.activeTurnPawnId ?? null;
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
    // Same silhouette, traced as a dashed unfilled outline rather than a solid shape - Mr. X's
    // "last known location" marker (ADR-0012). Same target resolution as "pawn" for the same
    // reason.
    this.load.svg("pawn_last_known", "/pawn/pawn_last_known.svg", { width: 200, height: 200 });
  }

  create() {
    this.add
      .image(0, 0, "board")
      .setOrigin(0, 0)
      .setDisplaySize(BOARD_WIDTH * RENDER_SCALE, BOARD_HEIGHT * RENDER_SCALE)
      .setDepth(DEPTH_BOARD);

    for (const node of this.mapData.nodes) {
      const pos = this.mapData.positions[String(node.id)];
      if (!pos) continue; // Defensive only - every node.json entry has a matching position entry.

      const hitCircle = this.add.circle(pos.x * RENDER_SCALE, pos.y * RENDER_SCALE, NODE_RADIUS * RENDER_SCALE, 0xffffff, 0);
      hitCircle.setStrokeStyle(1 * RENDER_SCALE, 0x888888, 0.4);
      hitCircle.setDepth(DEPTH_BOARD);
      hitCircle.setInteractive({ useHandCursor: true });
      hitCircle.on("pointerdown", () => this.onNodeClick?.(node.id));
    }

    this.renderPawns();
    this.created = true;
    this.paintHighlights();
  }

  private renderPawns() {
    for (const detId of DETECTIVE_IDS) {
      const detective = this.gameState.detectives[detId];
      if (!detective) continue;
      this.renderPawn(detId, detective.node_id, AGENT_COLORS[detId], DETECTIVE_LABELS[detId]);
    }
    this.renderLastKnownGhost();
    this.renderMrX();
    // Defensive resync, not the primary trigger (that's updateActiveTurn(), called whenever
    // activeTurnPawnId itself changes) - keeps the halo correctly placed even in the
    // never-expected case where a game-state update moved the pawn it's sitting on without an
    // activeTurnPawnId change in between.
    this.renderTurnHalo();
  }

  /**
   * Places or moves one pawn, and reports when it has arrived.
   *
   * Pawns are reused across renders rather than destroyed and recreated, which is what makes a
   * move animate instead of snap: a Phaser tween interpolates from whatever the target's own x/y
   * already is, so there is no need to look up or store the old position.
   *
   * Every pawn goes through here, detectives included. They used to be destroyed and recreated on
   * every render - harmless while all five moved at once at the end of a round, but the reason
   * they teleported. Now that each detective's move arrives on its own (ADR-0010), the same tween
   * Mr. X already had applies to all six pawns.
   *
   * The turn halo is deliberately NOT tracked here as a fellow tween target the way it briefly
   * was for Mr. X alone: a pawn only ever starts moving the instant its own turn ends
   * (agents.py:apply_detective_move), which is the same instant the halo is told to leave it
   * (activeTurnPawnId changes) - so the halo is always static while visible, and renderTurnHalo()
   * positions it independently. See ADR-0011.
   */
  private renderPawn(
    pawnId: string,
    nodeId: number,
    tint: number,
    label: string,
    tooltipLines: string[] = [],
    alpha = 1,
  ): Phaser.GameObjects.Image {
    const pos = this.mapData.positions[String(nodeId)];
    const cx = pos.x * RENDER_SCALE;
    const cy = pos.y * RENDER_SCALE;

    let pawn = this.pawns.get(pawnId);
    const lastNode = this.lastNodes.get(pawnId);
    const justMoved = lastNode !== undefined && lastNode !== nodeId;

    if (!pawn) {
      pawn = this.add
        .image(cx, cy, "pawn")
        .setDisplaySize(PAWN_SIZE * RENDER_SCALE, PAWN_SIZE * RENDER_SCALE)
        .setDepth(DEPTH_PAWN);
      this.pawns.set(pawnId, pawn);
    }
    pawn.setTint(tint);
    pawn.setAlpha(alpha);
    this.makePawnHoverable(pawn, label, nodeId, tooltipLines);

    if (justMoved) {
      this.tweens.killTweensOf(pawn);
      this.tweens.add({
        targets: pawn,
        x: cx,
        y: cy,
        duration: PAWN_MOVE_DURATION_MS,
        ease: PAWN_MOVE_EASE,
        // Fired only on a real move, which is exactly the contract onPawnSettled documents -
        // the backend is waiting on this before it lets the next detective start thinking.
        onComplete: () => this.onPawnSettled?.(pawnId),
      });
    } else {
      pawn.setPosition(cx, cy);
    }

    this.lastNodes.set(pawnId, nodeId);
    return pawn;
  }

  /**
   * Mr. X's "last known location" marker (ADR-0012): a dashed, hollow outline of his pawn,
   * sitting at mr_x.last_known_node whenever that is meaningfully DIFFERENT information from
   * what the real pawn (renderMrX(), below) already shows this round.
   *
   * Hidden in exactly two cases: before Mr. X has ever surfaced (last_known_node is still null -
   * there is nothing to show), and during the round he surfaces at his CURRENT node (last_known_
   * round equals the CURRENT round_number AND last_known_node equals current_node) - his real
   * pawn is already opaque there this round, so a second marker on top of it would be pure
   * redundancy. Every other round it is shown, because last_known_node is the detectives' own
   * last confirmed sighting and may well differ from wherever his real, currently-hidden pawn
   * actually is - including a surfacing round where a double-move was played: that reveals only
   * the *intermediate* hop (game_mechanics.md), so current_node stays hidden and this ghost is
   * exactly what still shows the detectives' one confirmed sighting for the round.
   *
   * Never tweened, unlike a pawn's own move - this marker doesn't represent something walking
   * there, only a static fact ("last confirmed here") that jumps straight to its new value the
   * round it changes.
   */
  private renderLastKnownGhost() {
    const mrX = this.gameState.mr_x;
    const nodeId = mrX.last_known_node;
    const isRedundantWithRealPawn =
      mrX.last_known_round === this.gameState.round_number && nodeId === mrX.current_node;

    if (nodeId === null || isRedundantWithRealPawn) {
      this.lastKnownGhost?.setVisible(false);
      this.lastKnownGhost?.disableInteractive();
      return;
    }

    const pos = this.mapData.positions[String(nodeId)];
    if (!pos) return; // Defensive only - every node has a position entry.

    if (!this.lastKnownGhost) {
      this.lastKnownGhost = this.add
        .image(0, 0, "pawn_last_known")
        .setDisplaySize(PAWN_SIZE * RENDER_SCALE, PAWN_SIZE * RENDER_SCALE)
        // Under every real pawn but over the halos - so a detective standing on the exact node
        // Mr. X was last seen at is unambiguously the one actually there, with this hollow
        // outline still legible around it.
        .setDepth(DEPTH_GHOST);
    }
    this.lastKnownGhost.setPosition(pos.x * RENDER_SCALE, pos.y * RENDER_SCALE);
    this.lastKnownGhost.setVisible(true);
    this.makeGhostHoverable(this.lastKnownGhost, nodeId);
  }

  // Hoverable the same way every pawn is (makePawnHoverable), but as its own method: the ghost's
  // tooltip text is fixed regardless of which pawn it stands in for (there's only ever one),
  // unlike a real pawn's, which always leads with its own name and "Current Node N". Rebound on
  // every render this ghost is visible - nodeId can change (a later surfacing round moves it) -
  // clearing the previous listeners first, same reasoning as makePawnHoverable.
  private makeGhostHoverable(ghost: Phaser.GameObjects.Image, nodeId: number) {
    ghost.setInteractive({ useHandCursor: true });
    ghost.off("pointerover");
    ghost.off("pointerout");
    ghost.on("pointerover", () =>
      this.showPawnTooltip(ghost, ["Detectives last saw Mr. X here", `Node ${nodeId}`]),
    );
    ghost.on("pointerout", () => this.hidePawnTooltip());
  }

  // Mr. X's pawn is always rendered at his real current_node (see serializers.py's own note on
  // why exposing this is safe: the only human-facing client is played BY Mr. X, and detectives
  // are backend-only agents with no client access at all). Alpha is a visual reminder for the
  // human of whether detectives ALSO currently know this position - opaque only when the
  // CURRENT node is what got revealed this round (last_known_round matches round_number, per
  // build_next_round_state, AND last_known_node matches current_node), semi-transparent every
  // other round - not an information-hiding mechanism, since nothing here is ever hidden from
  // the one person who can see this canvas.
  //
  // The node-equality half of that check matters for a double-move played on a surfacing round:
  // game_mechanics.md's mrx_turn documents that such a move reveals only the *intermediate* hop,
  // never the final destination, so current_node stays genuinely hidden even though this round's
  // last_known_round is the current one - without it this pawn would render fully opaque at a
  // node detectives were never actually shown.
  private renderMrX() {
    const mrX = this.gameState.mr_x;
    const isRevealedNow =
      mrX.last_known_round === this.gameState.round_number && mrX.last_known_node === mrX.current_node;
    this.renderPawn(
      "mr_x",
      mrX.current_node,
      MR_X_COLOR,
      "Mr. X",
      [isRevealedNow ? "Visible to Detectives" : "Invisible to Detectives"],
      isRevealedNow ? 1 : MR_X_HIDDEN_ALPHA,
    );
  }

  // The node a given pawn is CURRENTLY standing on, per the latest game state - "mr_x" for
  // Mr. X, otherwise a detective id. Used only by the turn halo (see its own comment for why it
  // never needs to track a mid-flight pawn the way it briefly did for Mr. X alone).
  private currentNodeForPawn(pawnId: string): number | undefined {
    if (pawnId === "mr_x") return this.gameState.mr_x.current_node;
    return this.gameState.detectives[pawnId as DetectiveId]?.node_id;
  }

  /**
   * The ring marking whose turn it currently is: Mr. X while he is deciding his move, then each
   * detective in turn while IT is deciding (agents.py:turn_node), cycling back to Mr. X once the
   * round resolves - driven by activeTurnPawnId (see updateActiveTurn() below). Same dimensions
   * and same per-node radius rule as every other halo on this board (nodeHaloBaseRadius): it sits
   * on the outermost marker tier the node itself draws, with zero gap.
   *
   * Unlike Mr. X's own halo before this existed, this one never needs to travel mid-tween with a
   * moving pawn: a pawn only starts moving the INSTANT its own turn ends
   * (agents.py:apply_detective_move), which is the same instant activeTurnPawnId changes away
   * from it - so whenever this halo is visible, the pawn underneath it is always stationary, and
   * a plain redraw-in-place is all a turn change ever needs. See ADR-0011.
   *
   * Pulses (scales up to TURN_HALO_PULSE_MAX_SCALE and back, looping) whenever that holds -
   * checked via `tweens.isTweening(pawn)` rather than just trusted, because it does NOT quite
   * hold for one edge case: a double-move's intermediate hop on a surfacing round
   * (useMrXMoveWizard.ts:submitDouble) deliberately holds gameState.status at
   * "awaiting_mr_x_move" - and so activeTurnPawnId at "mr_x" - for the ~1s that hop's own pawn
   * tween is still in flight. Checking the pawn's actual tween state catches that case too,
   * rather than only the common one.
   */
  private renderTurnHalo() {
    const pawnId = this.activeTurnPawnId;
    const nodeId = pawnId === null ? undefined : this.currentNodeForPawn(pawnId);

    if (pawnId === null || nodeId === undefined) {
      if (this.turnHalo) this.tweens.killTweensOf(this.turnHalo);
      this.turnHalo?.destroy();
      this.turnHalo = null;
      this.turnHaloKey = null;
      this.turnHaloRadius = null;
      return;
    }

    const pos = this.mapData.positions[String(nodeId)];
    if (!pos) return; // Defensive only - every node has a position entry.

    const radius = this.nodeHaloBaseRadius(nodeId) + TURN_HALO_THICKNESS / 2;
    const key = pawnId + ":" + nodeId;
    if (this.turnHalo && this.turnHaloKey === key && this.turnHaloRadius === radius) {
      return; // Already drawn exactly where it needs to be.
    }

    // Two concentric strokes at the same radius - a wider, low-alpha one for a soft glow and a
    // thinner full-alpha one for a crisp bright core - stand in for a blurred glow without an
    // actual blur filter. A ring, not a disc, so the pawn and the node's own marker stay visible.
    if (this.turnHalo) this.tweens.killTweensOf(this.turnHalo);
    this.turnHalo?.destroy();
    const halo = this.add.graphics();
    halo.lineStyle(TURN_HALO_THICKNESS * 2 * RENDER_SCALE, TURN_HALO_COLOR, 0.35);
    halo.strokeCircle(0, 0, radius * RENDER_SCALE);
    halo.lineStyle(TURN_HALO_THICKNESS * RENDER_SCALE, TURN_HALO_CORE_COLOR, 0.9);
    halo.strokeCircle(0, 0, radius * RENDER_SCALE);
    halo.setPosition(pos.x * RENDER_SCALE, pos.y * RENDER_SCALE);
    halo.setDepth(DEPTH_HALO);

    // Pulse only while the pawn underneath is actually stationary right now (see this method's
    // own doc comment on why that's checked rather than assumed) - otherwise leave it static at
    // its base scale, matching the pawn's own move looking like the more important motion.
    const pawn = this.pawns.get(pawnId);
    if (!pawn || !this.tweens.isTweening(pawn)) {
      this.tweens.add({
        targets: halo,
        scale: TURN_HALO_PULSE_MAX_SCALE,
        duration: TURN_HALO_PULSE_DURATION_MS,
        ease: "Sine.easeInOut",
        yoyo: true,
        repeat: -1,
      });
    }

    this.turnHalo = halo;
    this.turnHaloKey = key;
    this.turnHaloRadius = radius;
  }

  /** Called by BoardCanvas whenever whose turn it is changes - "mr_x", a DetectiveId, or null. */
  updateActiveTurn(pawnId: string | null) {
    this.activeTurnPawnId = pawnId;
    if (this.created) this.renderTurnHalo();
  }

  // A pawn's own display bounds (its interactive hit area, since setInteractive() is called with
  // no explicit shape) are used as the hover target rather than a separate invisible hit circle
  // like the node markers get - the pawn image itself is the only thing a player would expect to
  // hover to inspect it. EVERY pawn is now reused across renders (see renderPawn), and the node
  // in its tooltip changes when it moves, so this is called repeatedly on the same object -
  // clearing the previous listeners first is what stops a duplicate pair stacking every render.
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

    this.pawnTooltip = this.add.container(containerX, containerY, [background, ...textObjects]).setDepth(DEPTH_TOOLTIP);
  }

  private hidePawnTooltip() {
    this.pawnTooltip?.destroy();
    this.pawnTooltip = null;
  }

  /**
   * Called by BoardCanvas when a fresh game snapshot arrives, to move pawns without recreating
   * the scene. Under per-turn application (ADR-0010) this now fires once per detective turn as
   * well as once per Mr. X move, and renderPawn turns each into a single pawn's animation.
   */
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
      ring.lineStyle(LEGAL_TARGET_HALO_THICKNESS * RENDER_SCALE, TURN_HALO_COLOR, alpha);
      ring.strokeCircle(cx, cy, legalTargetHaloRadius * RENDER_SCALE);
      ring.setDepth(DEPTH_HALO);
      this.highlights.set(nodeId, ring);
    }
  }
}
