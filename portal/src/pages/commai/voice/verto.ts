/*
 * A small Verto client for the browser phone (ADR 0039): JSON-RPC over a
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
  /** The microphone to use (a deviceId from enumerateDevices); the default when unset. */
  micId = "";

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
        this.cleanup("ended", plainReason(String(params.cause || "")));
        break;
      default:
        break;
    }
  }

  private async media(): Promise<RTCPeerConnection> {
    try {
      this.local = await navigator.mediaDevices.getUserMedia({
        audio: this.micId ? { deviceId: { exact: this.micId } } : true,
        video: false,
      });
    } catch (e) {
      throw new Error(micProblem(e as DOMException));
    }
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
      const reason = plainReason((e as Error).message);
      this.cleanup("ended", reason);
      throw new Error(reason || "The call couldn't be connected.");
    }
  }

  async answer(): Promise<void> {
    if (!this.callID || !this.incomingSdp) return;
    try {
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
    } catch (e) {
      // Couldn't answer (no microphone, say): let the caller go on to voicemail, and say why.
      const reason = plainReason((e as Error).message) || "The call couldn't be answered.";
      if (this.ws?.readyState === WebSocket.OPEN)
        void this.rpc("verto.bye", { dialogParams: this.dialog(), sessid: this.sessid }).catch(() => undefined);
      this.cleanup("ended", reason);
      throw new Error(reason);
    }
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

/* FreeSWITCH hangup causes and Verto refusals, in words a caller understands ("" for a normal end). */
const REASONS: [RegExp, string][] = [
  [/UNALLOCATED_NUMBER|NO_ROUTE_DESTINATION|INVALID_NUMBER_FORMAT/, "That extension or number doesn't exist."],
  [/USER_NOT_REGISTERED|SUBSCRIBER_ABSENT/, "That person isn't signed in to a phone right now."],
  [/USER_BUSY/, "That line is busy."],
  [/NO_ANSWER|NO_USER_RESPONSE|ALLOTTED_TIMEOUT/, "No answer."],
  [/CALL_REJECTED|OUTGOING_CALL_BARRED/, "Your company's call rules don't allow that call."],
  [/NORMAL_CLEARING|ORIGINATOR_CANCEL/, ""],
  [/Invalid Method|Permission Denied/i, "The phone system refused the call. Sign out and back in; if it keeps happening, tell your voice admin."],
  [/NETWORK_OUT_OF_ORDER|DESTINATION_OUT_OF_ORDER|GATEWAY_DOWN|NORMAL_TEMPORARY_FAILURE/, "Outside calls aren't connected yet."],
];

/** What to tell someone whose microphone the browser wouldn't give us. */
export function micProblem(e: { name?: string }): string {
  if (e?.name === "NotAllowedError" || e?.name === "SecurityError")
    return "Calls need your microphone. Allow it for this site in your browser or device settings, then try again.";
  if (e?.name === "NotFoundError" || e?.name === "OverconstrainedError")
    return "No microphone found. Plug one in or choose another under Settings, then try again.";
  if (e?.name === "NotReadableError") return "Another app is using your microphone. Close it, then try again.";
  return "Your microphone couldn't be started.";
}

export function plainReason(raw: string): string {
  if (!raw) return "";
  for (const [re, words] of REASONS) if (re.test(raw)) return words;
  return /^[A-Z_]+$/.test(raw) ? "The call couldn't be connected." : raw;
}
