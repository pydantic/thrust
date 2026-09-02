/** Messages exchanged with the FastAPI server over the WebSocket. */

export type Status = "flying" | "landed" | "crashed" | "timeout";

export interface Vec2 {
  x: number;
  y: number;
}

export interface RocketState {
  x: number;
  y: number;
  vx: number;
  vy: number;
  /** Radians, 0 = pointing up, positive = clockwise. */
  angle: number;
  angularVelocity: number;
}

export interface Pad {
  x1: number;
  x2: number;
  y: number;
}

export interface WorldInfo {
  width: number;
  height: number;
  gravity: number;
}

/** Simulation constants, so a controller can model the rocket without a copy. */
export interface PhysicsConstants {
  /** Fixed timestep in seconds. */
  dt: number;
  /** Engine acceleration along the nose, m/s². */
  thrustAccel: number;
  /** Angular acceleration from a rotation key, rad/s². */
  rotationAccel: number;
  /** Angular velocity decays by this factor per second. */
  angularDamping: number;
  maxAngularVelocity: number;
  /** Linear drag toward the local wind velocity, 1/s. */
  windDrag: number;
  rocketHeight: number;
  rocketHalfBase: number;
  landingMaxAngle: number;
  landingMaxVy: number;
  landingMaxVx: number;
  /** A flight still going after this many seconds ends with status "timeout". */
  maxFlightTime: number;
}

/** Client -> server. Units are world metres, y up. */
export interface StateMessage {
  type: "state";
  tick: number;
  status: Status;
  rocket: RocketState;
  /** Wind at the rocket's position. */
  wind: Vec2;
  /** Landing target. */
  pad: Pad;
  /** Where the rocket started, resting on the ground. */
  launchPad: Pad;
  /** Terrain polyline, x increasing. */
  terrain: Array<[number, number]>;
  world: WorldInfo;
  physics: PhysicsConstants;
}

/** Server -> client. */
export interface MoveMessage {
  type: "move";
  thrust: boolean;
  left: boolean;
  right: boolean;
}

/** Response of `GET /plan`: the auto-pilot has written a script for the next flight. */
export interface PlanResponse {
  /** The script's own description of how it intends to fly. */
  strategy: string;
}

export function isPlanResponse(value: unknown): value is PlanResponse {
  if (typeof value !== "object" || value === null) return false;
  return typeof (value as Record<string, unknown>).strategy === "string";
}

export function isMoveMessage(value: unknown): value is MoveMessage {
  if (typeof value !== "object" || value === null) return false;
  const v = value as Record<string, unknown>;
  return (
    v.type === "move" &&
    typeof v.thrust === "boolean" &&
    typeof v.left === "boolean" &&
    typeof v.right === "boolean"
  );
}
