import type { Vec2 } from "./protocol";
import type { Rng } from "./random";

/** Base wind speed range, m/s. There is always some wind to fight. */
const MIN_BASE_SPEED = 1.5;
const MAX_BASE_SPEED = 7;
/** Gust amplitude as a fraction of the base speed; below 1 so the wind never reverses. */
const GUST_FRACTION = 0.6;

export interface WindField {
  base: Vec2;
  /** Random phase offsets so each spawn's gusts differ. */
  phases: [number, number, number];
}

export function generateWind(rng: Rng): WindField {
  const speed = rng.range(MIN_BASE_SPEED, MAX_BASE_SPEED);
  const direction = rng.next() < 0.5 ? -1 : 1;
  const vertical = rng.range(-0.25, 0.25);
  return {
    base: { x: direction * speed, y: speed * vertical },
    phases: [rng.range(0, Math.PI * 2), rng.range(0, Math.PI * 2), rng.range(0, Math.PI * 2)],
  };
}

/**
 * Wind velocity (m/s) at world position (x, y) and time t (s).
 *
 * Gusts are a few sines standing in for noise. Pairing a sin in y on the x
 * component with a cos in x on the y component gives the field curl, so it
 * swirls rather than just pulsing. Their amplitudes sum to 1, so the along-wind
 * component stays within GUST_FRACTION of the base and never reverses.
 */
export function windAt(field: WindField, x: number, y: number, t: number): Vec2 {
  const [p0, p1, p2] = field.phases;
  const speed = Math.hypot(field.base.x, field.base.y);
  const gustScale = GUST_FRACTION * speed;
  const gx =
    Math.sin(y * 0.12 + t * 0.6 + p0) * 0.5 +
    Math.sin(x * 0.07 - t * 0.45 + p1) * 0.3 +
    Math.sin((x - y) * 0.05 + t * 0.9 + p2) * 0.2;
  const gy =
    Math.cos(x * 0.12 - t * 0.6 + p0) * 0.5 +
    Math.sin(y * 0.09 + t * 0.7 + p2) * 0.3 +
    Math.cos((x + y) * 0.06 - t * 0.5 + p1) * 0.2;
  return {
    x: field.base.x + gx * gustScale,
    y: field.base.y + gy * gustScale * 0.8,
  };
}
