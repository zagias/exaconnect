import { useState, type ReactNode } from "react";

/** Runs a form action with a busy flag and an error message. */
export function useAction() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const run = async (fn: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return { busy, error, run, setError };
}

export function Card({ title, children, note }: { title: string; children: ReactNode; note?: ReactNode }) {
  return (
    <section className="card" style={{ marginBottom: 24 }}>
      <div className="card-head">
        <h2>{title}</h2>
        {note}
      </div>
      {children}
    </section>
  );
}
