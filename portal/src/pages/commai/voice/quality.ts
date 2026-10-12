/*
 * Call quality as the browser hears it, for the browser phone and Jibsy Phone.
 * During an answered call the meter reads the WebRTC statistics every 2 s and,
 * when the call ends, sums them up: what reached us (loss, jitter, how much
 * sound the browser had to make up), what the phone system heard from us, and
 * the round trip to it. The summary goes to the controller so a bad call can
 * be traced to the network it was on (docs/commai/voice-uat.md, call quality).
 * Numbers only: no audio, no addresses.
 */

import { api } from "../../../api";

export interface CallQuality {
  call_id: string;
  direction: "in" | "out";
  secs: number;
  codec: string;
  /** Packets that reached us, and those the browser counted as lost on the way. */
  rx_packets: number;
  rx_lost: number;
  /** Share of the sound the browser had to make up (lost, late or missing audio). */
  concealed_pct: number;
  /** 2 s stretches with more than 5% made-up sound: what a person hears as breaking up. */
  bad_stretches: number;
  jitter_ms: number;
  jitter_max_ms: number;
  jitter_buffer_ms: number;
  rtt_ms: number | null;
  rtt_max_ms: number | null;
  /** What the phone system heard from us (its reports back). */
  up_lost_pct: number | null;
  up_jitter_ms: number | null;
  /** How the connection was made: host, srflx, prflx or relay, over udp or tcp. */
  path: string;
  network: string;
}

interface Snap {
  t: number;
  rx: number;
  lost: number;
  conc: number;
  total: number;
  jitter: number | null;
  jbd: number;
  jbe: number;
  rtt: number | null;
  sent: number;
  upLost: number | null;
  upJitter: number | null;
  codec: string;
  path: string;
}

async function snap(pc: RTCPeerConnection): Promise<Snap> {
  const r = await pc.getStats();
  const s: Snap = { t: performance.now(), rx: 0, lost: 0, conc: 0, total: 0, jitter: null, jbd: 0, jbe: 0, rtt: null, sent: 0, upLost: null, upJitter: null, codec: "", path: "" };
  const codecs: Record<string, string> = {};
  const cands: Record<string, string> = {};
  let pair: { local?: string; rtt?: number } = {};
  r.forEach((x) => {
    if (x.type === "codec") codecs[x.id] = String(x.mimeType || "").replace(/^audio\//, "");
    if (x.type === "local-candidate") cands[x.id] = `${x.candidateType}/${x.protocol}`;
    if (x.type === "candidate-pair" && x.nominated && x.state === "succeeded") pair = { local: x.localCandidateId, rtt: x.currentRoundTripTime };
  });
  r.forEach((x) => {
    if (x.kind !== "audio") return;
    if (x.type === "inbound-rtp") {
      Object.assign(s, {
        rx: x.packetsReceived || 0,
        lost: Math.max(0, x.packetsLost || 0),
        conc: x.concealedSamples || 0,
        total: x.totalSamplesReceived || 0,
        jitter: x.jitter ?? null,
        jbd: x.jitterBufferDelay || 0,
        jbe: x.jitterBufferEmittedCount || 0,
      });
      s.codec = codecs[x.codecId] || s.codec;
    }
    if (x.type === "outbound-rtp") s.sent = x.packetsSent || 0;
    if (x.type === "remote-inbound-rtp") {
      s.rtt = x.roundTripTime ?? null;
      s.upLost = x.packetsLost ?? null;
      s.upJitter = x.jitter ?? null;
    }
  });
  if (s.rtt === null && pair.rtt !== undefined) s.rtt = pair.rtt;
  s.path = (pair.local && cands[pair.local]) || "";
  return s;
}

const ms = (v: number | null) => (v === null ? null : Math.round(v * 1000));
const round = (v: number, d = 2) => Math.round(v * 10 ** d) / 10 ** d;

export class QualityMeter {
  private first: Snap | null = null;
  private last: Snap | null = null;
  private bad = 0;
  private jitters: number[] = [];
  private rtts: number[] = [];
  private timer: ReturnType<typeof setInterval>;
  private started = performance.now();

  constructor(
    private pc: RTCPeerConnection,
    private callId: string,
    private direction: "in" | "out",
  ) {
    void this.sample();
    this.timer = setInterval(() => void this.sample(), 2000);
  }

  private async sample(): Promise<void> {
    let s: Snap;
    try {
      s = await snap(this.pc);
    } catch {
      return; // closed
    }
    const p = this.last;
    // The first 2 s after answering are left out: the jitter buffer filling up there is not a fault.
    if (!this.first) {
      if (s.t - this.started >= 1900) this.first = s;
    } else if (p && s.total > p.total && (s.conc - p.conc) / (s.total - p.total) > 0.05) this.bad++;
    if (s.jitter !== null) this.jitters.push(s.jitter);
    if (s.rtt !== null) this.rtts.push(s.rtt);
    this.last = s;
  }

  /** Stops sampling; the summary of the call so far, or null if it was too short to say. */
  stop(): CallQuality | null {
    clearInterval(this.timer);
    const a = this.first;
    const b = this.last;
    if (!a || !b || b.t - a.t < 1500 || b.total <= a.total) return null;
    const avg = (v: number[]) => (v.length ? v.reduce((x, y) => x + y, 0) / v.length : null);
    const conn = (navigator as Navigator & { connection?: { type?: string; effectiveType?: string } }).connection;
    return {
      call_id: this.callId,
      direction: this.direction,
      secs: Math.round((b.t - a.t) / 1000),
      codec: b.codec,
      rx_packets: b.rx - a.rx,
      rx_lost: b.lost - a.lost,
      concealed_pct: round((100 * (b.conc - a.conc)) / (b.total - a.total)),
      bad_stretches: this.bad,
      jitter_ms: ms(avg(this.jitters)) ?? 0,
      jitter_max_ms: ms(this.jitters.length ? Math.max(...this.jitters) : null) ?? 0,
      jitter_buffer_ms: b.jbe > a.jbe ? Math.round((1000 * (b.jbd - a.jbd)) / (b.jbe - a.jbe)) : 0,
      rtt_ms: ms(avg(this.rtts)),
      rtt_max_ms: ms(this.rtts.length ? Math.max(...this.rtts) : null),
      up_lost_pct: b.upLost !== null && b.sent > a.sent ? round((100 * (b.upLost - (a.upLost || 0))) / (b.sent - a.sent)) : null,
      up_jitter_ms: ms(b.upJitter),
      path: b.path,
      network: [conn?.type, conn?.effectiveType].filter(Boolean).join(" "),
    };
  }
}

/** Sends a call's quality summary to the controller; never gets in the way of the phone. */
export function reportQuality(base: string, q: CallQuality): void {
  void api(`${base}/voice/webphone/quality`, { method: "POST", body: JSON.stringify(q), keepalive: true }).catch(() => undefined);
}
