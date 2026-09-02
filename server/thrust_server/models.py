"""Pydantic models mirroring `src/protocol.ts` in the web client."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Status = Literal['flying', 'landed', 'crashed']


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


class Physics(BaseModel):
    """Simulation constants, sent with every state so nothing has to be kept in sync."""

    model_config = ConfigDict(populate_by_name=True)

    dt: float
    """Fixed timestep in seconds."""
    thrust_accel: float = Field(alias='thrustAccel')
    """Engine acceleration along the nose, m/s^2."""
    rotation_accel: float = Field(alias='rotationAccel')
    """Angular acceleration from a rotation key, rad/s^2."""
    angular_damping: float = Field(alias='angularDamping')
    max_angular_velocity: float = Field(alias='maxAngularVelocity')
    wind_drag: float = Field(alias='windDrag')
    """Linear drag toward the local wind velocity, 1/s."""
    rocket_height: float = Field(alias='rocketHeight')
    rocket_half_base: float = Field(alias='rocketHalfBase')
    landing_max_angle: float = Field(alias='landingMaxAngle')
    landing_max_vy: float = Field(alias='landingMaxVy')
    landing_max_vx: float = Field(alias='landingMaxVx')


class State(BaseModel):
    """Client -> server. Units are world metres, y up."""

    model_config = ConfigDict(populate_by_name=True)

    type: Literal['state'] = 'state'
    tick: int
    status: Status
    rocket: Rocket
    wind: Vec2
    """Wind at the rocket's position."""
    pad: Pad
    """Landing target."""
    launch_pad: Pad = Field(alias='launchPad')
    """Where the rocket started, resting on the ground."""
    terrain: list[tuple[float, float]]
    """Terrain polyline, x increasing."""
    world: WorldInfo
    physics: Physics


class Move(BaseModel):
    """Server -> client."""

    type: Literal['move'] = 'move'
    thrust: bool = False
    left: bool = False
    right: bool = False
