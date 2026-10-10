# ADR 0042: Jibsy Phone, an installable app on its own address

Date: 10 October 2026. Status: accepted.

## Context

The browser phone lived inside the portal (Jibsy > Phone > Browser phone). Dudley
asked for a phone app people can be sent to directly, on Mac, Windows, iOS and
Android, with a settings screen for their own preferences, without going into
the portal. He pointed `jibsyapp.exacarib.com` at the live server.

Options: native apps (four code bases, store accounts and review), a wrapper such
as Capacitor or Electron (still store accounts and signing), or a progressive web
app (one code base, installed from the browser, no store).

## Decision

- **A progressive web app**, built from the portal's code base as a second Vite
  page (`portal/phone.html`, `portal/src/phone/`). It shares sign-in, the API
  client, the brand and the Verto client (`pages/commai/voice/verto.ts`), so a
  fix to calling reaches the portal and the app at once.
- **Its own address**: Caddy serves the same site on `EXA_PHONE_HOST`
  (default `jibsyapp.exacarib.com`) and sends `/` there to `/phone`. The app
  always lives at `/phone`, so it also works at `connect.exacarib.com/phone`.
  Its manifest (`/phone.webmanifest`) and service worker (`/phone-sw.js`) are
  scoped to `/phone`, so installing it never installs the portal.
- **Same-host calling**: the app connects to `wss://<its own host>/verto`,
  whichever address it was opened on, because the page's security policy allows
  only its own host.
- **Screens**: keypad (also typed from a computer keyboard), contacts (people
  and shared lines in the business, names and extensions only, from
  `GET /voice/webphone/contacts`), recents (kept on the device, per extension),
  settings. Settings that follow the person (do not disturb, forwarding,
  voicemail greeting, voicemail by email) are saved to the phone system;
  settings for this device (ringtone, volume, notifications, microphone,
  speaker, appearance, connect on open) stay on the device.
- **Offline**: the service worker keeps the app shell, so the app opens without
  a network and says it is offline, then carries on when the network returns.
  It never caches the API or the call connection.

## Consequences

- No store accounts, fees or reviews; an update reaches everyone on their next
  open.
- iOS and iPadOS: the app rings only while it is open (there is no background
  push for a web app's calls). Android, Windows and Mac show a notification when
  a call comes in while the app is in the background but still running.
- The sign-in session lasts as long as a portal session (12 hours), then the app
  asks to sign in again.
- A native shell (for example Flutter with a SIP library) remains the route to
  ringing a locked phone, if customers need it.
