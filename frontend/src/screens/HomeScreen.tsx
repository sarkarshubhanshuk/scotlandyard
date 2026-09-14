import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { ApiError, createGame } from "../api/client";
import { BackendUnreachable } from "../components/BackendUnreachable";
import { HowToPlayModal } from "../components/HowToPlayModal";

export function HomeScreen() {
  const navigate = useNavigate();
  const [starting, setStarting] = useState(false);
  // unreachable distinguishes "the fetch itself failed" (ApiError was never constructed - the
  // backend genuinely wasn't reached, so BackendUnreachable's dev-setup hint is relevant) from a
  // real response the server sent on purpose, e.g. limits.py's rate-limit refusal. Without this,
  // a 429's own message ("You have started a lot of games recently...") rendered underneath a
  // "Failed to reach the backend - is it running?" banner that flatly contradicted it, since the
  // backend had very much just answered.
  const [error, setError] = useState<{ message: string; unreachable: boolean } | null>(null);
  const [showHowToPlay, setShowHowToPlay] = useState(false);

  async function handleNewGame() {
    setStarting(true);
    setError(null);
    try {
      const game = await createGame();
      void navigate(`/game/${game.game_id}`);
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      setError({ message, unreachable: !(err instanceof ApiError) });
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
      {/* 350x200 source (data/ui/game_logo.jpg, synced into public/ at build time) - the
          width constraint plus height:auto keeps that aspect ratio intact. */}
      <img src="/game_logo.jpg" alt="Scotland Yard" style={{ width: "min(420px, 80vw)", height: "auto" }} />
      <p>Play Mr. X against 5 AI-driven detectives.</p>
      <button className="sy-button" onClick={() => void handleNewGame()} disabled={starting}>
        {starting ? "Starting..." : "New Game"}
      </button>
      <button className="sy-button" onClick={() => setShowHowToPlay(true)}>
        How to Play
      </button>
      {showHowToPlay && <HowToPlayModal onClose={() => setShowHowToPlay(false)} />}
      {error &&
        (error.unreachable ? (
          <BackendUnreachable error={error.message} />
        ) : (
          <p className="sy-error">{error.message}</p>
        ))}
    </div>
  );
}
