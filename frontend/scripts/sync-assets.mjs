/**
 * Copies the project's canonical board/ticket/pawn art from `data/` into `frontend/public/`,
 * so Vite can serve it.
 *
 * Vite only serves static assets from inside the frontend project, but `data/` is the single
 * source of truth for game data and art - the backend reads the board graph from the very same
 * directory. Those files used to be duplicated into `frontend/public/` by hand, which is a
 * silent drift risk: nothing detected when one copy was edited and the other was not.
 *
 * Running this from `predev`/`prebuild` makes the copy a build step rather than a manual
 * ritual, so the served art is always whatever `data/` currently holds. The copied paths are
 * gitignored for the same reason - they are generated output, not source.
 *
 * `favicon.svg` is deliberately NOT handled here: it is frontend chrome, not game data, so it
 * lives in `public/` as real committed source.
 */
import { cpSync, mkdirSync, rmSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const dataDir = resolve(here, "../../data");
const publicDir = resolve(here, "../public");

// [source in data/, destination under public/] - the destination is what src/ references by
// URL (e.g. labels.ts's "/tickets/taxi_ticket.jpg", BoardScene's "/board/board.svg").
const copies = [
  ["board/board.svg", "board/board.svg"],
  ["ui/pawn.svg", "pawn/pawn.svg"],
  ["ui/pawn_last_known.svg", "pawn/pawn_last_known.svg"],
  ["ui/game_logo.jpg", "game_logo.jpg"],
  ["tickets", "tickets"],
];

for (const [from, to] of copies) {
  const src = resolve(dataDir, from);
  const dest = resolve(publicDir, to);
  rmSync(dest, { recursive: true, force: true });
  mkdirSync(dirname(dest), { recursive: true });
  cpSync(src, dest, { recursive: true });
}

console.log(`Synced ${copies.length} asset path(s) from data/ into public/`);
