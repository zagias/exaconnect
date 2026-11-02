"""Offline SDK compatibility smoke check. Does not connect to any provider."""

import asyncio
import inspect

from configure_sip import plan, requests_for, same_dispatch, same_trunk
from livekit import rtc
from livekit.agents import Agent, AgentSession, APIConnectOptions, JobContext, room_io
from livekit.agents.voice.agent_session import SessionConnectOptions
from livekit.plugins import silero

from agent import SpeechConfig, speech


async def check():
    item = plan(
        [
            {
                "customer_id": "11111111-1111-1111-1111-111111111111",
                "e164": "+18685550100",
            }
        ],
        "127.0.0.1",
    )[0]
    inbound, dispatch = requests_for(item, "offline-trunk")
    assert same_trunk(inbound.trunk, inbound.trunk)
    assert same_dispatch(dispatch.dispatch_rule, dispatch.dispatch_rule)
    assert "kind" in inspect.signature(JobContext.wait_for_participant).parameters
    assert rtc.ParticipantKind.PARTICIPANT_KIND_SIP is not None
    room_io.RoomOptions(
        participant_identity="offline-sip-participant", text_input=False
    )
    options = SessionConnectOptions(
        stt_conn_options=APIConnectOptions(max_retry=0, timeout=12)
    )
    config = SpeechConfig(
        "https://stt.invalid/v1",
        "offline-key",
        "whisper-1",
        "https://tts.invalid/v1",
        "offline-key",
        "tts-1",
        "alloy",
    )
    stt, tts = speech(config)
    session = AgentSession(
        stt=stt, tts=tts, vad=silero.VAD.load(), llm=None, conn_options=options
    )
    Agent(instructions="Offline compatibility check", llm=None)
    await session.aclose()
    await stt.aclose()
    await tts.aclose()
    print("Phone SDK compatibility: passed (no network/provider calls)")


if __name__ == "__main__":
    asyncio.run(check())
