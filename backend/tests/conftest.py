"""
Shared pytest fixtures for the backend suite.

Two things every test here depends on:

1. `sys.path` - the application package lives one directory up (`backend/scotland_yard`),
   and the suite is meant to run via a bare `pytest` from `backend/` whether or not the
   package has been `pip install -e`'d. Adding the parent directory explicitly makes both
   work identically.
2. Deterministic starting positions - `create_game()` normally draws 6 unique nodes at
   random from the rules' pool, which would make every board-dependent assertion flaky.
   `seed_positions` exists on `create_game` specifically so tests can pin the board; the
   `SEED_POSITIONS` map below is the one fixed board the whole suite reasons about.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scotland_yard.session import create_game  # noqa: E402

# One fixed, reproducible board, drawn from rules.md's starting-node pool. Every position
# here is a real pool entry, so the board is one the game could genuinely have dealt.
SEED_POSITIONS = {
    "mr_x": 13,
    "agent_red": 26,
    "agent_blue": 29,
    "agent_green": 34,
    "agent_orange": 50,
    "agent_purple": 53,
}


@pytest.fixture
def seeded_session():
    """A fresh game on the fixed SEED_POSITIONS board, awaiting Mr. X's round-1 move."""
    return create_game(seed_positions=SEED_POSITIONS)


@pytest.fixture
def seed_positions():
    """The fixed board `seeded_session` was built from, for tests that need the raw node ids."""
    return dict(SEED_POSITIONS)
