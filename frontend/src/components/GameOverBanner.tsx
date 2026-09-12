import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { createGame } from "../api/client";
import type { Winner } from "../types";

export function GameOverBanner({ winner, roundNumber }: { winner: Winner; roundNumber: number }) {
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
        <p style={{ color: "#666", margin: "0 0 20px" }}>
          {winner === "mr_x"
            ? roundNumber >= 24
              ? "Mr. X evaded capture through round 24."
              : "The detectives ran out of moves."
            : // Mr. X's final position is never sent to the client (even at game end), so a
              // "detectives win" cannot be attributed to a specific one of rules.md's three
              // sub-conditions (capture, an intermediate double-move landing on a detective, or
              // Mr. X having no legal move at all) - this copy stays true for any of them.
              "Mr. X was captured, or left with no legal move."}
        </p>
        <button onClick={() => void handleNewGame()} disabled={starting} style={{ padding: "10px 24px", fontSize: 16 }}>
          {starting ? "Starting..." : "New Game"}
        </button>
        {error && (
          <p style={{ color: "#b00020", marginTop: 12, fontSize: 13 }}>
            Failed to start a new game: {error}
          </p>
        )}
      </div>
    </div>
  );
}
