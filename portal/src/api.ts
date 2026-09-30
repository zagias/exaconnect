import { useEffect, useState } from "react";

export interface ControllerStatus {
  ok: boolean;
  version?: string;
}

/** Polls the controller's version endpoint so the header shows whether it is reachable. */
export function useControllerStatus(intervalMs = 10_000): ControllerStatus {
  const [status, setStatus] = useState<ControllerStatus>({ ok: false });
  useEffect(() => {
    let cancelled = false;
    const check = async () => {
      try {
        const r = await fetch("/api/v1/version");
        const body = r.ok ? await r.json() : null;
        if (!cancelled) setStatus(body ? { ok: true, version: body.version } : { ok: false });
      } catch {
        if (!cancelled) setStatus({ ok: false });
      }
    };
    check();
    const id = setInterval(check, intervalMs);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [intervalMs]);
  return status;
}
