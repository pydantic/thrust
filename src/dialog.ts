import type { Status } from "./protocol";

const AI_STORAGE_KEY = "thrust.aiControl";

export interface ShowOptions {
  /** Outcome of the game that just ended. */
  result?: Exclude<Status, "flying">;
  /** Game time in seconds at which the game ended. */
  time?: number;
  /** Seed of the game just played; prefilled so "Replay seed" reruns it. */
  seed?: number;
  /** Problem with the previous start attempt, e.g. the AI server was unreachable. */
  error?: string;
}

export interface Dialog {
  readonly open: boolean;
  show(options?: ShowOptions): void;
}

export interface StartRequest {
  aiControl: boolean;
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
  const aiCheckbox = must(document.getElementById("ai-control"), "#ai-control");
  const seedInput = must(document.getElementById("seed"), "#seed");
  const newButton = must(document.getElementById("start-new"), "#start-new");
  const replayButton = must(document.getElementById("start-replay"), "#start-replay");
  if (!(aiCheckbox instanceof HTMLInputElement)) throw new Error("#ai-control is not an input");
  if (!(seedInput instanceof HTMLInputElement)) throw new Error("#seed is not an input");
  if (!(newButton instanceof HTMLButtonElement)) throw new Error("#start-new is not a button");
  if (!(replayButton instanceof HTMLButtonElement)) {
    throw new Error("#start-replay is not a button");
  }

  aiCheckbox.checked = loadAiPreference();

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const replay = event.submitter === replayButton;
    const seed = replay ? parseSeed(seedInput.value) : undefined;
    if (replay && seed === undefined) {
      showError("Enter a seed (a whole number) to replay.");
      return;
    }
    root.hidden = true;
    saveAiPreference(aiCheckbox.checked);
    options.onStart({ aiControl: aiCheckbox.checked, seed });
  });

  // Space starts a new game with the checkbox as-is, unless a control that
  // uses space itself has focus. preventDefault also tells the game's key
  // handler to ignore this press.
  document.addEventListener("keydown", (event) => {
    if (root.hidden || event.code !== "Space") return;
    if (event.target === aiCheckbox || event.target === seedInput) return;
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
        result.textContent =
          outcome === "landed" ? `Landed in${seconds}` : `Crashed after${seconds}`;
        result.className = `result ${outcome}`;
      }
      if (seed !== undefined) seedInput.value = String(seed);
      showError(message);
      root.hidden = false;
      newButton.focus();
    },
  };
}

/** Accepts a non-negative integer; anything else is undefined. */
function parseSeed(text: string): number | undefined {
  const trimmed = text.trim();
  if (!/^\d{1,10}$/.test(trimmed)) return undefined;
  const n = Number(trimmed);
  return n <= 0xffffffff ? n : undefined;
}

function must<T>(value: T | null, what: string): T {
  if (value === null) throw new Error(`${what} not found`);
  return value;
}

function loadAiPreference(): boolean {
  try {
    return localStorage.getItem(AI_STORAGE_KEY) === "1";
  } catch {
    return false;
  }
}

function saveAiPreference(enabled: boolean): void {
  try {
    localStorage.setItem(AI_STORAGE_KEY, enabled ? "1" : "0");
  } catch {
    // Storage unavailable; the checkbox just won't be remembered.
  }
}
