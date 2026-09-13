# Board data authoring tools

One-off utilities used to produce the contents of `data/board/`. **Neither is part of the
running application** — nothing in `backend/` or `frontend/` imports them, and you only need
them if you are re-deriving the board data itself.

`data/board/map.json` and `data/board/node_positions.json` are treated as immutable truth data
by `.cursorrules`. These tools are how that data came to exist in the first place; they are not
a licence to regenerate it casually.

## `extract_node_positions.py` — the primary generator

Derives `data/board/node_positions.json` from `data/board/board.svg` automatically. No manual
coordinate-picking is needed: `board.svg` already draws a small numbered `<text>` label on top
of each node's `<circle>` marker, and both groups share the same `transform`, so each label's
nearest circle centre *is* that node's true position.

Output values are **percentages** of the board's width and height, not pixels, so the board can
be rendered at any resolution.

```bash
cd backend && source venv/Scripts/activate
python ../tools/extract_node_positions.py
```

## `node_coordinate_picker.html` — the manual corrector

A standalone browser page (open it directly, no server needed) for reviewing the generated
positions against the rendered board and nudging any that are wrong. It exports a corrected
`node_positions.json` to replace the generated one.

Verification history for the resulting data is in
[ISSUE-012](../docs/issues/known_issues.md) — the positions have been visually spot-checked
against the rendered board.
