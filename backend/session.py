import random
import uuid
import asyncio
from dataclasses import dataclass, field
from typing import Dict, Literal, Optional

from agents import DETECTIVE_IDS
from state import ScotlandYardState

# Rules 1: shared pool detectives and Mr. X are randomly, uniquely assigned starting nodes from.
STARTING_NODE_POOL = [13, 26, 29, 34, 50, 53, 91, 94, 103, 112, 117, 132, 138, 141, 155, 174, 197, 198]

# Rules 1: starting ticket inventories.
MR_X_STARTING_TICKETS = {"taxi_tickets": 2, "bus_tickets": 5, "metro_tickets": 3, "black_tickets": 5, "double_tickets": 2}
DETECTIVE_STARTING_TICKETS = {"taxi_tickets": 11, "bus_tickets": 8, "metro_tickets": 4}


@dataclass
class GameSession:
    """
    Owns one in-progress game's ScotlandYardState plus the orchestration bookkeeping that
    doesn't belong inside LangGraph state (whose turn it is, whether the game has ended, and a
    lock serializing concurrent submissions against this one game). No persistence/TTL - games
    live only as long as this process does, matching "a page refresh discards the game".
    """
    game_id: str
    state: ScotlandYardState
    status: Literal["awaiting_mr_x_move", "detective_loop_running", "game_over"] = "awaiting_mr_x_move"
    winner: Optional[Literal["detectives", "mr_x"]] = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


# Plain in-memory store - no database, no eviction. Accepted per the "no persistence needed" design decision.
GAMES: Dict[str, GameSession] = {}


def create_game(seed_positions: Optional[Dict[str, int]] = None) -> GameSession:
    """
    Starts a brand-new game: randomly (or, for tests, deterministically via seed_positions)
    assigns each of Mr. X and the 5 detectives a unique starting node from the rules' pool, and
    gives everyone their rules-mandated starting ticket inventory.

    seed_positions, when given, must map "mr_x" and each of DETECTIVE_IDS to a node id and
    bypasses the random draw entirely - lets tests construct a fixed, reproducible board.
    """
    if seed_positions is not None:
        positions = dict(seed_positions)
    else:
        drawn = random.sample(STARTING_NODE_POOL, 6)
        positions = {"mr_x": drawn[0]}
        for det_id, node in zip(DETECTIVE_IDS, drawn[1:]):
            positions[det_id] = node

    state: ScotlandYardState = {
        "round_number": 1,
        "debate_loop_count": 0,
        "mr_x": {
            "current_node": positions["mr_x"],
            "last_known_node": None,
            "last_known_round": None,
            "transport_history": [],
            **MR_X_STARTING_TICKETS,
        },
        "detectives": {
            det_id: {"node_id": positions[det_id], **DETECTIVE_STARTING_TICKETS}
            for det_id in DETECTIVE_IDS
        },
        "messages": [],
        "proposed_strategies": {},
        "locked_moves": {},
        "final_moves": {},
        "final_move_details": {},
    }

    session = GameSession(game_id=str(uuid.uuid4()), state=state)
    GAMES[session.game_id] = session
    return session
