"""
Pydantic models for the HTTP API's request bodies and query parameters.

These exist to separate two kinds of bad request that used to be conflated:

  * A *semantically* illegal move ("node 42 is occupied", "no metro tickets left").
    mrx_turn raises IllegalMoveError for these, and its message is client-facing.
  * A *structurally* malformed request (not JSON, missing "target_node", ticket_type_spent
    misspelled, from_node="abc"). These used to raise KeyError / ValueError / JSONDecodeError
    straight out of the route, which no handler caught, so the client got an opaque 500 with
    a stack trace for what is squarely a 400.

Validating the shape here means every route below can assume well-formed input and reason
only about game legality.
"""
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


class Hop(BaseModel):
    """One leg of a Mr. X move: where to, and which ticket pays for it."""

    model_config = ConfigDict(extra="forbid")

    target_node: int = Field(ge=1, le=200)
    # Deliberately not including "double": that is a travel-log sentinel, never a ticket that
    # can pay for a hop (see rules_constants.VALID_TICKET_TYPES).
    ticket_type_spent: Literal["taxi", "bus", "metro", "black"]


class SingleMoveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    move_type: Literal["single"]
    target_node: int = Field(ge=1, le=200)
    ticket_type_spent: Literal["taxi", "bus", "metro", "black"]

    def to_move_request(self) -> dict:
        return self.model_dump()


class DoubleMoveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    move_type: Literal["double"]
    hop1: Hop
    hop2: Hop

    def to_move_request(self) -> dict:
        return self.model_dump()


# Discriminated on move_type, so an unknown value produces one clear error naming that field
# rather than two confusing "did not match either branch" reports.
MrXMoveRequest = Annotated[
    Union[SingleMoveRequest, DoubleMoveRequest],
    Field(discriminator="move_type"),
]
MR_X_MOVE_ADAPTER = TypeAdapter(MrXMoveRequest)


class Hop2PreviewQuery(BaseModel):
    """
    The optional hop-2 preview parameters on GET /mrx/legal-moves. Both must be supplied
    together; supplying neither means "preview Mr. X's ordinary single-hop options".
    """

    model_config = ConfigDict(extra="forbid")

    from_node: int = Field(ge=1, le=200)
    ticket_type_spent: Literal["taxi", "bus", "metro", "black"]
