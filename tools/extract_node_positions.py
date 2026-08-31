"""
One-off extraction script: derives docs/map/node_positions.json from docs/map/board.svg.

board.svg already draws a small numbered <text> label directly on top of each node's <circle>
marker, and both the circle-marker group and the text-label group share the exact same
transform="translate(0,-550)" - the same transform the board's route-line <path> elements use.
So each label's nearest circle center, with that shared transform applied, IS the node's true
position in board.svg's own coordinate space - no manual coordinate-picking needed.

Run from the repo root: python tools/extract_node_positions.py
"""
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
BOARD_SVG = BASE_DIR / "docs" / "map" / "board.svg"
MAP_JSON = BASE_DIR / "docs" / "map" / "map.json"
OUTPUT = BASE_DIR / "docs" / "map" / "node_positions.json"

SVG_NS = "{http://www.w3.org/2000/svg}"


def local_translate(g_elem) -> tuple:
    transform = g_elem.get("transform", "")
    match = re.match(r"translate\(\s*([-\d.eE]+)[,\s]+([-\d.eE]+)\s*\)", transform)
    if not match:
        return (0.0, 0.0)
    return (float(match.group(1)), float(match.group(2)))


def find_group_with_transform(root, needle: str):
    """Returns the first <g> anywhere in the tree whose transform attribute equals `needle`."""
    for g in root.iter(f"{SVG_NS}g"):
        if g.get("transform") == needle:
            return g
    return None


def main():
    tree = ET.parse(BOARD_SVG)
    root = tree.getroot()

    marker_group = find_group_with_transform(root, "translate(0,-550)")
    # The circle-marker group and the text-label group are two SEPARATE top-level <g>
    # elements that both happen to use this same transform string - collect every <g> that
    # matches it, then split by which one contains circles vs. text.
    candidate_groups = [g for g in root.iter(f"{SVG_NS}g") if g.get("transform") == "translate(0,-550)"]

    circles = []
    labels = []
    for g in candidate_groups:
        found_circles = list(g.iter(f"{SVG_NS}circle"))
        found_texts = list(g.iter(f"{SVG_NS}text"))
        if found_circles and not found_texts:
            circles.extend(found_circles)
        if found_texts and not found_circles:
            labels.extend(found_texts)

    if not circles or not labels:
        raise SystemExit(f"Expected both a circle group and a text group; got {len(circles)} circles, {len(labels)} texts.")

    circle_points = [(float(c.get("cx")), float(c.get("cy"))) for c in circles]

    positions = {}
    for text_elem in labels:
        tspan = text_elem.find(f"{SVG_NS}tspan")
        node_id = int(tspan.text.strip())
        label_x = float(text_elem.get("x"))
        label_y = float(text_elem.get("y"))

        # Nearest circle center to this label - the label's own coordinates carry a small,
        # inconsistent baseline offset from the true marker center, so don't use them directly.
        nearest = min(circle_points, key=lambda p: (p[0] - label_x) ** 2 + (p[1] - label_y) ** 2)
        dist = ((nearest[0] - label_x) ** 2 + (nearest[1] - label_y) ** 2) ** 0.5
        if dist > 10:
            raise SystemExit(f"Node {node_id}: nearest circle is {dist:.2f} units away ({nearest}) - too far to trust.")

        if node_id in positions:
            raise SystemExit(f"Duplicate label for node {node_id}.")
        positions[node_id] = nearest

    # Apply the shared group transform to get coordinates in board.svg's own top-level
    # (0,0)-(600,450) viewBox space.
    tx, ty = local_translate(candidate_groups[0])
    final_positions = {
        str(node_id): {"x": round(x + tx, 2), "y": round(y + ty, 2)}
        for node_id, (x, y) in positions.items()
    }

    # Cross-check against map.json's authoritative node id set (read-only per .cursorrules).
    map_data = json.loads(MAP_JSON.read_text(encoding="utf-8"))
    expected_ids = {node["id"] for node in map_data["nodes"]}
    extracted_ids = {int(k) for k in final_positions}

    missing = expected_ids - extracted_ids
    extra = extracted_ids - expected_ids
    if missing:
        raise SystemExit(f"Missing positions for node ids: {sorted(missing)}")
    if extra:
        raise SystemExit(f"Extracted positions for unknown node ids: {sorted(extra)}")

    OUTPUT.write_text(json.dumps(final_positions, indent=2, sort_keys=False), encoding="utf-8")
    print(f"Wrote {len(final_positions)} node positions to {OUTPUT}")
    print(f"x range: {min(p['x'] for p in final_positions.values())}-{max(p['x'] for p in final_positions.values())}")
    print(f"y range: {min(p['y'] for p in final_positions.values())}-{max(p['y'] for p in final_positions.values())}")


if __name__ == "__main__":
    main()
