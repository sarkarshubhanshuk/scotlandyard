import { useEffect, useRef } from "react";
import type { ChatLogEntry } from "../hooks/useRoundStream";

const KIND_LABELS: Record<ChatLogEntry["kind"], string> = {
  proposal: "Proposals",
  debate: "Debate",
  vote_tally: "Vote",
  round_finalized: "Finalized",
  round_result: "Result",
};

// How close to the bottom (px) counts as "already at the bottom" for auto-scroll purposes.
const NEAR_BOTTOM_THRESHOLD = 24;

interface ChatLogProps {
  entries: ChatLogEntry[];
  connectionError: string | null;
  onRetry: () => void;
}

export function ChatLog({ entries, connectionError, onRetry }: ChatLogProps) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const wasNearBottomRef = useRef(true);

  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    // Only auto-scroll if the user hadn't scrolled up to reread something - otherwise a new
    // event arriving would yank their view back down to the bottom mid-read.
    if (wasNearBottomRef.current) {
      el.scrollTo({ top: el.scrollHeight });
    }
  }, [entries]);

  function handleScroll() {
    const el = scrollRef.current;
    if (!el) return;
    wasNearBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight <= NEAR_BOTTOM_THRESHOLD;
  }

  return (
    <section style={{ display: "flex", flexDirection: "column", minHeight: 0 }}>
      <h3 style={{ margin: "0 0 8px" }}>Chat Log</h3>
      <div
        ref={scrollRef}
        onScroll={handleScroll}
        style={{
          border: "1px solid #ddd",
          borderRadius: 4,
          padding: 8,
          height: 220,
          overflowY: "auto",
          fontSize: 12,
          display: "flex",
          flexDirection: "column",
          gap: 8,
        }}
      >
        {entries.length === 0 && <p style={{ color: "#666", margin: 0 }}>No debate yet.</p>}
        {entries.map((entry) => (
          <div key={entry.id}>
            <div style={{ fontWeight: 600, color: "#444" }}>
              Round {entry.round} - {KIND_LABELS[entry.kind]}
            </div>
            {entry.lines.map((line, i) => (
              <div key={i} style={{ whiteSpace: "pre-wrap" }}>
                {line}
              </div>
            ))}
          </div>
        ))}
      </div>
      {connectionError && (
        <div style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 4 }}>
          <p style={{ fontSize: 12, color: "#b00020", margin: 0 }}>{connectionError}</p>
          <button onClick={onRetry}>Retry</button>
        </div>
      )}
    </section>
  );
}
