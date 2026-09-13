import { API_BASE } from "../api/client";

interface BackendUnreachableProps {
  error: string;
  onRetry?: () => void;
}

/**
 * The "can't reach the backend" message, shared by HomeScreen and GameScreen.
 *
 * These were two hand-copied blocks that each hardcoded "http://localhost:8000" in their
 * text - so with VITE_API_BASE_URL pointed elsewhere, both confidently named the wrong URL.
 * It now reports the origin the client is actually configured to call.
 */
export function BackendUnreachable({ error, onRetry }: BackendUnreachableProps) {
  return (
    <div className="sy-error">
      <p>
        Failed to reach the backend at <code>{API_BASE}</code> - is it running? Start it with{" "}
        <code>python -m scotland_yard.server</code> from the <code>backend/</code> directory.
      </p>
      <p style={{ color: "var(--color-text-muted)" }}>({error})</p>
      {onRetry && (
        <button className="sy-button" onClick={onRetry}>
          Retry
        </button>
      )}
    </div>
  );
}
