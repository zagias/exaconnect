"""ExaCarib AI phone agent worker for LiveKit (ADR 0033).

It joins each phone call's LiveKit room, turns the caller's speech into text,
asks the ExaCarib controller what to say (the same AI agent, inbox rules and
hand-over as browser calls), and speaks the reply.

    POST {EXA_CONTROLLER_URL}/api/v1/commai/voice/livekit/calls          -> greeting, conversation
    POST .../calls/{customer_id}/{conversation_id}/turns  {"text": ...}  -> reply, handed_over
    POST .../calls/{customer_id}/{conversation_id}/end

Settings (environment only, never in the repo):
  LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET   the self-hosted LiveKit server
  EXA_CONTROLLER_URL                                 e.g. http://controller:8000
  EXA_LIVEKIT_AGENT_SECRET                           shared with the controller
  Speech: the STT and TTS plugins below need their own provider keys. None is
  chosen yet: this needs Dudley's yes to the spend (see ADR 0019).

Not run here. It uses the livekit-agents SDK (requirements.txt), installed only
in its own image.
"""

from __future__ import annotations

import asyncio
import logging
import os

import httpx
from livekit import agents, rtc
from livekit.agents import AutoSubscribe, JobContext, WorkerOptions, cli

log = logging.getLogger("exacarib.livekit.agent")

CONTROLLER = os.environ.get("EXA_CONTROLLER_URL", "http://controller:8000").rstrip("/")
BASE = f"{CONTROLLER}/api/v1/commai/voice/livekit/calls"


def _headers() -> dict:
    return {"Authorization": f"Bearer {os.environ['EXA_LIVEKIT_AGENT_SECRET']}"}


def speech():
    """The speech-to-text and text-to-speech plugins. Choose them when the spend is agreed."""
    raise RuntimeError("Choose the STT and TTS providers first (needs Dudley's yes to the spend).")


async def entrypoint(ctx: JobContext) -> None:
    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
    caller = await ctx.wait_for_participant()
    attrs = caller.attributes or {}
    dialled = attrs.get("sip.trunkPhoneNumber", "")
    async with httpx.AsyncClient(timeout=20) as http:
        r = await http.post(
            BASE, json={"dialled": dialled, "caller": attrs.get("sip.phoneNumber", "")}, headers=_headers()
        )
        r.raise_for_status()
        call = r.json()
        stt, tts = speech()
        source = rtc.AudioSource(16000, 1)
        track = rtc.LocalAudioTrack.create_audio_track("agent", source)
        await ctx.room.local_participant.publish_track(track)

        async def say(text: str) -> None:
            async for frame in tts.synthesize(text):
                await source.capture_frame(frame.frame)

        await say(call["greeting"])
        stream = stt.stream()
        audio = rtc.AudioStream(next(iter(caller.track_publications.values())).track)

        async def pump() -> None:
            async for ev in audio:
                stream.push_frame(ev.frame)

        feeder = asyncio.create_task(pump())
        try:
            async for event in stream:
                if event.type != agents.stt.SpeechEventType.FINAL_TRANSCRIPT:
                    continue
                text = event.alternatives[0].text.strip()
                if not text:
                    continue
                t = await http.post(
                    f"{BASE}/{call['customer_id']}/{call['conversation_id']}/turns",
                    json={"text": text},
                    headers=_headers(),
                )
                t.raise_for_status()
                out = t.json()
                if out["reply"]:
                    await say(out["reply"])
                if out["handed_over"]:
                    break  # staff take over in the inbox; FreeSWITCH's fallback handles the call
        finally:
            feeder.cancel()
            await http.post(f"{BASE}/{call['customer_id']}/{call['conversation_id']}/end", headers=_headers())


if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint, agent_name="exacarib-phone"))
