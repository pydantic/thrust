import { connectServer, type ServerClient } from "./client";
import { createDialog, parseSeed, type ShowOptions } from "./dialog";
import { attachKeyboard } from "./input";
import { createRocket, DT, type Inputs, PHYSICS, type Rocket, restingY, step } from "./physics";
import type { StateMessage } from "./protocol";
import { mulberry32, randomSeed } from "./random";
import { type Frame, Renderer } from "./render";
import { generateWind, type WindField, windAt } from "./wind";
import { generateWorld, type World, worldWidthFor } from "./world";

const WS_URL: string =
  typeof import.meta.env.VITE_WS_URL === "string"
    ? import.meta.env.VITE_WS_URL
    : "ws://localhost:8000/ws";

const MAX_FRAME_S = 0.25;
const RESIZE_DEBOUNCE_MS = 200;
/** How long the outcome stays on screen before the new-game dialog appears. */
const END_DIALOG_DELAY_MS = 1200;
/** How long to wait for the AI server before giving up and reporting it. */
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
  let endDialogTimer: number | undefined;
  let connectTimer: number | undefined;

  const dialog = createDialog(document, {
    onStart({ aiControl, seed }) {
      window.clearTimeout(connectTimer);
      if (aiControl && server === null) server = connectServer(WS_URL);
      if (!aiControl && server !== null) {
        server.close();
        server = null;
      }
      if (aiControl) {
        connectTimer = window.setTimeout(() => {
          if (server === null || server.connected) return;
          server.close();
          server = null;
          openDialog({
            error: `Could not connect to the AI server at ${WS_URL} within ${CONNECT_TIMEOUT_MS} ms. Is it running?`,
          });
        }, CONNECT_TIMEOUT_MS);
      }
      game = spawn(seed);
      resize();
      writeSeedToUrl(game.world.seed);
    },
  });
  const openDialog = (options?: ShowOptions): void => {
    window.clearTimeout(endDialogTimer);
    endDialogTimer = undefined;
    window.clearTimeout(connectTimer);
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

    if (dialog.open) {
      // Paused: keep drawing the last state behind the dialog.
      accumulator = 0;
      keyboard.consumeRespawn();
    } else if (keyboard.consumeRespawn()) {
      accumulator = 0;
      openDialog({ seed: game.world.seed });
    }

    while (!dialog.open && accumulator >= DT) {
      accumulator -= DT;
      const move = server?.latestMove ?? null;
      const keys = keyboard.inputs;
      // Thrust is additive, but a held rotation key takes the rotation axis
      // away from the AI so the player can steer against it.
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
      );
      game.tick += 1;
      // Game time is the score, so it stops the moment the flight ends.
      if (rocket.status === "flying") game.time += DT;
      server?.send(buildState(game, windAtRocket));
      if (rocket.status !== "flying" && endDialogTimer === undefined) {
        const outcome = rocket.status;
        const summary = { result: outcome, time: game.time, seed: world.seed };
        endDialogTimer = window.setTimeout(() => openDialog(summary), END_DIALOG_DELAY_MS);
      }
    }

    const move = server?.latestMove ?? null;
    const view: Frame = {
      world: game.world,
      wind: game.wind,
      rocket: game.rocket,
      inputs,
      time: game.time,
      paused: dialog.open,
      windAtRocket,
      aiControl: server !== null,
      connected: server?.connected ?? false,
      serverActive: move !== null && (move.thrust || move.left || move.right),
    };
    renderer.draw(view);
    requestAnimationFrame(frame);
  };
  requestAnimationFrame(frame);
  dialog.show({ seed: game.world.seed });
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
