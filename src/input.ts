import type { Inputs } from "./physics";

export interface Keyboard {
  readonly inputs: Inputs;
  /** True once since the last call to consumeRespawn. */
  consumeRespawn(): boolean;
}

export function attachKeyboard(target: Window): Keyboard {
  const inputs: Inputs = { thrust: false, left: false, right: false };
  let respawnRequested = false;

  const setKey = (code: string, down: boolean): boolean => {
    switch (code) {
      case "ArrowUp":
        inputs.thrust = down;
        return true;
      case "ArrowLeft":
        inputs.left = down;
        return true;
      case "ArrowRight":
        inputs.right = down;
        return true;
      default:
        return false;
    }
  };

  target.addEventListener("keydown", (e) => {
    if (setKey(e.code, true)) e.preventDefault();
    if (e.code === "KeyR" || e.code === "Space") {
      respawnRequested = true;
      e.preventDefault();
    }
  });
  target.addEventListener("keyup", (e) => {
    if (setKey(e.code, false)) e.preventDefault();
  });
  target.addEventListener("blur", () => {
    inputs.thrust = false;
    inputs.left = false;
    inputs.right = false;
  });

  return {
    inputs,
    consumeRespawn: () => {
      const r = respawnRequested;
      respawnRequested = false;
      return r;
    },
  };
}
