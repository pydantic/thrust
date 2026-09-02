import { isAbortMessage, isMoveMessage, type MoveMessage, type StateMessage } from "./protocol";

const MIN_BACKOFF_MS = 1000;
const MAX_BACKOFF_MS = 5000;
/** Drop the server's move if nothing new has arrived within this window. */
const MOVE_TTL_MS = 500;
/** Close code the server uses when it will not fly this connection (no plan). */
const REFUSED_CLOSE_CODE = 1008;

export interface ServerClient {
  readonly connected: boolean;
  /** Most recent move from the server, or null if stale/none. */
  readonly latestMove: MoveMessage | null;
  /** Set once the server has aborted the flight: why the script died. */
  readonly abortReason: string | null;
  /** Sends the state if the socket is open and no reply is outstanding. */
  send(state: StateMessage): void;
  /** Close the socket and stop reconnecting. */
  close(): void;
}

export function connectServer(url: string): ServerClient {
  let socket: WebSocket | null = null;
  let connected = false;
  let awaitingReply = false;
  let latestMove: MoveMessage | null = null;
  let latestMoveAt = 0;
  let abortReason: string | null = null;
  let backoff = MIN_BACKOFF_MS;
  let closed = false;
  let reconnectTimer: number | undefined;

  const open = (): void => {
    if (closed) return;
    const ws = new WebSocket(url);
    socket = ws;
    ws.addEventListener("open", () => {
      connected = true;
      awaitingReply = false;
      backoff = MIN_BACKOFF_MS;
    });
    ws.addEventListener("message", (event) => {
      let parsed: unknown;
      try {
        parsed = JSON.parse(String(event.data));
      } catch {
        return;
      }
      if (isMoveMessage(parsed)) {
        awaitingReply = false;
        latestMove = parsed;
        latestMoveAt = performance.now();
      } else if (isAbortMessage(parsed)) {
        awaitingReply = false;
        latestMove = null;
        abortReason = parsed.reason;
      }
    });
    const onClose = (event: CloseEvent): void => {
      if (socket !== ws) return;
      socket = null;
      connected = false;
      awaitingReply = false;
      latestMove = null;
      if (event.code === REFUSED_CLOSE_CODE) {
        // The server refused the flight (no plan): report it, do not retry.
        abortReason = event.reason || "the auto-pilot server refused the flight";
        closed = true;
      }
      if (closed) return;
      reconnectTimer = window.setTimeout(open, backoff);
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
    get abortReason() {
      return abortReason;
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
    close() {
      closed = true;
      window.clearTimeout(reconnectTimer);
      const ws = socket;
      socket = null;
      connected = false;
      latestMove = null;
      ws?.close();
    },
  };
}
