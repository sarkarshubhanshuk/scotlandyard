import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { createGame } from "../api/client";

export function HomeScreen() {
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
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        width: "100vw",
        height: "100vh",
        gap: 16,
      }}
    >
      {/* 350x200 source (docs/ui/game_logo.jpg) - width constraint + height:auto keeps that
          aspect ratio intact rather than stretching it. */}
      <img src="/game_logo.jpg" alt="Scotland Yard" style={{ width: "min(420px, 80vw)", height: "auto" }} />
      <p>Play Mr. X against 5 AI-driven detectives.</p>
      <button onClick={() => void handleNewGame()} disabled={starting} style={{ padding: "10px 24px", fontSize: 16 }}>
        {starting ? "Starting..." : "New Game"}
      </button>
      {error && (
        <p style={{ color: "#b00020" }}>
          Failed to reach the backend at http://localhost:8000 - is `uvicorn server:app --reload` running? ({error})
        </p>
      )}
    </div>
  );
}
