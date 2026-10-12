# 0045 Jibsy Phone rings when it's closed (Web Push)

Date: 2026-10-11
Status: accepted

## Context

Jibsy Phone (ADR 0042) rang only while the app was open and connected. On an
iPhone or a locked Android phone, or with the app closed on a laptop, calls went
to voicemail. Dudley asked for the phone to show a call on a locked screen
without a second number. He chose the free route (no app store accounts):
standard Web Push, which Chrome, Edge, Firefox and Safari support. On iPhone and
iPad it needs iOS 16.4 or later and works only once the app is on the Home Screen.

## Decision

1. **Each device turns it on in Settings** ("Ring this device when Jibsy Phone is
   closed"). The browser makes a push subscription (its push service's address and
   two keys), and the controller keeps it per extension (`voice_push_subscriptions`).
   A test button sends that person's devices a notification.
2. **The phone system wakes the app before an extension rings.** Each extension's
   dial plan runs `deploy/freeswitch/scripts/exa_ring.lua` just before the bridge.
   If the extension's app is not connected (no Verto registration), the script asks
   the controller (`GET /api/v1/commai/internal/voice/ring`, internal only, signed
   with the same keyed digest as the outside-call check). The controller sends each
   device a push message. If any device was reached, the caller hears ringing while
   the script waits up to 25 seconds for the app to connect, and then the extension
   rings as usual (app, desk phone, then voicemail). When no device has ringing
   turned on, nothing changes and nothing waits.
3. **The message is encrypted for the device and signed by ExaCarib** (RFC 8291
   aes128gcm, RFC 8292 VAPID), using `cryptography` only. The push service can't
   read it. The VAPID key is made on the server by `lab/scripts/init-env.sh` and
   kept only in its `.env` (`EXA_PUSH_VAPID_KEY`), because the repository is public.
4. **Only the browsers' own push services are contacted** (Google, Mozilla, Apple,
   Microsoft), so a subscription can't make the controller call any other address.
   The exception is `http://push-check:8099`, a name that exists on the compose
   network only while the live test runs.
5. **Tapping the notification answers.** The service worker shows "Incoming call"
   with the caller, which closes itself after 30 seconds. A tap opens
   `/phone?answer=…`. The app signs in from its saved session and connects, then
   answers the call that arrives within 40 seconds. If the app was open in the
   background, the tap answers there instead.
6. **Tested on live after every deploy** (`lab/ci/checks/voice-push.sh`,
   `deploy/voice/uat/push.mjs`). A test device turns ringing on and the app closes.
   B calls A. The test checks that the push message arrives encrypted and signed,
   and that A's real service worker shows the notification. Opening it must answer,
   with audio both ways. With the app open, no push is sent. A test browser has no
   Apple or Google push service, so a stand-in receives the message, and the test
   hands it to the service worker the way Chrome does.

## Consequences

- No cost, no store accounts and no new library.
- On iPhone and iPad, the person must add the app to the Home Screen and turn
  ringing on from there. iOS shows the notification on the lock screen, and a tap
  opens the app to answer. iOS does not show a full-screen call screen; only a
  native app (CallKit) can, which is the later Flutter app's job.
- The caller hears ringing a few seconds longer while a closed app opens.
- Ring groups and queues still ring only connected apps; direct calls to a
  person's extension (and internal forwarding to one) wake it.
