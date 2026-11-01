"""Measure configured speech and transfer one existing SIP test participant.

This command never originates a call. An operator must prepare the isolated
SIP participant and its human/PBX fallback first. Speech can be metered by the
configured provider: run it only after the operator has authorised that use.
A test-mode controller records lab evidence and cannot enable production.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import re
import time
import uuid

import httpx
from runtime import Controller, WorkerError

from agent import SpeechConfig, speech


async def loopback(
    controller: Controller, config: SpeechConfig, worker_id: str
) -> dict:
    identity = {"worker_id": worker_id, "provider_fingerprint": config.fingerprint}
    challenge = await controller.request("POST", "/evidence", identity)
    stt, tts = speech(config)
    try:
        frames = []
        async with tts.synthesize(challenge["phrase"]) as stream:
            async for event in stream:
                frames.append(event.frame)
                if len(frames) > 2000:
                    raise WorkerError("Speech check exceeded the bounded audio size")
        if not frames:
            raise WorkerError("Speech synthesis returned no audio")
        event = await stt.recognize(buffer=frames)
        transcript = event.alternatives[0].text if event.alternatives else ""
        await controller.request(
            "POST",
            f"/evidence/{challenge['id']}",
            {**identity, "transcript": transcript},
        )
        return challenge
    finally:
        for provider in (stt, tts):
            with contextlib.suppress(Exception):
                await asyncio.wait_for(provider.aclose(), 5)


async def media_check(
    controller: Controller, config: SpeechConfig, worker_id: str, args, challenge: dict
) -> None:
    """Use the real room audio pipeline: the SIP caller repeats the phrase.

    An isolated SIP echo endpoint is suitable for lab evidence. Production
    acceptance needs a prepared test caller and cannot be inferred from text.
    """
    from livekit import api, rtc
    from livekit.agents import Agent, AgentSession, APIConnectOptions, room_io
    from livekit.agents.voice.agent_session import SessionConnectOptions
    from livekit.plugins import silero

    stt, tts = speech(config)
    session = AgentSession(
        stt=stt,
        tts=tts,
        vad=silero.VAD.load(),
        llm=None,
        conn_options=SessionConnectOptions(
            stt_conn_options=APIConnectOptions(max_retry=0, timeout=10),
            tts_conn_options=APIConnectOptions(max_retry=0, timeout=10),
            max_unrecoverable_errors=0,
        ),
    )
    room = rtc.Room()
    heard = asyncio.get_running_loop().create_future()

    @session.on("user_input_transcribed")
    def transcribed(event):
        words = " ".join(re.findall(r"[a-z]+", event.transcript.lower()))
        if event.is_final and words == challenge["phrase"] and not heard.done():
            heard.set_result(event.transcript)

    @session.on("error")
    def failed(_):
        if not heard.done():
            heard.set_exception(WorkerError("The SIP audio challenge failed"))

    token = api.AccessToken(
        os.environ.get("LIVEKIT_API_KEY"), os.environ.get("LIVEKIT_API_SECRET")
    )
    token = token.with_identity("phone-acceptance-" + uuid.uuid4().hex).with_grants(
        api.VideoGrants(
            room_join=True, room=args.room, can_publish=True, can_subscribe=True
        )
    )
    try:
        await asyncio.wait_for(
            room.connect(os.environ["LIVEKIT_URL"], token.to_jwt()), 10
        )
        caller = room.remote_participants.get(args.participant)
        if caller is None or caller.kind != rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
            raise WorkerError("The prepared SIP caller is not in the acceptance room")
        if caller.attributes.get("sip.callID") != args.sip_call_id:
            raise WorkerError("The prepared SIP call identifier does not match")
        await asyncio.wait_for(
            session.start(
                room=room,
                agent=Agent(instructions="Audio acceptance only", llm=None),
                record=False,
                room_options=room_io.RoomOptions(
                    participant_identity=args.participant,
                    text_input=False,
                    participant_kinds=[rtc.ParticipantKind.PARTICIPANT_KIND_SIP],
                ),
            ),
            10,
        )
        print(
            "SIP audio check: repeat the six spoken words after playback.", flush=True
        )
        await asyncio.wait_for(
            session.say(challenge["phrase"], allow_interruptions=True), 15
        )
        transcript = await asyncio.wait_for(heard, 25)
        await controller.request(
            "POST",
            f"/evidence/{challenge['id']}/media",
            {
                "worker_id": worker_id,
                "provider_fingerprint": config.fingerprint,
                "transcript": transcript,
                "room": args.room,
                "participant_identity": args.participant,
                "sip_call_id": args.sip_call_id,
            },
        )
    finally:
        for component, method in (
            (session, "aclose"),
            (room, "disconnect"),
            (stt, "aclose"),
            (tts, "aclose"),
        ):
            with contextlib.suppress(Exception):
                await asyncio.wait_for(getattr(component, method)(), 5)


async def run(args) -> dict:
    if not args.speech_only and not all(
        (args.room, args.participant, args.sip_call_id)
    ):
        raise WorkerError("Supply the existing SIP test room, participant and call ID")
    config = SpeechConfig.load()
    worker_id = os.environ.get("EXA_PHONE_WORKER_ID", "exacarib-phone")
    async with httpx.AsyncClient(follow_redirects=False) as http:
        controller = Controller(
            http,
            os.environ.get("EXA_CONTROLLER_URL", "http://controller:8000"),
            os.environ.get("EXA_LIVEKIT_AGENT_SECRET", ""),
        )
        challenge = await asyncio.wait_for(loopback(controller, config, worker_id), 45)
        if args.speech_only:
            result = {"speech_loopback": "verified", "transfer": "not_attempted"}
        else:
            await asyncio.wait_for(
                media_check(controller, config, worker_id, args, challenge), 50
            )
            await controller.start(
                {
                    "room": args.room,
                    "participant_identity": args.participant,
                    "sip_call_id": args.sip_call_id,
                    "worker_id": worker_id,
                    "provider_fingerprint": config.fingerprint,
                    "acceptance_evidence_id": challenge["id"],
                }
            )
            try:
                transfer = await controller.handover()
                result = {
                    "speech_loopback": "verified",
                    "sip_audio": "verified",
                    "transfer": transfer["state"],
                    "human_answered": transfer["human_answered"],
                    "scope": transfer["evidence_scope"],
                }
            finally:
                await controller.end("acceptance_complete")
        print(json.dumps(result), flush=True)  # No identifiers, audio or credentials.
        if args.watch:
            # Fresh measured speech renews a short lease, only while the
            # controller's real transfer evidence is less than 24 hours old.
            started = time.monotonic()
            while time.monotonic() - started < 24 * 60 * 60:
                await asyncio.sleep(45)
                await asyncio.wait_for(loopback(controller, config, worker_id), 45)
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--room")
    parser.add_argument("--participant")
    parser.add_argument("--sip-call-id")
    parser.add_argument("--speech-only", action="store_true")
    parser.add_argument("--watch", action="store_true")
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except (WorkerError, TimeoutError):
        raise SystemExit(
            "Phone acceptance was not confirmed. Readiness remains unavailable or expires."
        ) from None


if __name__ == "__main__":
    main()
