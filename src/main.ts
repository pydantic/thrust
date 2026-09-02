import { connectServer } from "./client";
import { attachKeyboard } from "./input";
import { createRocket, DT, type Inputs, type Rocket, restingY, step } from "./physics";
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

interface Game {
  world: World;
  wind: WindField;
  rocket: Rocket;
  tick: number;
  time: number;
}

function spawn(): Game {
  const seed = randomSeed();
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
  };
}

function main(): void {
  const canvas = document.getElementById("game");
  if (!(canvas instanceof HTMLCanvasElement)) throw new Error("#game canvas not found");

  const renderer = new Renderer(canvas);
  const keyboard = attachKeyboard(window);
  const server = connectServer(WS_URL);
  let game = spawn();

  const resize = (): void => renderer.resize(game.world.info.width, game.world.info.height);
  // The world is sized to the viewport, so a resize means a new world.
  let resizeTimer: number | undefined;
  window.addEventListener("resize", () => {
    resize();
    window.clearTimeout(resizeTimer);
    resizeTimer = window.setTimeout(() => {
      game = spawn();
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

    if (keyboard.consumeRespawn()) {
      game = spawn();
      accumulator = 0;
    }

    while (accumulator >= DT) {
      accumulator -= DT;
      const move = server.latestMove;
      inputs = {
        thrust: keyboard.inputs.thrust || (move?.thrust ?? false),
        left: keyboard.inputs.left || (move?.left ?? false),
        right: keyboard.inputs.right || (move?.right ?? false),
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
      game.time += DT;
      server.send(buildState(game, windAtRocket));
    }

    const move = server.latestMove;
    const view: Frame = {
      world: game.world,
      wind: game.wind,
      rocket: game.rocket,
      inputs,
      time: game.time,
      windAtRocket,
      connected: server.connected,
      serverActive: move !== null && (move.thrust || move.left || move.right),
    };
    renderer.draw(view);
    requestAnimationFrame(frame);
  };
  requestAnimationFrame(frame);
}

main();
