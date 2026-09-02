import { isMoveMessage, type MoveMessage, type StateMessage } from "./protocol";

const MIN_BACKOFF_MS = 1000;
const MAX_BACKOFF_MS = 5000;
/** Drop the server's move if nothing new has arrived within this window. */
const MOVE_TTL_MS = 500;

export interface ServerClient {
  readonly connected: boolean;
  /** Most recent move from the server, or null if stale/none. */
  readonly latestMove: MoveMessage | null;
  /** Sends the state if the socket is open and no reply is outstanding. */
  send(state: StateMessage): void;
}

export function connectServer(url: string): ServerClient {
  let socket: WebSocket | null = null;
  let connected = false;
  let awaitingReply = false;
  let latestMove: MoveMessage | null = null;
  let latestMoveAt = 0;
  let backoff = MIN_BACKOFF_MS;

  const open = (): void => {
    const ws = new WebSocket(url);
    socket = ws;
    ws.addEventListener("open", () => {
      connected = true;
      awaitingReply = false;
      backoff = MIN_BACKOFF_MS;
    });
    ws.addEventListener("message", (event) => {
      awaitingReply = false;
      let parsed: unknown;
      try {
        parsed = JSON.parse(String(event.data));
      } catch {
        return;
      }
      if (isMoveMessage(parsed)) {
        latestMove = parsed;
        latestMoveAt = performance.now();
      }
    });
    const onClose = (): void => {
      if (socket !== ws) return;
      socket = null;
      connected = false;
      awaitingReply = false;
      latestMove = null;
      setTimeout(open, backoff);
      backoff = Math.min(MAX_BACKOFF_MS, backoff * 2);
    };
    ws.addEventListener("close", onClose);
    ws.addEventListener("error", () => ws.close());
  };
  open();

  return {
    get connected() {
      return connected;
    },
    get latestMove() {
      if (latestMove !== null && performance.now() - latestMoveAt > MOVE_TTL_MS) {
        latestMove = null;
      }
      return latestMove;
    },
    send(state) {
      if (socket === null || !connected || awaitingReply) return;
      if (socket.readyState !== WebSocket.OPEN) return;
      awaitingReply = true;
      socket.send(JSON.stringify(state));
    },
  };
}
