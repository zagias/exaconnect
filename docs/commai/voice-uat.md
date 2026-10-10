# Phone system: acceptance test

How the phone system is tested end to end, as a person uses it. Two layers:

1. **Every deploy** (lab run, `lab/ci/checks/voice-sbc.sh`): `deploy/voice/call_check.py`
   signs in two browser phones over Verto, calls one from the other, answers and hangs
   up. It uses the admin's test phone and a "Lab call check" extension in the demo
   business (added once). No browser and no audio; it catches a refused call, a phone
   that doesn't ring, a hang-up that doesn't reach the other side, a call that rings
   again after hang-up, and a new extension that FreeSWITCH doesn't see.
2. **Before telling anyone to try a phone feature**: the browser test below, in real
   Chromium against the real portal, with audio.

## Browser test

`deploy/voice/uat/browser-call.mjs` (Playwright). Two people sign in to the portal in
separate browser sessions with a fake microphone (a tone), open Phone, Browser phone,
and sign in to it.

| # | Step | Pass when |
| --- | --- | --- |
| 1 | Both sign in to the browser phone | Both show "Signed in as extension N" |
| 2 | A calls B | B shows "Incoming call" with A's name and extension |
| 3 | B answers | Both show "On a call" |
| 4 | Audio, 6 seconds | Each side receives over 100 audio packets with sound in them (WebRTC stats) |
| 5 | A mutes, unmutes | The button switches between Mute and Unmute |
| 6 | A hangs up | Both show "Call ended", and nothing rings again |
| 7 | B calls A, A declines | B reaches A's voicemail and hears the greeting |
| 8 | A calls 299 (no such extension) | "That extension or number doesn't exist." |
| 9 | A calls *97 | Voicemail answers and speaks |
| 10 | A calls a menu (optional) | The greeting is spoken; pressing 1 rings B; audio after B answers |
| 11 | A calls a queue with B in it (optional) | Hold music plays; B rings; the call connects |

Set up once: a second portal account in the business (Account, People, invite; open the
link in another browser) and a phone extension for it (Phone, People and numbers, Add a
person, with that portal account's email). For steps 10 and 11, a menu whose option 1 goes
to B and a queue with B as its member (Phone, Call routing).

Run, from `deploy/voice/uat` (`npm install` once; passwords from the environment only):

```
UAT_BASE=https://connect.exacarib.com \
A_EMAIL=... A_PW=... A_EXT=200 B_EMAIL=... B_PW=... B_EXT=201 \
MENU_EXT=500 QUEUE_EXT=600 node browser-call.mjs
```

Screenshots of each step go to `uat-shots/`. It exits non-zero if any step fails.

What it does not cover: a real microphone and speaker, a desk phone or softphone app
over SIP, and calls to or from outside numbers (those need a phone carrier).

## First run, 10 October 2026

On a local copy of the full stack (portal behind Caddy, controller, FreeSWITCH):
18 of 18 steps pass after these fixes, all found by this test:

- The browser phone was refused every call ("Invalid Method, Missing Method or
  Permission Denied"): the directory did not allow Verto calls.
- Pressing Hang up dialled the number again.
- People, menus and queues added in the portal reached FreeSWITCH only on the next
  deploy; a watcher in the container (`deploy/freeswitch/start.sh`) now reloads within
  seconds.
- FreeSWITCH had no recorded prompts or hold music, so voicemail hung up straight
  away (`deploy/voice/sounds.sh` installs the standard packs, checksums pinned).
- Call queues (mod_callcenter) and spoken menu greetings (mod_flite) were not loaded.
