import { useEffect, useRef } from "react";

/**
 * Live inbox events over the controller's web socket (signed in by the
 * session cookie). Calls `onEvent` for each event; the caller also polls, so
 * a blocked or dropped socket only makes updates a little slower.
 */
export function useLive(base: string | null, onEvent: (type: string, data: Record<string, unknown>) => void) {
  const cb = useRef(onEvent);
  cb.current = onEvent;
  useEffect(() => {
    if (!base || typeof WebSocket === "undefined") return;
    let ws: WebSocket | null = null;
    let stopped = false;
    let retry: ReturnType<typeof setTimeout> | undefined;
    let tries = 0;
    const open = () => {
      const proto = location.protocol === "https:" ? "wss:" : "ws:";
      ws = new WebSocket(`${proto}//${location.host}/api/v1${base}/live`);
      ws.onopen = () => {
        tries = 0;
        ws?.send(JSON.stringify({ token: "" }));
      };
      ws.onmessage = (m) => {
        try {
          const e = JSON.parse(m.data as string);
          if (e.type && e.type !== "ready") cb.current(e.type, e.data ?? {});
        } catch {
          /* ignore malformed frames */
        }
      };
      ws.onclose = () => {
        if (stopped) return;
        tries += 1;
        if (tries < 6) retry = setTimeout(open, Math.min(30_000, 1000 * 2 ** tries));
      };
    };
    open();
    return () => {
      stopped = true;
      clearTimeout(retry);
      ws?.close();
    };
  }, [base]);
}
