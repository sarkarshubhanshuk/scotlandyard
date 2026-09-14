import { lazy, Suspense, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ApiError, getGame, getMap } from "../api/client";
import { BackendUnreachable } from "../components/BackendUnreachable";
import { ChatLog } from "../components/ChatLog";
import { GameOverBanner } from "../components/GameOverBanner";
import { TicketInventory } from "../components/TicketInventory";
import { TravelLog } from "../components/TravelLog";
import { useMrXMoveWizard } from "../hooks/useMrXMoveWizard";
import { useRoundStream } from "../hooks/useRoundStream";
import { GameLayout } from "../layout/GameLayout";
import { AGENT_COLORS, AGENT_SHORT_LABEL_TO_ID, toCssColor } from "../labels";
import type { MapData, PublicGameState } from "../types";

// Lazy-loaded so Phaser (the bulk of the production bundle - see Vite's own chunk-size warning)
// only ever downloads once a game is actually entered, not on the home screen.
const BoardCanvas = lazy(() => import("../board/BoardCanvas").then((m) => ({ default: m.BoardCanvas })));

// Matches any detective's short callsign ("Red", "Blue", ...) wherever it appears in the
// sidebar's "Ongoing actions" label, so that name can be colored to match its pawn - mirrors
// ChatLog's own AGENT_NAME_PATTERN/ColoredLine, scoped to the short form this label uses instead
// of the full "Agent Red" display name ChatLog/tooltips use.
const AGENT_SHORT_NAME_PATTERN = new RegExp(
  `\\b(${Object.keys(AGENT_SHORT_LABEL_TO_ID)
    .map((name) => name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"))
    .join("|")})\\b`,
  "g",
);

// The label never mentions more than one detective (whichever one's turn it currently is), but
// splits generically on any of the five names rather than assuming that, so it stays correct if
// a future phase ever names more than one.
function OngoingActionText({ text }: { text: string }) {
  return (
    <>
      {text.split(AGENT_SHORT_NAME_PATTERN).map((part, i) => {
        const agentId = AGENT_SHORT_LABEL_TO_ID[part];
        if (!agentId) return part;
        return (
          <span key={i} style={{ color: toCssColor(AGENT_COLORS[agentId]) }}>
            {part}
          </span>
        );
      })}
    </>
  );
}

export function GameScreen() {
  const { gameId } = useParams<{ gameId: string }>();
  const [gameState, setGameState] = useState<PublicGameState | null>(null);
  const [mapData, setMapData] = useState<MapData | null>(null);
  // unreachable distinguishes "the fetch itself failed" from a real response the server sent on
  // purpose (a 403 "belongs to another player", a 429 rate limit, ...) - see HomeScreen's own
  // copy of this same fix for why conflating the two is actively misleading.
  const [error, setError] = useState<{ message: string; unreachable: boolean } | null>(null);
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
          const message = err instanceof Error ? err.message : String(err);
          setError({ message, unreachable: !(err instanceof ApiError) });
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
      <div style={{ padding: "var(--space-5)" }}>
        <p>
          Game <code>{gameId}</code> was not found - it may have ended when the backend process
          restarted, or been swept after sitting idle (games are in-memory only).
        </p>
        <Link to="/">Start a new game</Link>
      </div>
    );
  }

  if (error) {
    return error.unreachable ? (
      <BackendUnreachable error={error.message} onRetry={() => setRetryToken((t) => t + 1)} />
    ) : (
      <div className="sy-error">
        <p>{error.message}</p>
        <Link to="/">Start a new game</Link>
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
    <LoadedGame
      key={gameId}
      gameId={gameId}
      mapData={mapData}
      gameState={gameState}
      // setGameState's own state is PublicGameState | null, but LoadedGame only ever renders
      // once it is non-null and only ever sets a real snapshot, so the updater it passes can
      // safely assume a non-null previous value.
      onGameStateChange={setGameState as LoadedGameProps["onGameStateChange"]}
    />
  );
}

interface LoadedGameProps {
  gameId: string;
  mapData: MapData;
  gameState: PublicGameState;
  // Accepts an updater as well as a plain snapshot: useRoundStream applies each detective's move
  // as it streams in (ADR-0010), and those land against whatever the previous turn left behind
  // rather than against the state of the render that opened the stream.
  onGameStateChange: (
    update: PublicGameState | ((prev: PublicGameState) => PublicGameState),
  ) => void;
}

function LoadedGame({ gameId, mapData, gameState, onGameStateChange }: LoadedGameProps) {
  const wizard = useMrXMoveWizard(gameId, gameState, onGameStateChange);
  const roundStream = useRoundStream(gameId, gameState, onGameStateChange);

  // Whose pawn the board's "active turn" halo belongs on right now - cycling Mr. X -> Agent Red
  // -> ... -> Agent Purple -> (next round) Mr. X, same order the backend actually plays in.
  // gameState.status is the source of truth for "is it Mr. X's turn": the halo belongs on him
  // for the whole time status is "awaiting_mr_x_move", with no event needed to turn it on (it's
  // simply true the instant a fresh round starts) or off (submitting his move flips status away
  // immediately, before the round stream even opens). While detectives are moving, ownership
  // comes from roundStream's own turn_started/turn_decision tracking instead - gated on status
  // here too, so a detective from a round that has already ended can never leak through.
  const activeTurnPawnId =
    gameState.status === "awaiting_mr_x_move"
      ? "mr_x"
      : gameState.status === "detective_loop_running"
        ? roundStream.activeTurnDetective
        : null;

  // The sidebar's "Ongoing actions" line. wizard.movingHop takes priority over gameState.status
  // whenever it's set: a double-move's intermediate hop deliberately holds status back at
  // "awaiting_mr_x_move" until its second leg lands (see useMrXMoveWizard's own comment on why),
  // so status alone can't tell "still picking a move" apart from "first hop already animating" -
  // movingHop can. Once it clears (both for a single move and after a double-move's second leg),
  // this falls through to whatever gameState.status/roundStream actually says next.
  const ongoingActionLabel = wizard.movingHop
    ? `Mr. X's turn - Mr. X is moving from ${wizard.movingHop.from} to ${wizard.movingHop.to}`
    : gameState.status === "awaiting_mr_x_move"
      ? `Mr. X's turn - Waiting for user input, currently at ${gameState.mr_x.current_node}`
      : gameState.status === "detective_loop_running"
        ? roundStream.stageLabel
        : gameState.status.replaceAll("_", " ");

  return (
    <>
      {gameState.status === "game_over" && (
        <GameOverBanner
          winner={gameState.winner}
          roundNumber={gameState.round_number}
          winningDetective={gameState.winning_detective}
        />
      )}
      <GameLayout
        board={
          <Suspense fallback={<div style={{ padding: 24 }}>Loading board...</div>}>
            <BoardCanvas
              mapData={mapData}
              gameState={gameState}
              wizard={wizard}
              onPawnSettled={roundStream.handlePawnSettled}
              activeTurnPawnId={activeTurnPawnId}
            />
          </Suspense>
        }
        sidebar={
          <>
            {/* Header + Ticket Inventory grouped with their own tighter gap, distinct from the
                sidebar's regular section-to-section gap (set on GameLayout's outer flex column). */}
            <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              <h2 style={{ margin: 0, display: "flex", alignItems: "center", minWidth: 0 }}>
                {/* Round number stays at h2's own (larger, bold-by-default) size; the ongoing-
                    action label gets its own smaller, constant size regardless of phase.
                    display:flex + alignItems:center is what actually centers the shorter span
                    against the taller one - vertical-align is defined relative to the parent line
                    box's own baseline/x-height, not to a sibling's box, so setting it on just one
                    span (as this used to) doesn't align it to the OTHER span at all. The
                    separating space lives inside the second span's own text (rather than as a
                    bare text node between the two spans) because a flex container drops a
                    whitespace-only text node entirely, which would have closed the gap and
                    shifted this label left.

                    A narrow sidebar (a laptop at 150% display scaling measured ~210px of sidebar
                    width against a board pane sized from viewport HEIGHT, not width) used to wrap
                    this onto a second line - shrinking the font only delayed that, it never
                    prevented it. flexShrink: 0 on "Round N" keeps the one thing worth always
                    reading intact; minWidth: 0 + overflow/ellipsis on the status span is what
                    actually forces a single line, by truncating instead of wrapping when the two
                    together don't fit. */}
                <span style={{ fontWeight: 700, flexShrink: 0 }}>Round {gameState.round_number}</span>
                <span
                  // The concise counterpart to ChatLog's role="log": one line naming whose turn
                  // it is and what they are doing. A screen-reader user tracking the round wants
                  // this far more often than the full rationales.
                  role="status"
                  style={{
                    fontSize: 13,
                    fontWeight: 400,
                    color: "var(--color-text-muted)",
                    minWidth: 0,
                    overflow: "hidden",
                    textOverflow: "ellipsis",
                    whiteSpace: "nowrap",
                  }}
                >
                  {" - "}
                  <OngoingActionText text={ongoingActionLabel} />
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
