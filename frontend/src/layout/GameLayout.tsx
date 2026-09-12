import type { ReactNode } from "react";

interface GameLayoutProps {
  board: ReactNode;
  sidebar: ReactNode;
}

// Per CLAUDE.md's Phase 4 target architecture: 75% left pane (Phaser board), 25% right pane
// (React UI panels, stacked).
export function GameLayout({ board, sidebar }: GameLayoutProps) {
  return (
    <div style={{ display: "flex", width: "100vw", height: "100vh" }}>
      <div style={{ width: "75%", height: "100%" }}>{board}</div>
      <div
        style={{
          width: "25%",
          height: "100%",
          padding: 16,
          overflowY: "auto",
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
