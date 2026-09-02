import type { Inputs, Rocket } from "./physics";
import { ROCKET_HALF_BASE, ROCKET_HEIGHT, ROCKET_VERTICES, toWorld } from "./physics";
import type { Vec2 } from "./protocol";
import { type WindField, windAt } from "./wind";
import { terrainHeightAt, type World } from "./world";

const WIND_PARTICLES = 350;
/** Streak length in metres per m/s of wind. */
const WIND_LINE_SCALE = 0.18;
const WIND_LINE_MIN = 0.3;
const WIND_LINE_MAX = 2;
/** Visual speed-up so the drift is easy to see. */
const WIND_ADVECT = 1.5;
/**
 * Time constant (s) for smoothing the flame. The auto-pilot's thrust is bang-bang at up
 * to 30 Hz, so the raw command flickers; the flame shows the average throttle.
 */
const FLAME_SMOOTHING_S = 0.3;

export interface Frame {
  world: World;
  wind: WindField;
  rocket: Rocket;
  inputs: Inputs;
  time: number;
  /** The start dialog is open and physics is not running. */
  paused: boolean;
  windAtRocket: Vec2;
  autopilot: boolean;
  /** Waiting for `GET /plan`: the auto-pilot is writing its script. */
  planning: boolean;
  connected: boolean;
  serverActive: boolean;
  /** The auto-pilot script's own description of its plan, shown under the HUD. */
  strategy: string | null;
}

/** The HUD text is refreshed at most this often so the numbers are readable. */
const HUD_INTERVAL_MS = 100;
const HUD_FONT = "13px ui-monospace, SFMono-Regular, Menlo, monospace";
const HUD_LINE_PX = 17;
const STRATEGY_MAX_WIDTH_PX = 460;

export class Renderer {
  private readonly ctx: CanvasRenderingContext2D;
  private scale = 1;
  private offsetX = 0;
  private offsetY = 0;
  private particles: Vec2[] = [];
  private particleSeed = -1;
  private lastDrawAt = 0;
  /** Smoothed throttle in [0, 1] driving the flame length. */
  private flame = 0;

  constructor(private readonly canvas: HTMLCanvasElement) {
    const ctx = canvas.getContext("2d");
    if (ctx === null) throw new Error("2d canvas context unavailable");
    this.ctx = ctx;
  }

  resize(worldWidth: number, worldHeight: number): void {
    const dpr = window.devicePixelRatio || 1;
    const cssW = window.innerWidth;
    const cssH = window.innerHeight;
    this.canvas.width = Math.floor(cssW * dpr);
    this.canvas.height = Math.floor(cssH * dpr);
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    this.scale = Math.min(cssW / worldWidth, cssH / worldHeight);
    this.offsetX = (cssW - worldWidth * this.scale) / 2;
    this.offsetY = (cssH - worldHeight * this.scale) / 2;
  }

  /** World metres (y up) -> CSS pixels (y down). */
  private toScreen(x: number, y: number, worldHeight: number): Vec2 {
    return {
      x: this.offsetX + x * this.scale,
      y: this.offsetY + (worldHeight - y) * this.scale,
    };
  }

  draw(frame: Frame): void {
    const { ctx } = this;
    const { world } = frame;
    const H = world.info.height;
    const W = world.info.width;

    ctx.fillStyle = "#fff";
    ctx.fillRect(0, 0, this.canvas.width, this.canvas.height);

    if (this.particleSeed !== world.seed) {
      this.particleSeed = world.seed;
      this.flame = 0;
      this.particles = [];
      for (let i = 0; i < WIND_PARTICLES; i++) this.particles.push(this.spawnParticle(world));
    }
    // Wall-clock time drives the visuals, so the wind keeps blowing and the
    // flame keeps fading while the game is held (dialog open, planning, ended).
    const now = performance.now() / 1000;
    const dt = Math.max(0, Math.min(0.1, now - this.lastDrawAt));
    this.lastDrawAt = now;

    const throttle = frame.inputs.thrust && frame.rocket.status === "flying" ? 1 : 0;
    this.flame += (throttle - this.flame) * (1 - Math.exp(-dt / FLAME_SMOOTHING_S));

    this.drawWind(frame, dt);

    // Terrain.
    ctx.strokeStyle = "#000";
    ctx.lineWidth = 2;
    ctx.lineJoin = "round";
    ctx.beginPath();
    world.terrain.forEach(([x, y], i) => {
      const p = this.toScreen(x, y, H);
      if (i === 0) ctx.moveTo(p.x, p.y);
      else ctx.lineTo(p.x, p.y);
    });
    ctx.stroke();

    // World bounds.
    const tl = this.toScreen(0, H, H);
    const br = this.toScreen(W, 0, H);
    ctx.strokeStyle = "#ccc";
    ctx.lineWidth = 1;
    ctx.strokeRect(tl.x, tl.y, br.x - tl.x, br.y - tl.y);

    // Launch pad.
    const l1 = this.toScreen(world.launchPad.x1, world.launchPad.y, H);
    const l2 = this.toScreen(world.launchPad.x2, world.launchPad.y, H);
    ctx.strokeStyle = "#555";
    ctx.lineWidth = 5;
    ctx.lineCap = "butt";
    ctx.beginPath();
    ctx.moveTo(l1.x, l1.y);
    ctx.lineTo(l2.x, l2.y);
    ctx.stroke();

    // Landing pad.
    const p1 = this.toScreen(world.pad.x1, world.pad.y, H);
    const p2 = this.toScreen(world.pad.x2, world.pad.y, H);
    ctx.strokeStyle = "#0a0";
    ctx.lineWidth = 6;
    ctx.lineCap = "butt";
    ctx.beginPath();
    ctx.moveTo(p1.x, p1.y);
    ctx.lineTo(p2.x, p2.y);
    ctx.stroke();

    this.drawRocket(frame);
    this.drawHud(frame);
    this.drawClock(frame);
  }

  /** Flight time, big and red in the top-right corner: the score. */
  private drawClock(frame: Frame): void {
    const { ctx } = this;
    ctx.font = "bold 40px ui-monospace, SFMono-Regular, Menlo, monospace";
    ctx.fillStyle = "#c00";
    ctx.textAlign = "right";
    ctx.textBaseline = "top";
    const right = this.offsetX + frame.world.info.width * this.scale - 16;
    ctx.fillText(`${frame.time.toFixed(1)} s`, right, this.offsetY + 12);
    ctx.textAlign = "start";
  }

  private drawWind(frame: Frame, dt: number): void {
    const { ctx } = this;
    const { world, wind, time } = frame;
    const H = world.info.height;
    const W = world.info.width;

    ctx.strokeStyle = "#bbb";
    ctx.lineWidth = 1;
    ctx.lineCap = "round";
    ctx.beginPath();
    for (let i = 0; i < this.particles.length; i++) {
      const p = this.particles[i] as Vec2;
      const w = windAt(wind, p.x, p.y, time);
      const speed = Math.hypot(w.x, w.y);
      p.x += w.x * dt * WIND_ADVECT;
      p.y += w.y * dt * WIND_ADVECT;
      if (p.x < 0 || p.x > W || p.y > H || p.y < terrainHeightAt(world.terrain, p.x)) {
        this.particles[i] = this.spawnParticle(world);
        continue;
      }
      if (speed < 0.05) continue;
      const len = Math.min(WIND_LINE_MAX, Math.max(WIND_LINE_MIN, speed * WIND_LINE_SCALE));
      const ux = w.x / speed;
      const uy = w.y / speed;
      const a = this.toScreen(p.x, p.y, H);
      const b = this.toScreen(p.x + ux * len, p.y + uy * len, H);
      ctx.moveTo(a.x, a.y);
      ctx.lineTo(b.x, b.y);
    }
    ctx.stroke();
  }

  /** Random point in the world above the terrain. */
  private spawnParticle(world: World): Vec2 {
    for (let attempt = 0; attempt < 10; attempt++) {
      const x = Math.random() * world.info.width;
      const y = Math.random() * world.info.height;
      if (y > terrainHeightAt(world.terrain, x) + 0.5) return { x, y };
    }
    return { x: Math.random() * world.info.width, y: world.info.height - 1 };
  }

  private drawRocket(frame: Frame): void {
    const { ctx } = this;
    const { rocket, world } = frame;
    const H = world.info.height;

    if (this.flame > 0.05) {
      const flicker = 0.96 + Math.random() * 0.08;
      const length = ROCKET_HEIGHT * 0.8 * this.flame * flicker;
      const halfWidth = ROCKET_HALF_BASE * 0.5 * Math.sqrt(this.flame);
      const flame: Vec2[] = [
        { x: halfWidth, y: -ROCKET_HEIGHT * 0.4 },
        { x: 0, y: -ROCKET_HEIGHT * 0.4 - length },
        { x: -halfWidth, y: -ROCKET_HEIGHT * 0.4 },
      ];
      ctx.fillStyle = "#fc0";
      this.polygon(
        flame.map((v) => toWorld(rocket, v)),
        H,
      );
      ctx.fill();
    }

    ctx.fillStyle = rocket.status === "crashed" ? "#888" : "#e5442b";
    ctx.strokeStyle = "#000";
    ctx.lineWidth = 1.5;
    ctx.lineJoin = "round";
    this.rocketPath(rocket, H);
    ctx.fill();
    ctx.stroke();
    this.drawWindow(rocket, H);
  }

  /** A small round porthole with a face looking out. */
  private drawWindow(rocket: Rocket, worldHeight: number): void {
    const { ctx } = this;
    const body = (x: number, y: number): Vec2 => {
      const w = toWorld(rocket, { x, y });
      return this.toScreen(w.x, w.y, worldHeight);
    };
    const px = (m: number): number => m * this.scale;
    const centreY = ROCKET_HEIGHT * 0.05;
    const windowR = 0.42;
    const headR = 0.28;
    const c = body(0, centreY);

    // Porthole.
    ctx.beginPath();
    ctx.arc(c.x, c.y, px(windowR), 0, Math.PI * 2);
    ctx.fillStyle = "#cfe8ff";
    ctx.fill();
    ctx.strokeStyle = "#000";
    ctx.lineWidth = 1;
    ctx.stroke();

    // Head.
    ctx.beginPath();
    ctx.arc(c.x, c.y, px(headR), 0, Math.PI * 2);
    ctx.fillStyle = "#f4c9a4";
    ctx.fill();

    // Eyes.
    ctx.fillStyle = "#000";
    for (const ex of [-0.1, 0.1]) {
      const e = body(ex, centreY + 0.06);
      ctx.beginPath();
      ctx.arc(e.x, e.y, Math.max(0.6, px(0.035)), 0, Math.PI * 2);
      ctx.fill();
    }

    // Smile: an arc on the lower half of the face, rotated with the rocket.
    const m = body(0, centreY - 0.04);
    ctx.beginPath();
    ctx.arc(m.x, m.y, px(0.14), rocket.angle + Math.PI * 0.15, rocket.angle + Math.PI * 0.85);
    ctx.strokeStyle = "#000";
    ctx.lineWidth = Math.max(0.6, px(0.03));
    ctx.stroke();
  }

  /**
   * Rocket outline: pointed tip, sides that bow out slightly wider than the
   * base, flat base. The physics still uses the triangle in ROCKET_VERTICES.
   */
  private rocketPath(rocket: Rocket, worldHeight: number): void {
    const { ctx } = this;
    const [tip, baseRight, baseLeft] = ROCKET_VERTICES as [Vec2, Vec2, Vec2];
    // Control points sit outside the hull so the curve bulges past the base width.
    const bulge = ROCKET_HALF_BASE * 1.35;
    const bulgeY = -ROCKET_HEIGHT * 0.05;
    const s = (v: Vec2): Vec2 =>
      this.toScreen(toWorld(rocket, v).x, toWorld(rocket, v).y, worldHeight);
    const t = s(tip);
    const br = s(baseRight);
    const bl = s(baseLeft);
    const cr = s({ x: bulge, y: bulgeY });
    const cl = s({ x: -bulge, y: bulgeY });
    ctx.beginPath();
    ctx.moveTo(t.x, t.y);
    ctx.quadraticCurveTo(cr.x, cr.y, br.x, br.y);
    ctx.lineTo(bl.x, bl.y);
    ctx.quadraticCurveTo(cl.x, cl.y, t.x, t.y);
    ctx.closePath();
  }

  private polygon(points: Vec2[], worldHeight: number): void {
    const { ctx } = this;
    ctx.beginPath();
    points.forEach((p, i) => {
      const s = this.toScreen(p.x, p.y, worldHeight);
      if (i === 0) ctx.moveTo(s.x, s.y);
      else ctx.lineTo(s.x, s.y);
    });
    ctx.closePath();
  }

  private hudLines: string[] = [];
  private hudUpdatedAt = Number.NEGATIVE_INFINITY;

  private drawHud(frame: Frame): void {
    const { ctx } = this;
    const { rocket } = frame;
    ctx.fillStyle = "#000";
    ctx.font = HUD_FONT;
    ctx.textBaseline = "top";
    const now = performance.now();
    if (now - this.hudUpdatedAt >= HUD_INTERVAL_MS) {
      this.hudUpdatedAt = now;
      this.hudLines = hudLines(frame);
      if (frame.strategy !== null) {
        this.hudLines.push("", ...wrapText(ctx, frame.strategy, STRATEGY_MAX_WIDTH_PX));
      }
    }
    this.hudLines.forEach((line, i) => {
      ctx.fillText(line, this.offsetX + 12, this.offsetY + 12 + i * HUD_LINE_PX);
    });

    if (rocket.status !== "flying" && !frame.paused) {
      const msg =
        rocket.status === "landed"
          ? "Landed!"
          : rocket.status === "crashed"
            ? "Crashed"
            : "Out of time";
      ctx.font = "bold 36px ui-monospace, SFMono-Regular, Menlo, monospace";
      ctx.textAlign = "center";
      const cx = this.offsetX + (frame.world.info.width * this.scale) / 2;
      const cy = this.offsetY + (frame.world.info.height * this.scale) / 3;
      ctx.fillStyle = rocket.status === "landed" ? "#0a0" : "#c00";
      ctx.fillText(msg, cx, cy);
      ctx.textAlign = "start";
    }
  }
}

function hudLines(frame: Frame): string[] {
  const { rocket, windAtRocket } = frame;
  const status = frame.paused && rocket.status === "flying" ? "paused" : rocket.status;
  return [
    `status  ${status}`,
    `time    ${frame.time.toFixed(1)} s`,
    `vel     ${rocket.vx.toFixed(1)}, ${rocket.vy.toFixed(1)} m/s`,
    `angle   ${((rocket.angle * 180) / Math.PI).toFixed(0)}°`,
    `wind    ${windAtRocket.x.toFixed(1)}, ${windAtRocket.y.toFixed(1)} m/s`,
    `pilot   ${pilotStatus(frame)}`,
    `seed    ${frame.world.seed}`,
  ];
}

function pilotStatus(frame: Frame): string {
  if (!frame.autopilot) return "off";
  if (frame.planning) return "planning flight";
  if (!frame.connected) return "connecting";
  return frame.serverActive ? "steering" : "connected";
}

/** Greedy word wrap for the current canvas font. */
function wrapText(ctx: CanvasRenderingContext2D, text: string, maxWidth: number): string[] {
  const lines: string[] = [];
  let line = "";
  for (const word of text.split(/\s+/)) {
    const candidate = line === "" ? word : `${line} ${word}`;
    if (line !== "" && ctx.measureText(candidate).width > maxWidth) {
      lines.push(line);
      line = word;
    } else {
      line = candidate;
    }
  }
  if (line !== "") lines.push(line);
  return lines;
}
