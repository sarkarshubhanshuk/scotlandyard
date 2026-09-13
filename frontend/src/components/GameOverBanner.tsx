import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { createGame } from "../api/client";
import { AGENT_COLORS, DETECTIVE_SHORT_LABELS, toCssColor } from "../labels";
import type { DetectiveId, Winner } from "../types";

// Mirrors data/ui/pawn.svg's own two-group shape (colored fill group + black outline group on
// top of the identical geometry) - duplicated rather than loaded from the shared asset because
// that file is designed to be recolored via the --pawn-color CSS custom property when INLINED,
// which only works for markup actually present in the page's own DOM; an <img src="..."> (or a
// Phaser-loaded texture, see BoardScene.ts's own comment on the same constraint) can't see a
// custom property from the embedding page at all. pawn_last_known.svg is an existing precedent
// for tracing this same silhouette a second time for a different rendering need.
function AgentPawnIcon({ color, size = 28 }: { color: string; size?: number }) {
  return (
    <svg viewBox="0 0 100 100" width={size} height={size} aria-hidden="true">
      <g fill={color} stroke="none">
        <circle cx="50" cy="24" r="13" />
        <path d="M38 40 C38 40 30 46 27 58 C24 70 20 82 20 88 C20 92 24 94 28 94 L72 94 C76 94 80 92 80 88 C80 82 76 70 73 58 C70 46 62 40 62 40 C58 44 42 44 38 40 Z" />
      </g>
      <g fill="none" stroke="#000000" strokeWidth={4} strokeLinejoin="round" strokeLinecap="round">
        <circle cx="50" cy="24" r="13" />
        <path d="M38 40 C38 40 30 46 27 58 C24 70 20 82 20 88 C20 92 24 94 28 94 L72 94 C76 94 80 92 80 88 C80 82 76 70 73 58 C70 46 62 40 62 40 C58 44 42 44 38 40 Z" />
      </g>
    </svg>
  );
}

export function GameOverBanner({
  winner,
  roundNumber,
  winningDetective,
}: {
  winner: Winner;
  roundNumber: number;
  // The detective that physically caught Mr. X, when that's how the game ended - null for
  // every other ending (see types.ts:PublicGameState.winning_detective).
  winningDetective: DetectiveId | null;
}) {
  const navigate = useNavigate();
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleNewGame() {
    setStarting(true);
    setError(null);
    try {
      const game = await createGame();
      void navigate(`/game/${game.game_id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setStarting(false);
    }
  }

  return (
    <div
      style={{
        position: "fixed",
        inset: 0,
        background: "rgba(0, 0, 0, 0.55)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        zIndex: 10,
      }}
    >
      <div style={{ background: "white", borderRadius: 8, padding: "32px 40px", textAlign: "center", minWidth: 320 }}>
        <h1 style={{ margin: "0 0 8px" }}>{winner === "mr_x" ? "Mr. X wins!" : "The detectives win!"}</h1>
        {winner === "detectives" && winningDetective ? (
          // Only shown when a detective actually landed on Mr. X (winning_detective is null for
          // the other "detectives win" sub-condition - he simply ran out of legal moves, with
          // nobody to credit). "Agent" stays plain; only the callsign itself is colored, to
          // match the pawn.
          <div style={{ display: "flex", alignItems: "center", justifyContent: "center", gap: 8, margin: "0 0 20px" }}>
            <span style={{ color: "#666" }}>
              Caught by Agent{" "}
              <span style={{ color: toCssColor(AGENT_COLORS[winningDetective]), fontWeight: 600 }}>
                {DETECTIVE_SHORT_LABELS[winningDetective]}
              </span>
            </span>
            <AgentPawnIcon color={toCssColor(AGENT_COLORS[winningDetective])} />
          </div>
        ) : (
          <p style={{ color: "#666", margin: "0 0 20px" }}>
            {winner === "mr_x"
              ? roundNumber >= 24
                ? "Mr. X evaded capture through round 24."
                : "The detectives ran out of moves."
              : // Mr. X's final position is never sent to the client (even at game end), so a
                // "detectives win" without a winningDetective cannot be attributed to a specific
                // one of rules.md's remaining sub-conditions (an intermediate double-move
                // landing on a detective, or Mr. X having no legal move at all) - this copy
                // stays true for either.
                "Mr. X was captured, or left with no legal move."}
          </p>
        )}
        <button onClick={() => void handleNewGame()} disabled={starting} style={{ padding: "10px 24px", fontSize: 16 }}>
          {starting ? "Starting..." : "New Game"}
        </button>
        {error && (
          <p style={{ color: "var(--color-error)", marginTop: 12, fontSize: 13 }}>
            Failed to start a new game: {error}
          </p>
        )}
      </div>
    </div>
  );
}
