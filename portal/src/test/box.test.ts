import { describe, expect, it } from "vitest";
import { boxSetupCode } from "../box";

describe("box setup code", () => {
  it("carries the four join values as base64url JSON after EXA1.", () => {
    const code = boxSetupCode({ controller: "https://connect.exacarib.com:8443", token: "tok_ü-123", ca_fingerprint: "ab".repeat(32), site: "kingston-hq" });
    expect(code.startsWith("EXA1.")).toBe(true);
    const body = code.slice(5);
    expect(body).toMatch(/^[A-Za-z0-9_-]+$/);
    const bin = atob(body.replace(/-/g, "+").replace(/_/g, "/"));
    const json = new TextDecoder().decode(Uint8Array.from(bin, (c) => c.charCodeAt(0)));
    expect(JSON.parse(json)).toEqual({ c: "https://connect.exacarib.com:8443", t: "tok_ü-123", f: "ab".repeat(32), n: "kingston-hq" });
  });
});
