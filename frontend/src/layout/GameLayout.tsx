import type { ReactNode } from "react";
import { BOARD_HEIGHT, BOARD_WIDTH } from "../board/BoardScene";

interface GameLayoutProps {
  board: ReactNode;
  sidebar: ReactNode;
}

// Board pane: pinned to the left edge, full viewport height, width derived from the board's own
// aspect ratio (via CSS aspect-ratio, not a fixed %) so it's never letterboxed or stretched -
// BoardCanvas's Phaser.Scale.FIT then exactly fills this pane with zero leftover space, since the
// pane's aspect ratio matches the game's internal resolution exactly. Sidebar pane: takes
// whatever width remains out to the right edge (flex: 1), starting flush against the board with
// no gap. overflow: hidden on the outer row is a belt-and-suspenders guard against a page-level
// scrollbar - the sidebar keeps its own internal overflowY so long content scrolls within itself
// instead of ever pushing the browser page to scroll.
export function GameLayout({ board, sidebar }: GameLayoutProps) {
  return (
    <div style={{ display: "flex", width: "100vw", height: "100vh", overflow: "hidden" }}>
      <div style={{ height: "100%", aspectRatio: `${BOARD_WIDTH} / ${BOARD_HEIGHT}`, flexShrink: 0 }}>{board}</div>
      <div
        style={{
          flex: 1,
          minWidth: 0,
          height: "100%",
          padding: 16,
          overflowY: "auto",
          overflowX: "hidden",
          display: "flex",
          flexDirection: "column",
          gap: 20,
          borderLeft: "1px solid #ddd",
        }}
      >
        {sidebar}
      </div>
    </div>
  );
}
