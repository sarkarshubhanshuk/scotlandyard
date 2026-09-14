import { useEffect, useState, type ReactNode } from "react";

/**
 * The rules, five paged screens deep, opened from the main menu.
 *
 * Paged rather than one long scroll (project owner's call), with a FIXED body height so the
 * card never resizes as you step through it - screens 1, 2 and 5 are a few lines while screen 4
 * shows a screenshot, and a modal that jumped between those sizes on every Next would be
 * unpleasant to read. Screen 4 sub-pages through its four screenshots on the same Back/Next.
 */

// A window onto the real board (data/board/board.svg), cropped by viewBox rather than by
// shipping four more image files: the whole board renders once and each figure shows its own
// slice of it, so these stay crisp at any size and can never drift out of sync with the board
// the game actually draws. The boxes are in board.svg's own 600x450 coordinate space - the same
// space data/board/node_positions.json uses, so each was derived directly from the two nodes it
// needs to frame.
//
// Each window is sized to its own pair rather than to a common zoom: nodes 150-151 sit 25 units
// apart while the 115-118 ferry spans 125, so one shared window would either crop the ferry in
// half or shrink the taxi pair to a speck. Framing each properly matters more than matching
// magnifications.
const CONNECTION_EXAMPLES = [
  { box: "147.5 295 80 60", label: "Taxi", detail: "yellow", nodes: "150-151",
    ends: [[175, 325], [200, 325]] },
  // Shifted left of centre on purpose: the board's drawn area stops short of x=600, so a window
  // centred on this pair would show a strip of dead space down its right edge.
  { box: "505 146 78 58", label: "Bus", detail: "green", nodes: "72-107",
    ends: [[550, 150], [575, 200]] },
  // 79-111 rather than the longer 13-46: metro connections are drawn as continuous Underground
  // routes linking red-ringed stations, never as one segment per connection, so a pair far apart
  // shows two ringed nodes with no visible line between them. This pair is close enough that the
  // red dashed run joining them sits inside the frame.
  { box: "167.5 166 90 68", label: "Metro", detail: "red dashed", nodes: "79-111",
    ends: [[200, 175], [225, 225]] },
  { box: "357.5 165 160 120", label: "Boat", detail: "black dashed", nodes: "115-118",
    ends: [[375, 225], [500, 225]] },
];

// Magenta appears nowhere on the board itself, so a ring in it can only ever read as "this one".
// The wider windows (the metro run spans 150 units, the ferry 125) would otherwise leave you
// guessing which of a dozen visible lines the caption is talking about.
const ENDPOINT_RING = "#f0f";

const MOVE_STEPS = [
  { src: "/howto/howto-1-destinations.webp", caption: "Available destinations, ringed around you" },
  { src: "/howto/howto-2-select.webp", caption: "Click one - the rest dim" },
  { src: "/howto/howto-3-ticket.webp", caption: "Pick your ticket" },
  { src: "/howto/howto-4-moved.webp", caption: "You're there. Ticket spent." },
];

const cellStyle = { padding: "3px 10px", textAlign: "right" } as const;
const headStyle = { ...cellStyle, textAlign: "left", fontWeight: 600 } as const;

function ConnectionFigure({ box, label, detail, nodes, ends }: (typeof CONNECTION_EXAMPLES)[number]) {
  // The radius is fixed in BOARD units so the ring always clears the node marker it is pointing
  // at (the widest tier is r=12.5); only the stroke scales with the window, so it stays visible
  // on the wide ferry crop without turning into a blob on the tight taxi one.
  const windowWidth = Number(box.split(" ")[2]);
  const stroke = Math.max(1.5, windowWidth / 40);
  return (
    <figure style={{ margin: 0, textAlign: "center" }}>
      <svg
        viewBox={box}
        width={112}
        height={84}
        style={{ border: "1px solid var(--color-border)", borderRadius: 4, display: "block" }}
        role="img"
        aria-label={`A ${detail} ${label.toLowerCase()} connection between nodes ${nodes}`}
      >
        <image href="/board/board.svg" x={0} y={0} width={600} height={450} />
        {ends.map(([cx, cy]) => (
          <circle
            key={`${cx},${cy}`}
            cx={cx}
            cy={cy}
            r={14.5}
            fill="none"
            stroke={ENDPOINT_RING}
            strokeWidth={stroke}
          />
        ))}
      </svg>
      <figcaption style={{ fontSize: 12, marginTop: 4 }}>
        <strong>{label}</strong>
        <br />
        <span style={{ color: "var(--color-text-muted)" }}>{detail}</span>
      </figcaption>
    </figure>
  );
}

type Step = {
  title: string;
  body?: ReactNode;
  intro?: ReactNode;
  pages?: { src: string; caption: string }[];
};

const STEPS: Step[] = [
  {
    title: "You are Mr. X",
    body: (
      <p>
        You're Mr. X, hunted by five AI detectives across 199 locations. <strong>Survive all 24
        rounds to win.</strong> They win by landing on your node — or by leaving you nowhere to
        move.
      </p>
    ),
  },
  {
    title: "How a round works",
    body: (
      <>
        <p>
          You move first, then the five detectives in order. You're hidden — except on{" "}
          <strong>rounds 3, 8, 13, 18 and 24</strong>, when you must surface.
        </p>
        <p>
          Between reveals they deduce where you are from the tickets you spend. Their reasoning
          appears live in the Chat Log.
        </p>
      </>
    ),
  },
  {
    title: "Tickets",
    body: (
      <>
        <p style={{ marginTop: 0 }}>Every move spends a ticket matching the connection you travel.</p>
        <div style={{ display: "flex", justifyContent: "space-between", gap: 6, margin: "12px 0" }}>
          {CONNECTION_EXAMPLES.map((c) => (
            <ConnectionFigure key={c.label} {...c} />
          ))}
        </div>
        <table style={{ borderCollapse: "collapse", fontSize: 13, width: "100%" }}>
          <thead>
            <tr>
              <th style={headStyle}>Player</th>
              <th style={cellStyle}>Taxi</th>
              <th style={cellStyle}>Bus</th>
              <th style={cellStyle}>Metro</th>
              <th style={cellStyle}>Black</th>
              <th style={cellStyle}>Double</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td style={headStyle}>You</td>
              <td style={cellStyle}>2</td>
              <td style={cellStyle}>5</td>
              <td style={cellStyle}>3</td>
              <td style={cellStyle}>5</td>
              <td style={cellStyle}>2</td>
            </tr>
            <tr>
              <td style={headStyle}>Each detective</td>
              <td style={cellStyle}>11</td>
              <td style={cellStyle}>8</td>
              <td style={cellStyle}>4</td>
              <td style={cellStyle}>—</td>
              <td style={cellStyle}>—</td>
            </tr>
          </tbody>
        </table>
        <p style={{ marginBottom: 0 }}>
          <strong>Black</strong> pays for any connection — including the boat routes only you can
          use — and your travel log only ever says "black". <strong>Double-move</strong> takes two
          hops in one round; you get two all game. Every ticket a detective spends comes to you.
        </p>
      </>
    ),
  },
  {
    title: "Making a move",
    intro: (
      <p style={{ marginTop: 0 }}>
        Your available destinations are ringed in light blue. Click one, choose which ticket to
        spend, and your pawn moves. Tick <strong>Double move</strong> in the popup to chain a
        second hop.
      </p>
    ),
    // Paged through by the SAME Back/Next buttons the five screens use, rather than shown as a
    // grid of four: these are portrait crops of a dense board, and at grid size the node numbers
    // are unreadable - which defeats the point of showing a real screenshot at all.
    pages: MOVE_STEPS,
  },
  {
    title: "Good luck",
    body: (
      <p>
        That's it. Spend black tickets when it matters, save your double-moves for when the net
        tightens, and get to round 24. Good luck.
      </p>
    ),
  },
];

export function HowToPlayModal({ onClose }: { onClose: () => void }) {
  const [step, setStep] = useState(0);
  // Which screenshot of the "Making a move" walkthrough is showing. Lives here rather than
  // inside that screen so Back/Next can page through it without a second set of controls.
  const [page, setPage] = useState(0);
  const current = STEPS[step];
  const pageCount = current.pages?.length ?? 1;
  const isFirst = step === 0 && page === 0;
  const isLast = step === STEPS.length - 1 && page === pageCount - 1;

  function goNext() {
    if (page < pageCount - 1) return setPage(page + 1);
    if (step < STEPS.length - 1) {
      setStep(step + 1);
      setPage(0);
    }
  }

  function goBack() {
    if (page > 0) return setPage(page - 1);
    if (step > 0) {
      const previous = step - 1;
      setStep(previous);
      // Stepping backwards into a sub-paged screen lands on its LAST page, so Back retraces
      // exactly the path Next took rather than skipping to the front of it.
      setPage((STEPS[previous].pages?.length ?? 1) - 1);
    }
  }

  function goToStep(index: number) {
    setStep(index);
    setPage(0);
  }

  // Esc closes, and the arrow keys page - the same keys the Back/Next buttons are standing in
  // for, so someone reading with the keyboard never has to reach for the mouse.
  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape") onClose();
      if (event.key === "ArrowRight") goNext();
      if (event.key === "ArrowLeft") goBack();
    }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  });

  return (
    <div
      onClick={onClose}
      style={{
        position: "fixed",
        inset: 0,
        background: "rgba(0, 0, 0, 0.55)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        zIndex: 20,
        padding: 16,
      }}
    >
      {/* Stops a click INSIDE the card reaching the backdrop's close handler above. */}
      <div
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label="How to play"
        style={{
          background: "white",
          borderRadius: 8,
          padding: "24px 28px",
          width: "min(560px, 100%)",
          maxHeight: "90vh",
          display: "flex",
          flexDirection: "column",
          gap: 12,
        }}
      >
        <h2 style={{ margin: 0 }}>
          <span style={{ color: "var(--color-text-muted)", fontWeight: 400, fontSize: 14 }}>
            {step + 1} of {STEPS.length}
          </span>
          <br />
          {current.title}
        </h2>

        {/* The fixed-height body: every screen reserves the same space, so paging never resizes
            the card. Only screen 4 comes close to filling it. */}
        <div style={{ height: 380, overflowY: "auto", overflowX: "hidden", fontSize: 14, lineHeight: 1.5 }}>
          {current.body}
          {current.intro}
          {current.pages && (
            <figure style={{ margin: "10px 0 0", textAlign: "center" }}>
              <img
                src={current.pages[page].src}
                alt={current.pages[page].caption}
                style={{
                  maxHeight: 245,
                  maxWidth: "100%",
                  border: "1px solid var(--color-border)",
                  borderRadius: 4,
                }}
              />
              <figcaption style={{ fontSize: 13, marginTop: 6 }}>
                <strong>
                  {page + 1} of {pageCount}.
                </strong>{" "}
                {current.pages[page].caption}
              </figcaption>
            </figure>
          )}
        </div>

        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <button onClick={goBack} disabled={isFirst} style={{ padding: "6px 14px" }}>
            Back
          </button>
          <div style={{ display: "flex", gap: 6, flex: 1, justifyContent: "center" }}>
            {STEPS.map((s, i) => (
              <button
                key={s.title}
                onClick={() => goToStep(i)}
                aria-label={`Go to step ${i + 1}: ${s.title}`}
                aria-current={i === step}
                style={{
                  width: 9,
                  height: 9,
                  padding: 0,
                  borderRadius: "50%",
                  border: "none",
                  cursor: "pointer",
                  background: i === step ? "#333" : "#ccc",
                }}
              />
            ))}
          </div>
          <button onClick={goNext} disabled={isLast} style={{ padding: "6px 14px" }}>
            Next
          </button>
        </div>

        <button onClick={onClose} className="sy-button" style={{ width: "100%" }}>
          Close
        </button>
      </div>
    </div>
  );
}
