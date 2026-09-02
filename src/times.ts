import type { Status } from "./protocol";

/** How a finished flight of a seed went. */
export interface FlightTime {
  time: number;
  status: Exclude<Status, "flying">;
  /** Rocket positions every `TRAIL_INTERVAL_S`, so dot spacing shows speed. */
  trail: Array<[number, number]>;
}

export const TRAIL_INTERVAL_S = 0.1;

/** Flights kept per seed: all are drawn as trails, the first few listed under the clock. */
const MAX_TIMES = 20;
const KEY_PREFIX = "thrust.times.";

/** Most recent first. Kept in localStorage per seed so reloads keep the list. */
export function loadTimes(seed: number): FlightTime[] {
  try {
    const raw = localStorage.getItem(KEY_PREFIX + seed);
    if (raw === null) return [];
    const parsed: unknown = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed.filter(isFlightTime) : [];
  } catch {
    return [];
  }
}

export function recordTime(seed: number, flight: FlightTime): FlightTime[] {
  const times = [flight, ...loadTimes(seed)].slice(0, MAX_TIMES);
  try {
    localStorage.setItem(KEY_PREFIX + seed, JSON.stringify(times));
  } catch {
    // Storage unavailable; the list just lives for this page.
  }
  return times;
}

function isFlightTime(value: unknown): value is FlightTime {
  if (typeof value !== "object" || value === null) return false;
  const v = value as Record<string, unknown>;
  return (
    typeof v.time === "number" &&
    (v.status === "landed" ||
      v.status === "crashed" ||
      v.status === "timeout" ||
      v.status === "aborted") &&
    Array.isArray(v.trail)
  );
}
