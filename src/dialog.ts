import type { Status } from "./protocol";

const AUTOPILOT_STORAGE_KEY = "thrust.autopilot";

export interface ShowOptions {
  /** Outcome of the game that just ended. */
  result?: Exclude<Status, "flying">;
  /** Game time in seconds at which the game ended. */
  time?: number;
  /** Seed of the game just played; prefilled so "Replay" reruns it. */
  seed?: number;
  /** Problem with the previous start attempt, e.g. the auto-pilot server was unreachable. */
  error?: string;
}

export interface Dialog {
  readonly open: boolean;
  show(options?: ShowOptions): void;
}

export interface StartRequest {
  autopilot: boolean;
  /** Replay the same seed automatically when a flight ends, instead of showing the dialog. */
  autoReplay: boolean;
  /** Seed to replay; undefined means pick a random one. */
  seed: number | undefined;
}

export interface DialogOptions {
  onStart(request: StartRequest): void;
}

export function createDialog(document: Document, options: DialogOptions): Dialog {
  const root = must(document.getElementById("dialog"), "#dialog");
  const form = must(document.getElementById("dialog-form"), "#dialog-form");
  if (!(form instanceof HTMLFormElement)) throw new Error("#dialog-form is not a form");
  const result = must(document.getElementById("dialog-result"), "#dialog-result");
  const error = must(document.getElementById("dialog-error"), "#dialog-error");
  const autopilotCheckbox = must(document.getElementById("autopilot"), "#autopilot");
  const autoReplayCheckbox = must(document.getElementById("autoreplay"), "#autoreplay");
  const seedLabel = must(document.getElementById("seed-label"), "#seed-label");
  const seedInput = must(document.getElementById("seed"), "#seed");
  const newButton = must(document.getElementById("start-new"), "#start-new");
  const replayButton = must(document.getElementById("start-replay"), "#start-replay");
  if (!(autopilotCheckbox instanceof HTMLInputElement))
    throw new Error("#autopilot is not an input");
  if (!(autoReplayCheckbox instanceof HTMLInputElement))
    throw new Error("#autoreplay is not an input");
  if (!(seedInput instanceof HTMLInputElement)) throw new Error("#seed is not an input");
  if (!(newButton instanceof HTMLButtonElement)) throw new Error("#start-new is not a button");
  if (!(replayButton instanceof HTMLButtonElement)) {
    throw new Error("#start-replay is not a button");
  }

  autopilotCheckbox.checked = loadPreference(AUTOPILOT_STORAGE_KEY);
  // Auto-replay is deliberate each time: always shown, never remembered.
  autoReplayCheckbox.checked = false;

  /**
   * Until a seed is pinned in the URL the dialog is minimal: the two checkboxes
   * and a Start button that runs the map already on screen.
   */
  let pinned = false;
  /** Seed of the map behind the dialog, which Start runs when nothing is pinned. */
  let shownSeed: number | undefined;

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const replay = event.submitter === replayButton;
    let seed: number | undefined;
    if (replay) {
      seed = parseSeed(seedInput.value);
      if (seed === undefined) {
        showError("Enter a seed (a whole number) to replay.");
        return;
      }
    } else if (!pinned) {
      seed = shownSeed;
    }
    root.hidden = true;
    savePreference(AUTOPILOT_STORAGE_KEY, autopilotCheckbox.checked);
    options.onStart({
      autopilot: autopilotCheckbox.checked,
      autoReplay: autoReplayCheckbox.checked,
      seed,
    });
  });

  // Space starts a new game with the checkbox as-is, unless a control that
  // uses space itself has focus. preventDefault also tells the game's key
  // handler to ignore this press.
  document.addEventListener("keydown", (event) => {
    if (root.hidden || event.code !== "Space") return;
    if (event.target === autopilotCheckbox || event.target === autoReplayCheckbox) return;
    if (event.target === seedInput) return;
    if (event.target === replayButton) return;
    event.preventDefault();
    form.requestSubmit(newButton);
  });

  const showError = (message: string | undefined): void => {
    error.hidden = message === undefined;
    error.textContent = message ?? "";
  };

  return {
    get open() {
      return !root.hidden;
    },
    show({ result: outcome, time, seed, error: message } = {}) {
      if (outcome === undefined) {
        result.hidden = true;
      } else {
        result.hidden = false;
        const seconds = time === undefined ? "" : ` ${time.toFixed(1)} s`;
        result.textContent = resultText(outcome, seconds);
        result.className = `result ${outcome}`;
      }
      if (seed !== undefined) seedInput.value = String(seed);
      shownSeed = seed;
      pinned = new URLSearchParams(document.location.search).has("seed");
      seedLabel.hidden = !pinned;
      replayButton.hidden = !pinned;
      newButton.textContent = pinned ? "New game" : "Start";
      showError(message);
      root.hidden = false;
      newButton.focus();
    },
  };
}

function resultText(outcome: Exclude<Status, "flying">, seconds: string): string {
  switch (outcome) {
    case "landed":
      return `Landed in${seconds}`;
    case "crashed":
      return `Crashed after${seconds}`;
    case "timeout":
      return `Out of time after${seconds}`;
    case "aborted":
      return `Script failed after${seconds}`;
  }
}

/** Accepts a non-negative integer; anything else is undefined. */
export function parseSeed(text: string): number | undefined {
  const trimmed = text.trim();
  if (!/^\d{1,10}$/.test(trimmed)) return undefined;
  const n = Number(trimmed);
  return n <= 0xffffffff ? n : undefined;
}

function must<T>(value: T | null, what: string): T {
  if (value === null) throw new Error(`${what} not found`);
  return value;
}

function loadPreference(key: string): boolean {
  try {
    return localStorage.getItem(key) === "1";
  } catch {
    return false;
  }
}

function savePreference(key: string, enabled: boolean): void {
  try {
    localStorage.setItem(key, enabled ? "1" : "0");
  } catch {
    // Storage unavailable; the checkbox just won't be remembered.
  }
}
