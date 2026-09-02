import type { Pad, WorldInfo } from "./protocol";
import type { Rng } from "./random";

export const WORLD_HEIGHT = 100;
export const GRAVITY = 4;
export const MIN_WORLD_WIDTH = 80;
export const MAX_WORLD_WIDTH = 400;

const PAD_WIDTH = 12;
const LAUNCH_PAD_WIDTH = 10;
const MIN_HEIGHT = 5;
const MAX_HEIGHT = 60;
/** Terrain heights are pulled back toward this so mountains stay peaks, not plateaus. */
const MEAN_HEIGHT = 22;
const MEAN_REVERSION = 0.15;
/** Roughly one terrain sample every this many metres. */
const SAMPLE_SPACING = 4;
const PAD_CANDIDATES = 8;
/** Minimum launch-to-landing pad distance as a fraction of the world width. */
const MIN_PAD_SEPARATION = 0.4;
const SPAWN_MARGIN = 15;

export interface World {
  seed: number;
  info: WorldInfo;
  /** Polyline with strictly increasing x, covering [0, width]. */
  terrain: Array<[number, number]>;
  /** Landing target. */
  pad: Pad;
  /** Where the rocket starts, resting on the ground. */
  launchPad: Pad;
}

/** Choose a world width (metres) that fills a viewport of the given aspect ratio. */
export function worldWidthFor(aspect: number): number {
  return clamp(WORLD_HEIGHT * aspect, MIN_WORLD_WIDTH, MAX_WORLD_WIDTH);
}

export function generateWorld(rng: Rng, seed: number, width: number): World {
  const minSeparation = MIN_PAD_SEPARATION * width;
  const padCentreMin = 4 + PAD_WIDTH / 2;
  const padCentreMax = width - 4 - PAD_WIDTH / 2;
  // Ranges of landing pad centre x that are far enough from the launch pad.
  const allowedRanges = (spawn: number): Array<[number, number]> =>
    (
      [
        [padCentreMin, spawn - minSeparation],
        [spawn + minSeparation, padCentreMax],
      ] as Array<[number, number]>
    ).filter(([a, b]) => b > a);
  let spawnX = rng.range(SPAWN_MARGIN, width - SPAWN_MARGIN);
  let padRanges = allowedRanges(spawnX);
  if (padRanges.length === 0) {
    // Narrow world with a central spawn: push the launch pad to an edge.
    spawnX = rng.next() < 0.5 ? SPAWN_MARGIN : width - SPAWN_MARGIN;
    padRanges = allowedRanges(spawnX);
  }

  const sampleCount = Math.max(12, Math.round(width / SAMPLE_SPACING));
  const xs: number[] = [];
  for (let i = 0; i < sampleCount; i++) {
    xs.push((width * i) / (sampleCount - 1));
  }

  // Mean-reverting random walk with bounded step, then one smoothing pass.
  let h = rng.range(8, 30);
  const raw: number[] = [];
  for (let i = 0; i < sampleCount; i++) {
    const pull = (MEAN_HEIGHT - h) * MEAN_REVERSION;
    h = clamp(h + pull + rng.range(-13, 13), MIN_HEIGHT, MAX_HEIGHT);
    raw.push(h);
  }
  const heights = raw.map((v, i) => {
    const prev = raw[i - 1] ?? v;
    const next = raw[i + 1] ?? v;
    return (prev + v + next) / 3;
  });

  const launchX1 = spawnX - LAUNCH_PAD_WIDTH / 2;
  const launchPad: Pad = {
    x1: launchX1,
    x2: launchX1 + LAUNCH_PAD_WIDTH,
    y: heightAtSamples(xs, heights, spawnX),
  };

  // Pick the lowest of several candidate pad positions far enough from the
  // launch pad, so the target tends to sit in a valley behind the mountains.
  const totalRange = padRanges.reduce((acc, [a, b]) => acc + (b - a), 0);
  let padCentre = spawnX < width / 2 ? padCentreMax : padCentreMin;
  let padHeight = heightAtSamples(xs, heights, padCentre);
  for (let i = 0; i < PAD_CANDIDATES && totalRange > 0; i++) {
    // Uniform over the union of allowed ranges.
    let offset = rng.next() * totalRange;
    let centre = padCentre;
    for (const [a, b] of padRanges) {
      if (offset <= b - a) {
        centre = a + offset;
        break;
      }
      offset -= b - a;
    }
    const h = heightAtSamples(xs, heights, centre);
    if (h < padHeight) {
      padHeight = h;
      padCentre = centre;
    }
  }
  const pad: Pad = { x1: padCentre - PAD_WIDTH / 2, x2: padCentre + PAD_WIDTH / 2, y: padHeight };

  return {
    seed,
    info: { width, height: WORLD_HEIGHT, gravity: GRAVITY },
    terrain: withFlats(xs, heights, [launchPad, pad]),
    pad,
    launchPad,
  };
}

/** Build the terrain polyline, replacing the samples under each flat with its endpoints. */
function withFlats(xs: number[], heights: number[], flats: Pad[]): Array<[number, number]> {
  const sorted = [...flats].sort((a, b) => a.x1 - b.x1);
  const terrain: Array<[number, number]> = [];
  let fi = 0;
  const pushFlat = (f: Pad): void => {
    terrain.push([f.x1, f.y], [f.x2, f.y]);
  };
  for (let i = 0; i < xs.length; i++) {
    const x = xs[i] as number;
    const y = heights[i] as number;
    let flat = sorted[fi];
    while (flat !== undefined && flat.x2 < x) {
      pushFlat(flat);
      fi++;
      flat = sorted[fi];
    }
    if (flat !== undefined && x >= flat.x1 && x <= flat.x2) continue;
    terrain.push([x, y]);
  }
  for (; fi < sorted.length; fi++) pushFlat(sorted[fi] as Pad);
  return terrain;
}

/** Linear interpolation of the terrain polyline at x. */
export function terrainHeightAt(terrain: Array<[number, number]>, x: number): number {
  const first = terrain[0];
  const last = terrain[terrain.length - 1];
  if (first === undefined || last === undefined) return 0;
  if (x <= first[0]) return first[1];
  if (x >= last[0]) return last[1];
  // Binary search for the segment containing x.
  let lo = 0;
  let hi = terrain.length - 1;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if ((terrain[mid] as [number, number])[0] <= x) lo = mid;
    else hi = mid;
  }
  const [x0, y0] = terrain[lo] as [number, number];
  const [x1, y1] = terrain[hi] as [number, number];
  const t = x1 === x0 ? 0 : (x - x0) / (x1 - x0);
  return y0 + (y1 - y0) * t;
}

function heightAtSamples(xs: number[], heights: number[], x: number): number {
  const pts: Array<[number, number]> = xs.map((xv, i) => [xv, heights[i] as number]);
  return terrainHeightAt(pts, x);
}

function clamp(v: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, v));
}
