// board.svg's own viewBox (docs/map/board.svg) - node_positions.json's x/y coordinates are
// already expressed in this same 600x450 pixel space. Kept in this tiny standalone module with
// no Phaser import so GameLayout.tsx (not lazy-loaded) can use it for the board pane's CSS
// aspect-ratio without pulling the entire Phaser library into the main bundle - only
// BoardScene.ts and BoardCanvas.tsx actually need Phaser, and both are lazy-loaded together via
// GameScreen.tsx's React.lazy() call.
export const BOARD_WIDTH = 600;
export const BOARD_HEIGHT = 450;
