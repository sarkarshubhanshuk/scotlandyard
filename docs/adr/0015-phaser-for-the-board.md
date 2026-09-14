# ADR-0015 — Phaser for the board, not DOM or SVG

- **Status:** Accepted (2026-09-14). Records a choice made during Phase 4 that was never written
  down; the consequences below were already being lived with, scattered across
  `frontend/README.md` and four issue entries.
- **Area:** `frontend/src/board/BoardScene.ts`, `BoardCanvas.tsx`, `boardDimensions.ts`,
  `frontend/package.json`, `.github/workflows/ci.yml`

## Context

The board is a 600×450 static SVG image with 199 fixed node positions, six pawns, and a handful
of rings drawn around nodes. What it has to do:

- place pawns at node coordinates and **tween** them between nodes (ADR-0010 paces a round on
  exactly this animation, and the backend waits on an ack that the tween finished);
- give every node a click/tap target — at 199 nodes on a board scaled to fit a pane, each is only
  a few CSS pixels across;
- draw halos: the turn ring with an idle pulse (ADR-0011), and a set of legal-destination rings;
- draw a dashed "last known location" outline (ADR-0012);
- show a tooltip on pawn hover;
- stay crisp when the 600×450 logical board is scaled up to fill a ~1000px pane.

This is a real-time-ish rendering job with a fixed coordinate space, which is what a 2D game
engine is for — and Phaser is the default reach in that category. It was adopted without the
alternative being seriously costed.

## Decision

Render the board with **Phaser 4**, in a single `Scene` mounted once and driven imperatively.

React never renders into the canvas. `BoardCanvas.tsx` creates the `Phaser.Game` exactly once
(empty dependency array) and pushes prop changes into the running scene through
`updateGameState` / `updateHighlights` / `updateActiveTurn`. The ticket-choice popup is a plain
HTML overlay positioned in percentages of the board's own dimensions, not a Phaser object, so it
can hold real form controls.

## Alternatives considered

**Absolutely-positioned DOM elements over the SVG, with CSS transitions.** Each pawn is a `<div>`
or `<img>` at a percentage offset; movement is a `transform` transition; halos are bordered
circles; hit targets are real DOM nodes with real focus and pointer semantics.

This would have been enough. 199 nodes and six pawns is nothing for the DOM, the animation is a
single eased translate, and `transitionend` gives the same "pawn settled" signal
`BoardScene`'s tween callback provides. It would also have cost **none** of the consequences
below: no bundle problem, no depth system, no canvas/StrictMode interaction, and accessibility
and hit-testing would have come free rather than needing `NODE_RADIUS` to be padded well beyond
the drawn marker so taps land.

**Inline SVG with a small animation helper.** Same properties as the DOM option, with cleaner
shape drawing (the dashed ghost outline is literally an SVG path already — `pawn_last_known.svg`)
and native scaling, at the cost of hand-rolling the tween loop.

Neither was rejected on merit. Phaser was chosen because the board *reads* as a game board, and
that framing made a game engine feel like the obvious tool. **On the evidence since, DOM or SVG
would probably have been the better call** — the engine's capabilities that justify its weight
(a scene graph, a physics system, sprite batching, an asset pipeline, a game loop) are ones this
board does not use.

## Consequences

**The bundle is dominated by it.** Phaser is ~1.4 MB, roughly 5× the rest of the application.
`GameScreen.tsx` lazy-loads `BoardCanvas` so it only downloads when a game is entered, which
keeps the entry chunk near 245 kB. That boundary is invisible and easy to break: any module
imported by non-lazy code must not transitively import Phaser, which is why `boardDimensions.ts`
and `labels.ts` exist as deliberately Phaser-free modules. CI fails the build if the main chunk
exceeds 600 kB, because nothing else would notice (ISSUE-019).

**Depth must be managed explicitly.** Phaser depth-sorts the entire display list as soon as *any*
object sets a depth. The board background is full-bleed and opaque, so anything sorted below it
is invisible rather than merely behind the artwork — which is exactly what happened when the turn
halo and the last-known ghost were given negative depths to "sit behind the pawns" (ISSUE-038).
Every layer is now a named constant.

**Resolution is manual.** `Phaser.Scale.FIT` stretches the framebuffer to the CSS box, so the
fixed 600×450 buffer was being magnified ~2× and looked blurred. The fix is a `RENDER_SCALE` of 3
applied to every drawn coordinate and radius (ISSUE-018). A DOM/SVG board would have been
resolution-independent by construction.

**It interacts badly with StrictMode.** React 19's dev-mode double-invoke can leave two canvases
stacked, the visible one dead, silently swallowing hover and clicks (ISSUE-024, still open). The
diagnostic is `document.querySelectorAll('canvas').length`.

**It is effectively untestable in the existing toolchain.** The hooks are covered by Vitest
(ADR-0016), but a Phaser canvas needs a browser and a different class of tool, so `BoardScene` —
600 lines, the largest single file in the frontend — has no automated coverage at all.

**Accessibility is opt-out rather than opt-in.** Canvas content is invisible to assistive
technology and unreachable by keyboard. A DOM board would have been focusable and labelled for
free; here, any keyboard path has to be built alongside the canvas as a parallel affordance.

**Reversing this is a contained rewrite, not a migration.** `BoardScene.ts` and `BoardCanvas.tsx`
are the only Phaser-importing modules, and the interface they expose to the rest of the app is
four imperative methods plus two callbacks (`onNodeClick`, `onPawnSettled`). A DOM or SVG
implementation behind that same interface would not touch a single hook, component, or type.
That is the main reason this ADR records the trade rather than proposing we act on it now: the
cost of the current choice is paid, bounded, and documented, and the cost of changing it is
knowable whenever it stops being worth paying.
