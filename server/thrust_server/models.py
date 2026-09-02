"""Pydantic models mirroring ``src/protocol.ts`` in the web client."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Status = Literal["flying", "landed", "crashed"]


class Vec2(BaseModel):
    x: float
    y: float


class Rocket(BaseModel):
    x: float
    y: float
    vx: float
    vy: float
    angle: float
    """Radians, 0 = pointing up, positive = clockwise."""
    angularVelocity: float  # noqa: N815 - matches the JSON wire format


class Pad(BaseModel):
    x1: float
    x2: float
    y: float


class WorldInfo(BaseModel):
    width: float
    height: float
    gravity: float


class State(BaseModel):
    """Client -> server. Units are world metres, y up."""

    model_config = ConfigDict(populate_by_name=True)

    type: Literal["state"] = "state"
    tick: int
    status: Status
    rocket: Rocket
    wind: Vec2
    """Wind at the rocket's position."""
    pad: Pad
    """Landing target."""
    launch_pad: Pad = Field(alias="launchPad")
    """Where the rocket started, resting on the ground."""
    terrain: list[tuple[float, float]]
    """Terrain polyline, x increasing."""
    world: WorldInfo


class Move(BaseModel):
    """Server -> client."""

    type: Literal["move"] = "move"
    thrust: bool = False
    left: bool = False
    right: bool = False
