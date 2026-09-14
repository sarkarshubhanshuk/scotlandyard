# Frontend — Scotland Yard

The React 19 + Phaser 4 client. The human plays **Mr. X** here; the five detectives are LLM
agents running in the backend and have no client of their own (see
[ADR-0003](../docs/adr/0003-human-mr-x-llm-detectives.md)).

## Commands

| Command | What it does |
|---|---|
| `npm run dev` | Vite dev server on <http://localhost:5173> (runs `sync-assets` first). |
| `npm run build` | Type-check and production build (runs `sync-assets` first). |
| `npm run preview` | Serve the production build — the only way to test without StrictMode. |
| `npm run lint` | ESLint, type-checked rules. |
| `npm test` | Vitest (jsdom), run once. |
| `npm run test:watch` | Vitest in watch mode. |
| `npm run sync-assets` | Copy board/ticket/pawn/how-to-play art from `../data` into `public/`. |

The backend must be running at `http://localhost:8000`. Override with `VITE_API_BASE_URL` in
`.env.local` (see `.env.example`).

## Architecture

```
src/
  screens/      HomeScreen (new game), GameScreen (owns the one PublicGameState)
  layout/       GameLayout — the board/sidebar split
  board/        Phaser: BoardCanvas (React bridge) + BoardScene (the scene itself)
  components/   TicketInventory, ChatLog, TravelLog, GameOverBanner, HowToPlayModal,
                BackendUnreachable
  hooks/        useMrXMoveWizard (the move state machine), useRoundStream (SSE)
  api/          client.ts — the only place that talks to the backend
  test/         fixtures.ts — a PublicGameState builder and a fake EventSource
  types.ts      Hand-written mirror of the backend's payloads
  labels.ts     Agent names/colors, ticket labels/icons — deliberately Phaser-free
```

**State** lives in `GameScreen`/`LoadedGame` and is passed down; there is no global store
([ADR-0008](../docs/adr/0008-no-global-state-library-on-the-frontend.md)). Both hooks report a
new state via the same `onGameStateChange` callback, which is also what resets the move wizard
between rounds.

**The board is outside React's render path.** One Phaser `Scene` is mounted once and updated
imperatively via `updateGameState`/`updateHighlights`, never recreated per render.

## Constraints worth knowing before you edit

### 1. Don't pull Phaser into the main chunk

`GameScreen.tsx` lazy-loads `BoardCanvas`, so Phaser (~1.4 MB) downloads only when a game is
entered. This is easy to undo by accident: anything imported by a component *outside* that lazy
boundary must not transitively import `board/BoardScene.ts` or any other Phaser-importing
module. `board/boardDimensions.ts` and `labels.ts` exist specifically as Phaser-free modules
that non-lazy code can safely import from.

Verify with `npm run build` — **the main chunk should stay around 245 kB**, not balloon to
~1.6 MB. See [ISSUE-019](../docs/issues/known_issues.md).

### 2. `types.ts` is hand-maintained

The backend is Starlette, not FastAPI, so there is no OpenAPI schema to generate from
([ADR-0002](../docs/adr/0002-starlette-over-fastapi.md)). When `backend/scotland_yard/serializers.py`
changes shape, `types.ts` must be updated by hand — nothing will catch it otherwise.
`labels.ts` similarly mirrors backend constants (`MAX_ROUND`, `SURFACING_ROUNDS`,
`DETECTIVE_LABELS`); each mirror says which backend symbol it tracks.

### 3. Art in `public/` is generated, not source

`scripts/sync-assets.mjs` copies `board/`, `pawn/`, `tickets/` and `game_logo.jpg` from the
top-level `data/` directory on every `dev`/`build`. Those paths are gitignored — **edit the
originals in `data/`**, not the copies. `favicon.svg` is real committed frontend source and is
not touched by the sync.

### 4. StrictMode double-invoke

`main.tsx` uses `<StrictMode>`, so in development React mounts → unmounts → remounts effects
once. This has bitten this project twice:

- [ISSUE-024](../docs/issues/known_issues.md) (**open**) — two Phaser canvases can end up
  stacked, the visible one being the dead one, which silently swallows hover and clicks. If
  board interaction mysteriously stops working in `npm run dev`, check
  `document.querySelectorAll('canvas').length` before debugging anything else, and confirm
  against `npm run preview`, which does not double-invoke.
- [ISSUE-027](../docs/issues/known_issues.md) (fixed) — two `EventSource` connections per round
  made the backend run the detective loop twice. Fixed server-side, since a client-side guard
  would not have helped with two browser tabs.

## Tests

`npm test` (Vitest, jsdom) covers the two hooks and nothing else, on purpose.

Both hooks duplicate rules the backend also implements — which ticket a detective's move
transfers to Mr. X, which pawn a turn-ack belongs to, how a double-move on a surfacing round
splits into two visible legs. That duplication is deliberate (the board has to stay live
mid-round — ADR-0010), but duplicated rules drift, and neither ESLint nor `tsc` can see a wrong
ticket. They are also where React's render-phase-reset pattern is used, which is easy to break
in a refactor and fails silently when it is.

`BoardScene` is deliberately out of scope: it renders to a canvas Phaser owns imperatively, and
testing it meaningfully needs a different tool than a jsdom unit test.

`src/test/fixtures.ts` holds the shared `makeGameState()` builder and `FakeEventSource`, which
dispatches by SSE event *name* exactly as the browser's own does — so the hook's real listener
registration is what is under test, not a reimplementation of it.

## Styling

Design tokens (colors, spacing, radius) are CSS custom properties in `src/index.css`.
Components use inline styles for genuinely dynamic values — per-agent colors, popup positioning
— and tokens (`var(--color-error)`) for everything shared.
