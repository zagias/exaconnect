import { useState } from "react";
import { api } from "../../api";
import { ErrorNote } from "../../components";
import { useAction } from "../../ui";

/** Sending limits for one email address (ADR 0032): emails a day in all, and
 * a day to any one domain, so a mistake can't burn the address's reputation. */
export function EmailLimits({ base, a, reload }: { base: string; a: { id: string; settings: Record<string, unknown> }; reload: () => void }) {
  const current = { daily: 2000, per_domain_daily: 300, ...((a.settings.email_limits as Record<string, number> | undefined) ?? {}) };
  const [daily, setDaily] = useState(String(current.daily));
  const [perDomain, setPerDomain] = useState(String(current.per_domain_daily));
  const save = useAction();
  return (
    <form
      className="form"
      onSubmit={(e) => {
        e.preventDefault();
        save.run(async () => {
          const email_limits = { daily: Number(daily), per_domain_daily: Number(perDomain) };
          if (!Object.values(email_limits).every((v) => Number.isInteger(v) && v >= 1)) throw new Error("Limits are whole numbers of 1 or more.");
          await api(`${base}/channel-accounts/${a.id}`, { method: "PATCH", body: JSON.stringify({ settings: { email_limits } }) });
          reload();
        });
      }}
    >
      <label>
        Emails a day
        <input type="number" inputMode="numeric" min={1} max={1000000} value={daily} onChange={(e) => setDaily(e.target.value)} />
      </label>
      <label>
        Emails a day to one domain
        <input type="number" inputMode="numeric" min={1} max={1000000} value={perDomain} onChange={(e) => setPerDomain(e.target.value)} />
      </label>
      <div className="actions">
        <button className="button secondary small" disabled={save.busy}>
          Save limits
        </button>
      </div>
      <ErrorNote error={save.error} />
    </form>
  );
}
