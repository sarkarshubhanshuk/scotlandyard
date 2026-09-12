import { lazy, Suspense, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ApiError, getGame, getMap } from "../api/client";
import { ChatLog } from "../components/ChatLog";
import { GameOverBanner } from "../components/GameOverBanner";
import { TicketInventory } from "../components/TicketInventory";
import { TravelLog } from "../components/TravelLog";
import { useMrXMoveWizard } from "../hooks/useMrXMoveWizard";
import { useRoundStream } from "../hooks/useRoundStream";
import { GameLayout } from "../layout/GameLayout";
import type { MapData, PublicGameState } from "../types";

// Lazy-loaded so Phaser (the bulk of the production bundle - see Vite's own chunk-size warning)
// only ever downloads once a game is actually entered, not on the home screen.
const BoardCanvas = lazy(() => import("../board/BoardCanvas").then((m) => ({ default: m.BoardCanvas })));

export function GameScreen() {
  const { gameId } = useParams<{ gameId: string }>();
  const [gameState, setGameState] = useState<PublicGameState | null>(null);
  const [mapData, setMapData] = useState<MapData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notFound, setNotFound] = useState(false);
  const [retryToken, setRetryToken] = useState(0);

  // Reset everything the moment gameId changes, during render rather than in an effect (React's
  // documented "adjust state when a prop changes" pattern - see useMrXMoveWizard for the same
  // pattern with the same rationale). Without this, navigating from one game to a different one
  // (e.g. "New Game" from GameOverBanner) would render this component's OLD gameState paired
  // with the NEW gameId for one frame, since the fetch effect below only ever overwrites state
  // once it resolves - it never clears it first.
  const [lastGameId, setLastGameId] = useState(gameId);
  if (gameId !== lastGameId) {
    setLastGameId(gameId);
    setGameState(null);
    setMapData(null);
    setError(null);
    setNotFound(false);
  }

  useEffect(() => {
    if (!gameId) return;
    let cancelled = false;
    async function load() {
      try {
        const [game, map] = await Promise.all([getGame(gameId!), getMap(gameId!)]);
        if (cancelled) return;
        setGameState(game);
        setMapData(map);
      } catch (err) {
        if (cancelled) return;
        if (err instanceof ApiError && err.status === 404) {
          setNotFound(true);
        } else {
          setError(err instanceof Error ? err.message : String(err));
        }
      }
    }
    void load();
    return () => {
      cancelled = true;
    };
  }, [gameId, retryToken]);

  if (notFound) {
    return (
      <div style={{ padding: 24 }}>
        <p>
          Game <code>{gameId}</code> was not found - it may have ended when the backend process
          restarted (games are in-memory only).
        </p>
        <Link to="/">Start a new game</Link>
      </div>
    );
  }

  if (error) {
    return (
      <div style={{ padding: 24, color: "#b00020" }}>
        <p>
          Failed to reach the backend at http://localhost:8000 - is `uvicorn server:app --reload`
          running? ({error})
        </p>
        <button onClick={() => setRetryToken((t) => t + 1)}>Retry</button>
      </div>
    );
  }

  if (!gameId || !gameState || !mapData) {
    return <div style={{ padding: 24 }}>Loading game...</div>;
  }

  // key={gameId} additionally guarantees LoadedGame's own hook state (the move wizard, the
  // accumulated chat log entries) starts fresh for every distinct game, rather than carrying
  // over stale in-progress picks or a previous game's debate history.
  return (
    <LoadedGame key={gameId} gameId={gameId} mapData={mapData} gameState={gameState} onGameStateChange={setGameState} />
  );
}

interface LoadedGameProps {
  gameId: string;
  mapData: MapData;
  gameState: PublicGameState;
  onGameStateChange: (gameState: PublicGameState) => void;
}

function LoadedGame({ gameId, mapData, gameState, onGameStateChange }: LoadedGameProps) {
  const wizard = useMrXMoveWizard(gameId, gameState, onGameStateChange);
  const roundStream = useRoundStream(gameId, gameState, onGameStateChange);

  return (
    <>
      {gameState.status === "game_over" && (
        <GameOverBanner winner={gameState.winner} roundNumber={gameState.round_number} />
      )}
      <GameLayout
        board={
          <Suspense fallback={<div style={{ padding: 24 }}>Loading board...</div>}>
            <BoardCanvas mapData={mapData} gameState={gameState} wizard={wizard} />
          </Suspense>
        }
        sidebar={
          <>
            {/* Header + Ticket Inventory grouped with their own tighter gap, distinct from the
                sidebar's regular section-to-section gap (set on GameLayout's outer flex column). */}
            <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              <h2 style={{ margin: 0 }}>
                Round {gameState.round_number}{" "}
                <span style={{ color: "#666", fontWeight: 400 }}>
                  -{" "}
                  {gameState.status === "detective_loop_running"
                    ? roundStream.stageLabel
                    : gameState.status.replaceAll("_", " ")}
                </span>
              </h2>
              <TicketInventory gameState={gameState} />
            </div>
            <ChatLog
              entries={roundStream.entries}
              connectionError={roundStream.connectionError}
              onRetry={roundStream.retry}
            />
            <TravelLog mrX={gameState.mr_x} />
          </>
        }
      />
    </>
  );
}
