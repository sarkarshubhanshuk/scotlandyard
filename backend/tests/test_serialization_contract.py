"""
The backend's outward-facing payloads against the frontend's hand-written mirror of them.

`frontend/src/types.ts` mirrors `serializers.py` by hand, because the backend is Starlette and
has no OpenAPI schema to generate from (ADR-0002). `frontend/README.md` states plainly that
"nothing will catch it otherwise" when the two drift - this is the something.

It reads the real `types.ts` rather than restating its fields here. A checked-in copy of the
expected shape would just be a third hand-maintained list, drifting on its own schedule and
capable of agreeing with neither side.

Deliberately a field-NAME contract, not a type contract. Names are where the drift actually bites
(a renamed or dropped key silently reads as `undefined` in the client), and matching TypeScript's
type syntax from here would be a parser rather than a test.
"""
import re
from pathlib import Path

import pytest

from scotland_yard.serializers import serialize_public_state
from scotland_yard.session import create_game

from .conftest import SEED_POSITIONS

TYPES_TS = Path(__file__).resolve().parents[2] / "frontend" / "src" / "types.ts"

# Lines that declare a field: `name: type;` or `name?: type;`, after comments are stripped.
_FIELD = re.compile(r"^\s*(\w+)\??\s*:", re.M)


def interface_fields(source: str, name: str) -> set:
    """
    The field names declared by `export interface <name>` in `source`.

    Brace-matched from the interface's opening `{` so a nested object literal in a field's type
    cannot end the block early, and comments are stripped first so a `//` line mentioning a field
    name is never mistaken for a declaration.
    """
    start = source.index(f"export interface {name} {{") + len(f"export interface {name} ")
    depth = 0
    for index in range(start, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                body = source[start + 1:index]
                break
    else:
        raise AssertionError(f"unbalanced braces in interface {name}")

    body = re.sub(r"//[^\n]*", "", body)
    body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
    return set(_FIELD.findall(body))


@pytest.fixture(scope="module")
def types_source() -> str:
    assert TYPES_TS.is_file(), (
        f"{TYPES_TS} not found. This test reads the frontend's hand-written mirror of the "
        "backend payloads; if the frontend has moved, update TYPES_TS."
    )
    return TYPES_TS.read_text(encoding="utf-8")


def _mismatch(interface: str, backend: set, frontend: set) -> str:
    missing = backend - frontend
    extra = frontend - backend
    parts = []
    if missing:
        parts.append(f"the backend sends {sorted(missing)}, which {interface} does not declare")
    if extra:
        parts.append(f"{interface} declares {sorted(extra)}, which the backend does not send")
    return (
        f"serialize_public_state and frontend/src/types.ts:{interface} disagree - "
        + "; and ".join(parts)
        + ". Update frontend/src/types.ts to match (it is maintained by hand - see ADR-0002)."
    )


class TestPublicStateContract:
    """
    The payload every route and the terminal stream event carry. A mismatch here is the exact
    failure `frontend/README.md` warns about and nothing else can see.
    """

    @pytest.fixture
    def payload(self):
        return serialize_public_state(create_game(seed_positions=SEED_POSITIONS))

    def test_top_level_fields_match(self, payload, types_source):
        frontend = interface_fields(types_source, "PublicGameState")
        backend = set(payload)
        assert backend == frontend, _mismatch("PublicGameState", backend, frontend)

    def test_mr_x_fields_match(self, payload, types_source):
        frontend = interface_fields(types_source, "PublicMrX")
        backend = set(payload["mr_x"])
        assert backend == frontend, _mismatch("PublicMrX", backend, frontend)

    def test_detective_fields_match(self, payload, types_source):
        frontend = interface_fields(types_source, "PublicDetective")
        backend = set(payload["detectives"]["agent_red"])
        assert backend == frontend, _mismatch("PublicDetective", backend, frontend)

    def test_mr_x_real_position_is_still_deliberately_included(self, payload, types_source):
        """
        Not drift-detection - a standing check on ADR-0007.

        current_node is Mr. X's true location, published because the only human client is played
        BY Mr. X. If a detective-facing client is ever added, this field must be excluded from
        whatever that client receives, and this test should start failing loudly enough to make
        someone re-read serializers.py's own warning.
        """
        assert "current_node" in payload["mr_x"]
        assert "current_node" in interface_fields(types_source, "PublicMrX")


class TestParserItself:
    """
    The contract above is only as good as its reading of types.ts - a parser that silently
    returned an empty set would make every assertion above pass vacuously.
    """

    def test_it_finds_real_fields(self, types_source):
        assert interface_fields(types_source, "PublicDetective") == {
            "node_id", "taxi_tickets", "bus_tickets", "metro_tickets",
        }

    def test_it_ignores_field_names_mentioned_in_comments(self):
        source = """
export interface Thing {
  // last_known_node is explained here but not declared.
  real_field: number;
  /* another_commented_field: string; */
}
"""
        assert interface_fields(source, "Thing") == {"real_field"}

    def test_it_does_not_stop_at_a_nested_object_literal(self):
        source = """
export interface Thing {
  nested: { inner: number };
  after_the_nesting: string;
}
"""
        assert interface_fields(source, "Thing") == {"nested", "after_the_nesting"}
