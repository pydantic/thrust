import type { Status } from "./protocol";

const AI_STORAGE_KEY = "thrust.aiControl";

export interface ShowOptions {
  /** Outcome of the game that just ended. */
  result?: Exclude<Status, "flying">;
  /** Problem with the previous start attempt, e.g. the AI server was unreachable. */
  error?: string;
}

export interface Dialog {
  readonly open: boolean;
  show(options?: ShowOptions): void;
}

export interface DialogOptions {
  onStart(aiControl: boolean): void;
}

export function createDialog(document: Document, options: DialogOptions): Dialog {
  const root = must(document.getElementById("dialog"), "#dialog");
  const form = must(document.getElementById("dialog-form"), "#dialog-form");
  if (!(form instanceof HTMLFormElement)) throw new Error("#dialog-form is not a form");
  const result = must(document.getElementById("dialog-result"), "#dialog-result");
  const error = must(document.getElementById("dialog-error"), "#dialog-error");
  const aiCheckbox = must(document.getElementById("ai-control"), "#ai-control");
  if (!(aiCheckbox instanceof HTMLInputElement)) throw new Error("#ai-control is not an input");

  aiCheckbox.checked = loadAiPreference();

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    root.hidden = true;
    saveAiPreference(aiCheckbox.checked);
    options.onStart(aiCheckbox.checked);
  });

  // Space starts the game with the checkbox as-is (unless the checkbox itself
  // has focus, where space toggles it). preventDefault also tells the game's
  // key handler to ignore this press.
  document.addEventListener("keydown", (event) => {
    if (root.hidden || event.code !== "Space" || event.target === aiCheckbox) return;
    event.preventDefault();
    form.requestSubmit();
  });

  return {
    get open() {
      return !root.hidden;
    },
    show({ result: outcome, error: message } = {}) {
      if (outcome === undefined) {
        result.hidden = true;
      } else {
        result.hidden = false;
        result.textContent = outcome === "landed" ? "Landed!" : "Crashed";
        result.className = `result ${outcome}`;
      }
      error.hidden = message === undefined;
      error.textContent = message ?? "";
      root.hidden = false;
      const button = form.querySelector("button");
      button?.focus();
    },
  };
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
