import type { Vec2 } from "./protocol";
import type { Rng } from "./random";

const MAX_BASE_SPEED = 4;

export interface WindField {
  base: Vec2;
  /** Random phase offsets so each spawn's gusts differ. */
  phases: [number, number, number];
}

export function generateWind(rng: Rng): WindField {
  const speed = rng.range(0, MAX_BASE_SPEED);
  const direction = rng.next() < 0.5 ? -1 : 1;
  const vertical = rng.range(-0.25, 0.25);
  return {
    base: { x: direction * speed, y: speed * vertical },
    phases: [rng.range(0, Math.PI * 2), rng.range(0, Math.PI * 2), rng.range(0, Math.PI * 2)],
  };
}

/** Wind velocity (m/s) at world position (x, y) and time t (s). */
export function windAt(field: WindField, x: number, y: number, t: number): Vec2 {
  const [p0, p1, p2] = field.phases;
  const speed = Math.hypot(field.base.x, field.base.y);
  // Gusts stay below the base speed so the wind never reverses.
  const gustScale = 0.3 * speed;
  // A few low-frequency sines standing in for noise.
  const gx =
    Math.sin(x * 0.05 + t * 0.4 + p0) * 0.6 +
    Math.sin(y * 0.08 - t * 0.3 + p1) * 0.3 +
    Math.sin((x + y) * 0.03 + t * 0.7 + p2) * 0.1;
  const gy = Math.cos(x * 0.07 - t * 0.5 + p1) * 0.4 + Math.sin(y * 0.06 + t * 0.6 + p2) * 0.2;
  return {
    x: field.base.x + gx * gustScale,
    y: field.base.y + gy * gustScale * 0.5,
  };
}
