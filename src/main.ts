import { connectServer, type ServerClient } from "./client";
import { createDialog, parseSeed, type ShowOptions } from "./dialog";
import { attachKeyboard } from "./input";
import { createRocket, DT, type Inputs, PHYSICS, type Rocket, restingY, step } from "./physics";
import { isPlanResponse, type StateMessage } from "./protocol";
import { mulberry32, randomSeed } from "./random";
import { type Frame, Renderer } from "./render";
import { generateWind, type WindField, windAt } from "./wind";
import { generateWorld, type World, worldWidthFor } from "./world";

/** The auto-pilot server: `GET /plan` writes the script, `/ws` flies it. */
const SERVER_URL: string =
  typeof import.meta.env.VITE_SERVER_URL === "string"
    ? import.meta.env.VITE_SERVER_URL
    : "http://localhost:8000";
const PLAN_URL = new URL("/plan", SERVER_URL).href;
const WS_URL = ((): string => {
  const url = new URL("/ws", SERVER_URL);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  return url.href;
})();

const MAX_FRAME_S = 0.25;
const RESIZE_DEBOUNCE_MS = 200;
/** How long the outcome stays on screen before the new-game dialog appears. */
const END_DIALOG_DELAY_MS = 1200;
/** How long to wait for the auto-pilot server before giving up and reporting it. */
const CONNECT_TIMEOUT_MS = 1000;

interface Game {
  world: World;
  wind: WindField;
  rocket: Rocket;
  tick: number;
  time: number;
}

function spawn(seed: number = randomSeed()): Game {
  const rng = mulberry32(seed);
  const width = worldWidthFor(window.innerWidth / Math.max(1, window.innerHeight));
  const world = generateWorld(rng, seed, width);
  const wind = generateWind(rng);
  const { launchPad } = world;
  const rocket = createRocket((launchPad.x1 + launchPad.x2) / 2, restingY(launchPad.y), 0);
  return { world, wind, rocket, tick: 0, time: 0 };
}

function buildState(game: Game, windAtRocket: { x: number; y: number }): StateMessage {
  const { rocket, world } = game;
  return {
    type: "state",
    tick: game.tick,
    status: rocket.status,
    rocket: {
      x: rocket.x,
      y: rocket.y,
      vx: rocket.vx,
      vy: rocket.vy,
      angle: rocket.angle,
      angularVelocity: rocket.angularVelocity,
    },
    wind: windAtRocket,
    pad: world.pad,
    launchPad: world.launchPad,
    terrain: world.terrain,
    world: world.info,
    physics: PHYSICS,
  };
}

function main(): void {
  const canvas = document.getElementById("game");
  if (!(canvas instanceof HTMLCanvasElement)) throw new Error("#game canvas not found");

  const renderer = new Renderer(canvas);
  const keyboard = attachKeyboard(window);
  let server: ServerClient | null = null;
  let game = spawn(seedFromUrl());
  let endTimer: number | undefined;
  let autopilot = false;
  let autoReplay = false;
  /** Waiting for the plan and the connection; physics is held meanwhile. */
  let planning = false;
  let strategy: string | null = null;
  /** Bumped on every start so a plan that arrives late is thrown away. */
  let flightId = 0;

  const disconnect = (): void => {
    server?.close();
    server = null;
  };

  /**
   * Start flying the given seed. With the auto-pilot on, first ask the server
   * for a plan and connect; the game stays paused until both are in place.
   */
  const beginFlight = async (seed: number | undefined): Promise<void> => {
    const id = ++flightId;
    disconnect();
    window.clearTimeout(endTimer);
    endTimer = undefined;
    strategy = null;
    game = spawn(seed);
    resize();
    writeSeedToUrl(game.world.seed);
    if (!autopilot) return;
    planning = true;
    try {
      strategy = (await fetchPlan()).strategy;
      if (id !== flightId) return;
      server = connectServer(WS_URL);
      await waitForConnection(server, CONNECT_TIMEOUT_MS);
    } catch (error) {
      if (id !== flightId) return;
      disconnect();
      planning = false;
      openDialog({ error: error instanceof Error ? error.message : String(error) });
      return;
    }
    if (id === flightId) planning = false;
  };

  const dialog = createDialog(document, {
    onStart({ autopilot: wanted, autoReplay: replayWanted, seed }) {
      autopilot = wanted;
      autoReplay = replayWanted;
      void beginFlight(seed);
    },
  });
  const openDialog = (options?: ShowOptions): void => {
    flightId += 1; // abandon any plan in progress
    planning = false;
    disconnect();
    window.clearTimeout(endTimer);
    endTimer = undefined;
    keyboard.consumeRespawn();
    dialog.show(options);
  };

  const resize = (): void => renderer.resize(game.world.info.width, game.world.info.height);
  // The world is sized to the viewport, so a resize means a new world.
  let resizeTimer: number | undefined;
  window.addEventListener("resize", () => {
    resize();
    window.clearTimeout(resizeTimer);
    resizeTimer = window.setTimeout(() => {
      // Same seed, regenerated for the new width.
      game = spawn(game.world.seed);
      resize();
    }, RESIZE_DEBOUNCE_MS);
  });
  resize();

  let last = performance.now();
  let accumulator = 0;
  let inputs: Inputs = { thrust: false, left: false, right: false };
  let windAtRocket = windAt(game.wind, game.rocket.x, game.rocket.y, 0);

  const frame = (now: number): void => {
    accumulator += Math.min(MAX_FRAME_S, (now - last) / 1000);
    last = now;
    const paused = dialog.open || planning;

    if (paused) {
      // Keep drawing the last state behind the dialog or while planning.
      accumulator = 0;
      if (dialog.open) keyboard.consumeRespawn();
    }
    if (!dialog.open && keyboard.consumeRespawn()) {
      accumulator = 0;
      openDialog({ seed: game.world.seed });
    }

    while (!paused && !dialog.open && accumulator >= DT) {
      accumulator -= DT;
      const move = server?.latestMove ?? null;
      const keys = keyboard.inputs;
      // Thrust is additive, but a held rotation key takes the rotation axis
      // away from the auto-pilot so the player can steer against it.
      const manualRotation = keys.left || keys.right;
      inputs = {
        thrust: keys.thrust || (move?.thrust ?? false),
        left: manualRotation ? keys.left : (move?.left ?? false),
        right: manualRotation ? keys.right : (move?.right ?? false),
      };
      const { rocket, world } = game;
      windAtRocket = windAt(game.wind, rocket.x, rocket.y, game.time);
      step(
        rocket,
        inputs,
        windAtRocket,
        world.info.gravity,
        world.terrain,
        world.pad,
        world.launchPad,
        world.info.width,
        world.info.height,
      );
      game.tick += 1;
      // Game time is the score, so it stops the moment the flight ends.
      if (rocket.status === "flying") {
        game.time += DT;
        if (game.time >= PHYSICS.maxFlightTime) rocket.status = "timeout";
      }
      server?.send(buildState(game, windAtRocket));
      if (rocket.status !== "flying" && endTimer === undefined) {
        // One flight per connection: the server has seen the outcome, so
        // after a moment drop the socket and either replay or ask what next.
        const summary = { result: rocket.status, time: game.time, seed: world.seed };
        endTimer = window.setTimeout(() => {
          disconnect();
          if (autoReplay) void beginFlight(game.world.seed);
          else openDialog(summary);
        }, END_DIALOG_DELAY_MS);
      }
    }

    const move = server?.latestMove ?? null;
    const view: Frame = {
      world: game.world,
      wind: game.wind,
      rocket: game.rocket,
      inputs,
      time: game.time,
      paused,
      windAtRocket,
      autopilot,
      planning,
      connected: server?.connected ?? false,
      serverActive: move !== null && (move.thrust || move.left || move.right),
      strategy,
    };
    renderer.draw(view);
    requestAnimationFrame(frame);
  };
  requestAnimationFrame(frame);
  dialog.show({ seed: game.world.seed });
}

/** Ask the server to write a script for the next flight. Throws with a readable message. */
async function fetchPlan(): Promise<{ strategy: string }> {
  let response: Response;
  try {
    response = await fetch(PLAN_URL);
  } catch {
    throw new Error(`Could not reach the auto-pilot server at ${PLAN_URL}. Is it running?`);
  }
  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    const detail =
      typeof body === "object" && body !== null && "detail" in body
        ? String((body as { detail: unknown }).detail)
        : response.statusText;
    throw new Error(`The auto-pilot could not plan the flight: ${detail}`);
  }
  if (!isPlanResponse(body)) throw new Error("The auto-pilot server sent an invalid plan.");
  return body;
}

function waitForConnection(client: ServerClient, timeoutMs: number): Promise<void> {
  return new Promise((resolve, reject) => {
    const started = performance.now();
    const poll = (): void => {
      if (client.connected) resolve();
      else if (performance.now() - started > timeoutMs) {
        reject(
          new Error(
            `Could not connect to the auto-pilot server at ${WS_URL} within ${timeoutMs} ms. Is it running?`,
          ),
        );
      } else window.setTimeout(poll, 50);
    };
    poll();
  });
}

/** `?seed=N` in the URL, so a reload reruns the same game. */
function seedFromUrl(): number | undefined {
  const raw = new URLSearchParams(window.location.search).get("seed");
  return raw === null ? undefined : parseSeed(raw);
}

function writeSeedToUrl(seed: number): void {
  const url = new URL(window.location.href);
  url.searchParams.set("seed", String(seed));
  window.history.replaceState(null, "", url);
}

main();
