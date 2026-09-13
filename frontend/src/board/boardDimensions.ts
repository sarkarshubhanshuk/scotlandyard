// board.svg's own viewBox (docs/map/board.svg) - node_positions.json's x/y coordinates are
// already expressed in this same 600x450 pixel space. Kept in this tiny standalone module with
// no Phaser import so GameLayout.tsx (not lazy-loaded) can use it for the board pane's CSS
// aspect-ratio without pulling the entire Phaser library into the main bundle - only
// BoardScene.ts and BoardCanvas.tsx actually need Phaser, and both are lazy-loaded together via
// GameScreen.tsx's React.lazy() call.
export const BOARD_WIDTH = 600;
export const BOARD_HEIGHT = 450;

// How long a pawn takes to slide from one node to the next, shared by BoardScene (which runs the
// tween) and useRoundStream (which holds the round stream closed for exactly this long so Mr. X
// finishes moving before Agent Red starts deliberating - ADR-0010). Lives here rather than in
// BoardScene.ts for the same reason the dimensions above do: useRoundStream is in the main
// bundle and must not import Phaser.
//
// Fixed regardless of hop distance - a Phaser tween interpolates by time, not distance. The
// backend's own rules_constants.TURN_ACK_TIMEOUT_SECONDS must stay comfortably above this.
export const PAWN_MOVE_DURATION_MS = 1000;
