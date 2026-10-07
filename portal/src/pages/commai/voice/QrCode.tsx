import { useEffect, useState } from "react";
import { useCommaiBase } from "../lib";

/**
 * A QR code for a softphone sign-in link (ADR 0033). The controller draws it
 * as SVG (POST, so the link never sits in a URL); it is shown as an image, so
 * nothing in it can run. No outside service sees the link.
 */
export function QrCode({ text, label = "QR code for the softphone sign-in link" }: { text: string; label?: string }) {
  const base = useCommaiBase();
  const [src, setSrc] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    if (!base || !text) return;
    let url: string | null = null;
    let cancelled = false;
    fetch(`/api/v1${base}/qr`, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-Requested-With": "exa-portal" },
      body: JSON.stringify({ text, label }),
    })
      .then((r) => (r.ok ? r.blob() : Promise.reject(new Error(String(r.status)))))
      .then((b) => {
        if (cancelled) return;
        url = URL.createObjectURL(new Blob([b], { type: "image/svg+xml" }));
        setSrc(url);
      })
      .catch(() => !cancelled && setFailed(true));
    return () => {
      cancelled = true;
      if (url) URL.revokeObjectURL(url);
    };
  }, [base, text, label]);
  if (failed) return <span className="small muted">The QR code couldn't be drawn; type the link instead.</span>;
  if (!src) return <span className="small muted">Drawing the QR code…</span>;
  return <img src={src} alt={label} width={180} height={180} style={{ display: "block", margin: "8px 0", background: "#fff" }} />;
}
