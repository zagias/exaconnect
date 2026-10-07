/*
 * A small Verto client for the browser phone (ADR 0033): JSON-RPC over a
 * secure WebSocket to FreeSWITCH's mod_verto, audio through the browser's
 * WebRTC. Only what a staff phone needs: sign in, call out, answer, hang up,
 * keypad tones and mute. No library from a CDN.
 */

export type CallState = "idle" | "calling" | "ringing" | "incoming" | "active" | "ended";

export interface VertoEvents {
  onStatus: (connected: boolean, message: string) => void;
  onCall: (state: CallState, detail: { number?: string; name?: string; reason?: string }) => void;
  onRemoteStream: (stream: MediaStream) => void;
}

interface Pending {
  resolve: (v: Record<string, unknown>) => void;
  reject: (e: Error) => void;
}

function uuid(): string {
  return crypto.randomUUID();
}

function waitForIce(pc: RTCPeerConnection, ms = 3000): Promise<void> {
  if (pc.iceGatheringState === "complete") return Promise.resolve();
  return new Promise((resolve) => {
    const done = () => {
      pc.removeEventListener("icegatheringstatechange", check);
      resolve();
    };
    const check = () => pc.iceGatheringState === "complete" && done();
    pc.addEventListener("icegatheringstatechange", check);
    setTimeout(done, ms); // send what we have: host candidates are enough on most networks
  });
}

export class VertoPhone {
  private ws: WebSocket | null = null;
  private seq = 1;
  private pending = new Map<number, Pending>();
  private sessid = uuid();
  private pc: RTCPeerConnection | null = null;
  private local: MediaStream | null = null;
  private callID: string | null = null;
  private incomingSdp: string | null = null;
  private incomingParams: Record<string, unknown> = {};

  constructor(
    private url: string,
    private login: string,
    private password: string,
    private me: { name: string; extension: string },
    private ev: VertoEvents,
  ) {}

  connect(): void {
    if (!this.url.startsWith("wss://")) {
      this.ev.onStatus(false, "The browser phone needs a secure (wss://) address.");
      return;
    }
    const ws = new WebSocket(this.url);
    this.ws = ws;
    ws.onopen = () => {
      this.rpc("login", { login: this.login, passwd: this.password, sessid: this.sessid })
        .then(() => this.ev.onStatus(true, `Signed in as extension ${this.me.extension}`))
        .catch((e: Error) => this.ev.onStatus(false, `Sign-in refused: ${e.message}`));
    };
    ws.onclose = () => {
      this.ev.onStatus(false, "Disconnected");
      this.cleanup("ended", "The connection closed.");
    };
    ws.onerror = () => this.ev.onStatus(false, "The phone service couldn't be reached.");
    ws.onmessage = (m) => this.onMessage(String(m.data));
  }

  disconnect(): void {
    this.hangup();
    this.ws?.close();
    this.ws = null;
  }

  private send(obj: Record<string, unknown>): void {
    this.ws?.send(JSON.stringify({ jsonrpc: "2.0", ...obj }));
  }

  private rpc(method: string, params: Record<string, unknown>): Promise<Record<string, unknown>> {
    const id = this.seq++;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.send({ method, params, id });
      setTimeout(() => {
        if (this.pending.delete(id)) reject(new Error("no answer"));
      }, 10_000);
    });
  }

  private onMessage(raw: string): void {
    let msg: { id?: number; method?: string; params?: Record<string, unknown>; result?: Record<string, unknown>; error?: { message?: string } };
    try {
      msg = JSON.parse(raw);
    } catch {
      return;
    }
    if (msg.id !== undefined && !msg.method) {
      const p = this.pending.get(msg.id);
      if (!p) return;
      this.pending.delete(msg.id);
      if (msg.error) p.reject(new Error(msg.error.message || "refused"));
      else p.resolve(msg.result || {});
      return;
    }
    const params = msg.params || {};
    if (msg.id !== undefined) this.send({ id: msg.id, result: { method: msg.method } });
    switch (msg.method) {
      case "verto.media":
      case "verto.answer":
        if (params.sdp && this.pc) void this.pc.setRemoteDescription({ type: "answer", sdp: String(params.sdp) });
        if (msg.method === "verto.answer") this.ev.onCall("active", {});
        else this.ev.onCall("ringing", {});
        break;
      case "verto.invite": {
        this.callID = String(params.callID || "");
        this.incomingSdp = String(params.sdp || "");
        this.incomingParams = params;
        this.ev.onCall("incoming", {
          number: String(params.caller_id_number || ""),
          name: String(params.caller_id_name || ""),
        });
        break;
      }
      case "verto.bye":
        this.cleanup("ended", String(params.cause || "Call ended"));
        break;
      default:
        break;
    }
  }

  private async media(): Promise<RTCPeerConnection> {
    this.local = await navigator.mediaDevices.getUserMedia({ audio: true, video: false });
    const pc = new RTCPeerConnection({ iceServers: [] });
    this.local.getTracks().forEach((t) => pc.addTrack(t, this.local!));
    pc.ontrack = (e) => this.ev.onRemoteStream(e.streams[0]);
    this.pc = pc;
    return pc;
  }

  private dialog(extra: Record<string, unknown> = {}): Record<string, unknown> {
    return {
      callID: this.callID,
      caller_id_name: this.me.name,
      caller_id_number: this.me.extension,
      useVideo: false,
      useStereo: false,
      screenShare: false,
      ...extra,
    };
  }

  async call(number: string): Promise<void> {
    if (this.pc) throw new Error("Hang up the current call first.");
    const dest = number.replace(/[^\d+*#]/g, "");
    if (!dest) throw new Error("Enter an extension or a number.");
    this.callID = uuid();
    this.ev.onCall("calling", { number: dest });
    try {
      const pc = await this.media();
      await pc.setLocalDescription(await pc.createOffer());
      await waitForIce(pc);
      await this.rpc("verto.invite", {
        sdp: pc.localDescription?.sdp,
        dialogParams: this.dialog({ destination_number: dest, remote_caller_id_number: dest }),
        sessid: this.sessid,
      });
    } catch (e) {
      this.cleanup("ended", (e as Error).message);
      throw e;
    }
  }

  async answer(): Promise<void> {
    if (!this.callID || !this.incomingSdp) return;
    const pc = await this.media();
    await pc.setRemoteDescription({ type: "offer", sdp: this.incomingSdp });
    await pc.setLocalDescription(await pc.createAnswer());
    await waitForIce(pc);
    await this.rpc("verto.answer", {
      sdp: pc.localDescription?.sdp,
      dialogParams: this.dialog({
        destination_number: this.incomingParams.callee_id_number ?? this.me.extension,
        remote_caller_id_number: this.incomingParams.caller_id_number,
      }),
      sessid: this.sessid,
    });
    this.ev.onCall("active", {});
  }

  hangup(): void {
    if (this.callID && this.ws?.readyState === WebSocket.OPEN) {
      void this.rpc("verto.bye", { dialogParams: this.dialog(), sessid: this.sessid }).catch(() => undefined);
    }
    this.cleanup("ended", "You hung up.");
  }

  dtmf(digit: string): void {
    if (!this.callID || !/^[0-9*#]$/.test(digit)) return;
    void this.rpc("verto.info", { dialogParams: this.dialog(), dtmf: digit, sessid: this.sessid }).catch(() => undefined);
  }

  mute(on: boolean): void {
    this.local?.getAudioTracks().forEach((t) => (t.enabled = !on));
  }

  private cleanup(state: CallState, reason: string): void {
    const had = !!this.callID || !!this.pc;
    this.pc?.close();
    this.pc = null;
    this.local?.getTracks().forEach((t) => t.stop());
    this.local = null;
    this.callID = null;
    this.incomingSdp = null;
    if (had) this.ev.onCall(state, { reason });
  }
}
