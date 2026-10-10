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

## The phone app (Jibsy Phone, ADR 0042)

`deploy/voice/uat/phone-app.mjs` tests the app at `/phone` the way people use it: A on a
phone-sized screen, B on a computer. It checks that the address opens the app, the manifest,
icons and service worker (and that Chrome would install it), sign-in and automatic
connection, contacts and search, a call from Contacts with audio both ways, the call timer,
mute, the in-call keypad and hang-up, recents (outgoing, incoming and missed), the message for
a missing extension, typing on a computer keyboard, declining to voicemail, do not disturb,
forwarding, the voicemail greeting, ringtone and appearance surviving a restart, install help,
losing and regaining the network, opening offline, a person with no extension, and no
sideways scrolling on a small phone, a tablet and a computer.

```
UAT_BASE=https://jibsyapp.exacarib.com \
A_EMAIL=... A_PW=... A_EXT=200 A_NAME=... B_EMAIL=... B_PW=... B_EXT=201 B_NAME=... \
C_EMAIL=... C_PW=... MENU_EXT=500 node phone-app.mjs
```

On the live server the lab runs it after every deploy (`lab/ci/checks/voice-phone-app.sh`), in
Microsoft's Playwright image, with three test accounts in the demo business (Phone app test A, B
and C) that `deploy/voice/uat/live_users.py` sets up and gives fresh random passwords each run.

`UAT_ENGINE=firefox` or `UAT_ENGINE=webkit` runs A in Firefox or in Safari's engine (B stays
in Chromium). Playwright's WebKit has no stand-in microphone, so in that run A instead checks
what someone sees who hasn't allowed the microphone.

First runs, 10 October 2026, on the local full stack: Chromium 39 of 39, Firefox 35 of 35,
WebKit 27 of 27, after fixing what they found:

- Do not disturb and voicemail-by-email switches only moved after the save came back; they
  now move at once and go back if the save fails.
- A message after a call (for example a missing extension) vanished after a third of a
  second; it now stays for seven seconds.
- With the microphone blocked, pressing Call or Answer did nothing visible; it now says how
  to allow the microphone, and an unanswerable call goes on to voicemail.
- Losing the network left the app showing it was connected; it now says it is offline at
  once and reconnects when the network returns.
- Opened with no network, the app asked the person to sign in again; it now says it is
  offline and carries on when the network is back.
- The app could not reach the phone system from its own address (the security policy
  allowed only the portal's); it now uses its own host.

