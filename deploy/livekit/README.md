# LiveKit: AI on phone calls

ADR 0027. This is self-hosted LiveKit, run under the compose profile `voice`
(not started by default):

```
docker compose --profile voice up -d livekit-redis livekit livekit-sip livekit-agent
```

How a phone call reaches the AI agent: carrier → Kamailio → FreeSWITCH (an AI
rule transfers to the `exacarib_ai` gateway) → LiveKit SIP → a room
`call-<id>`. The agent worker (`agent/agent.py`) joins that room. It turns
speech into text and asks the controller what to say
(`/api/v1/commai/voice/livekit/calls...`, with `EXA_LIVEKIT_AGENT_SECRET`), then
speaks the reply. The call is an inbox conversation, so staff can take over. If
LiveKit or the agent is down, FreeSWITCH's fallback (voicemail, ring group or
queue) answers. Basic calling never depends on it.

## Settings (environment or a git-ignored `.env`, never in the repo)

| Name | Used by |
| --- | --- |
| `LIVEKIT_KEYS` (`"<key>: <secret>"`) | the LiveKit server |
| `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` | LiveKit SIP and the agent |
| `EXA_LIVEKIT_URL`, `EXA_LIVEKIT_API_KEY`, `EXA_LIVEKIT_API_SECRET` | the controller (room tokens) |
| `EXA_LIVEKIT_AGENT_SECRET` | the controller and the agent (a random string you make) |

## What Dudley must provide

- The speech providers for the agent (speech to text and text to speech). None
  is chosen. Each costs money, so this needs your yes first.
- A public IP for WebRTC media (7881/tcp, 50000–60000/udp) if staff will join
  AI calls from a browser. LiveKit SIP (5062) stays on the internal network.

Not run here. Check that the image tags (`deploy/docker-compose.yml`) have arm64
builds before first use.
