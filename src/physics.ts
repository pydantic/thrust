import type { Pad, RocketState, Status, Vec2 } from "./protocol";
import { terrainHeightAt } from "./world";

export const DT = 1 / 60;

const THRUST_ACCEL = 9;
const ROTATION_ACCEL = 6;
const ANGULAR_DAMPING = 2.5;
const MAX_ANGULAR_VELOCITY = 3;
/** Linear drag coefficient toward the local wind velocity (1/s). */
const WIND_DRAG = 0.15;

const LANDING_MAX_ANGLE = 0.28;
const LANDING_MAX_VY = 5;
const LANDING_MAX_VX = 3.2;

/** Rocket outline in body space: tip up, base at the bottom. Metres. */
export const ROCKET_HEIGHT = 4;
export const ROCKET_HALF_BASE = 1.25;
export const ROCKET_VERTICES: readonly Vec2[] = [
  { x: 0, y: ROCKET_HEIGHT * 0.6 },
  { x: ROCKET_HALF_BASE, y: -ROCKET_HEIGHT * 0.4 },
  { x: -ROCKET_HALF_BASE, y: -ROCKET_HEIGHT * 0.4 },
];

export interface Inputs {
  thrust: boolean;
  left: boolean;
  right: boolean;
}

export const NO_INPUTS: Inputs = { thrust: false, left: false, right: false };

export interface Rocket extends RocketState {
  status: Status;
}

export function createRocket(x: number, y: number, angle: number): Rocket {
  return { x, y, vx: 0, vy: 0, angle, angularVelocity: 0, status: "flying" };
}

/** Rotate a body-space point into world space for the given rocket. */
export function toWorld(rocket: RocketState, p: Vec2): Vec2 {
  // angle is clockwise-positive with y up, so rotate by -angle in the usual sense.
  const c = Math.cos(rocket.angle);
  const s = Math.sin(rocket.angle);
  return { x: rocket.x + p.x * c + p.y * s, y: rocket.y - p.x * s + p.y * c };
}

/** Unit vector the rocket's nose points along. */
export function upVector(rocket: RocketState): Vec2 {
  return { x: Math.sin(rocket.angle), y: Math.cos(rocket.angle) };
}

/** Rocket centre height when resting upright on a pad at padY. */
export function restingY(padY: number): number {
  return padY + ROCKET_HEIGHT * 0.4;
}

export function step(
  rocket: Rocket,
  inputs: Inputs,
  wind: Vec2,
  gravity: number,
  terrain: Array<[number, number]>,
  pad: Pad,
  launchPad: Pad,
  worldWidth: number,
): void {
  if (rocket.status !== "flying") return;

  // Rotation.
  let torque = 0;
  if (inputs.left) torque -= ROTATION_ACCEL;
  if (inputs.right) torque += ROTATION_ACCEL;
  rocket.angularVelocity += (torque - ANGULAR_DAMPING * rocket.angularVelocity) * DT;
  rocket.angularVelocity = Math.max(
    -MAX_ANGULAR_VELOCITY,
    Math.min(MAX_ANGULAR_VELOCITY, rocket.angularVelocity),
  );
  rocket.angle += rocket.angularVelocity * DT;

  // Linear acceleration: gravity, thrust, wind drag.
  let ax = WIND_DRAG * (wind.x - rocket.vx);
  let ay = -gravity + WIND_DRAG * (wind.y - rocket.vy);
  if (inputs.thrust) {
    const up = upVector(rocket);
    ax += up.x * THRUST_ACCEL;
    ay += up.y * THRUST_ACCEL;
  }
  const prevX = rocket.x;
  rocket.vx += ax * DT;
  rocket.vy += ay * DT;
  rocket.x += rocket.vx * DT;
  rocket.y += rocket.vy * DT;

  // Flying off the sides counts as a crash.
  if (rocket.x < 0 || rocket.x > worldWidth) {
    rocket.status = "crashed";
    return;
  }

  // Ground contact.
  const corners = ROCKET_VERTICES.map((v) => toWorld(rocket, v));
  const touching = corners.some((c) => c.y <= terrainHeightAt(terrain, c.x));
  if (!touching) return;

  const base = [corners[1], corners[2]] as [Vec2, Vec2];
  const onFlat = (flat: Pad): boolean => base.every((c) => c.x >= flat.x1 && c.x <= flat.x2);
  const gentle =
    Math.abs(normaliseAngle(rocket.angle)) < LANDING_MAX_ANGLE &&
    Math.abs(rocket.vy) < LANDING_MAX_VY &&
    Math.abs(rocket.vx) < LANDING_MAX_VX;

  if (onFlat(pad) && gentle) {
    rocket.status = "landed";
    rocket.y = restingY(pad.y);
    rocket.vx = 0;
    rocket.vy = 0;
    rocket.angularVelocity = 0;
    rocket.angle = 0;
    return;
  }
  if (onFlat(launchPad) && gentle) {
    // Sitting on (or gently back onto) the launch pad: the ground holds it
    // still against wind, but it is still "flying" as far as the game goes.
    rocket.x = prevX;
    rocket.y = Math.max(rocket.y, restingY(launchPad.y));
    rocket.vx = 0;
    rocket.vy = Math.max(0, rocket.vy);
    return;
  }
  rocket.status = "crashed";
}

function normaliseAngle(a: number): number {
  let r = a % (Math.PI * 2);
  if (r > Math.PI) r -= Math.PI * 2;
  if (r < -Math.PI) r += Math.PI * 2;
  return r;
}
