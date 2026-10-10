/* Ringtones made in the browser (Web Audio): no sound files to download or licence. */

export type Ringtone = "classic" | "soft" | "digital" | "off";

export const RINGTONES: { id: Ringtone; name: string }[] = [
  { id: "classic", name: "Classic (two rings)" },
  { id: "soft", name: "Soft chime" },
  { id: "digital", name: "Digital" },
  { id: "off", name: "Silent (screen only)" },
];

/** One ring cycle: [frequencies, start s, length s], repeating every `every` seconds. */
const PATTERNS: Record<Exclude<Ringtone, "off">, { every: number; notes: [number[], number, number][] }> = {
  classic: { every: 3, notes: [[[440, 480], 0, 0.4], [[440, 480], 0.6, 0.4]] },
  soft: { every: 2.6, notes: [[[659], 0, 0.35], [[523], 0.4, 0.5]] },
  digital: { every: 2, notes: [[[1200], 0, 0.08], [[1200], 0.15, 0.08], [[1200], 0.3, 0.08], [[1200], 0.45, 0.08]] },
};

export class Ringer {
  private ctx: AudioContext | null = null;
  private timer: ReturnType<typeof setInterval> | null = null;
  private stopPreview: ReturnType<typeof setTimeout> | null = null;

  /** Browsers allow sound only after a tap; any tap in the app unlocks it for later rings. */
  unlock(): void {
    try {
      this.ctx ??= new AudioContext();
      if (this.ctx.state === "suspended") void this.ctx.resume();
    } catch {
      /* no Web Audio: calls still show on screen */
    }
  }

  start(tone: Ringtone, volume: number): void {
    this.stop();
    if (tone === "off") return;
    this.unlock();
    const p = PATTERNS[tone];
    const once = () => this.cycle(tone, volume);
    once();
    this.timer = setInterval(once, p.every * 1000);
    if ("vibrate" in navigator) navigator.vibrate?.([400, 200, 400]);
  }

  stop(): void {
    if (this.timer) clearInterval(this.timer);
    if (this.stopPreview) clearTimeout(this.stopPreview);
    this.timer = null;
    this.stopPreview = null;
  }

  preview(tone: Ringtone, volume: number): void {
    this.start(tone, volume);
    this.stopPreview = setTimeout(() => this.stop(), 3500);
  }

  private cycle(tone: Exclude<Ringtone, "off">, volume: number): void {
    const ctx = this.ctx;
    if (!ctx) return;
    const t0 = ctx.currentTime + 0.02;
    for (const [freqs, at, len] of PATTERNS[tone].notes) {
      const gain = ctx.createGain();
      gain.gain.setValueAtTime(0, t0 + at);
      gain.gain.linearRampToValueAtTime(0.25 * volume, t0 + at + 0.02);
      gain.gain.setValueAtTime(0.25 * volume, t0 + at + len - 0.03);
      gain.gain.linearRampToValueAtTime(0, t0 + at + len);
      gain.connect(ctx.destination);
      for (const f of freqs) {
        const osc = ctx.createOscillator();
        osc.type = tone === "digital" ? "square" : "sine";
        osc.frequency.value = f;
        osc.connect(gain);
        osc.start(t0 + at);
        osc.stop(t0 + at + len);
      }
    }
  }
}
