"""XaKore Jibsy telephone worker. No provider or paid call is enabled implicitly.

LiveKit owns media subscriptions, resampling, VAD and interruption. The same
controller conversation agent answers browser and telephone conversations.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import sys
from dataclasses import dataclass
from urllib.parse import urlencode, urlsplit

import httpx
from runtime import Controller, Limits, PhoneLifecycle, WorkerError

log = logging.getLogger("xakore.jibsy.phone")


@dataclass(frozen=True)
class SpeechConfig:
    stt_url: str
    stt_key: str
    stt_model: str
    tts_url: str
    tts_key: str
    tts_model: str
    voice: str
    language: str = "en"

    @classmethod
    def load(cls) -> SpeechConfig:
        names = (
            "STT_URL",
            "STT_KEY",
            "STT_MODEL",
            "TTS_URL",
            "TTS_KEY",
            "TTS_MODEL",
            "TTS_VOICE",
        )
        values = [os.environ.get(f"EXA_PHONE_{key}", "") for key in names]
        if not all(values):
            raise WorkerError("Explicit phone STT and TTS configuration is required")
        for url in (values[0], values[3]):
            parsed = urlsplit(url)
            if (
                parsed.scheme not in ("https", "http")
                or not parsed.hostname
                or parsed.username
                or parsed.password
            ):
                raise WorkerError("Invalid phone speech provider endpoint")
            if parsed.scheme == "http" and parsed.hostname not in (
                "localhost",
                "127.0.0.1",
                "::1",
                "speech-fixture",
            ):
                raise WorkerError(
                    "Phone speech endpoints must use HTTPS (except isolated loopback fixtures)"
                )
        return cls(*values, os.environ.get("EXA_PHONE_LANGUAGE", "en"))

    @property
    def fingerprint(self) -> str:
        # Keys never appear in status, fingerprints or logs.
        public = (
            self.stt_url,
            self.stt_model,
            self.tts_url,
            self.tts_model,
            self.voice,
            self.language,
        )
        return hashlib.sha256(json.dumps(public).encode()).hexdigest()


def speech(config: SpeechConfig):
    from livekit.plugins import openai

    return (
        openai.STT(
            base_url=config.stt_url,
            api_key=config.stt_key,
            model=config.stt_model,
            language=config.language,
        ),
        openai.TTS(
            base_url=config.tts_url,
            api_key=config.tts_key,
            model=config.tts_model,
            voice=config.voice,
        ),
    )


class LiveKitAudio:
    def __init__(self, session, ctx, caller):
        self.session, self.ctx, self.caller = session, ctx, caller

    async def say(self, text: str) -> None:
        await self.session.say(text, allow_interruptions=True)

    def interrupt(self) -> None:
        with contextlib.suppress(
            RuntimeError
        ):  # A startup failure may have no active media pipeline.
            self.session.interrupt()

    async def hangup(self) -> None:
        from livekit import api

        await self.ctx.api.room.remove_participant(
            api.RoomParticipantIdentity(
                room=self.ctx.room.name, identity=self.caller.identity
            )
        )

    async def close(self) -> None:
        await self.session.aclose()


async def entrypoint(ctx) -> None:
    config = SpeechConfig.load()
    from livekit import rtc
    from livekit.agents import (
        Agent,
        AgentSession,
        APIConnectOptions,
        AutoSubscribe,
        room_io,
    )
    from livekit.agents.voice.agent_session import SessionConnectOptions
    from livekit.plugins import silero

    stt, tts = speech(config)
    controller_url = os.environ.get("EXA_CONTROLLER_URL", "http://controller:8000")
    secret = os.environ.get("EXA_LIVEKIT_AGENT_SECRET", "")
    worker_id = os.environ.get("EXA_PHONE_WORKER_ID", "exacarib-phone")
    session = AgentSession(
        stt=stt,
        tts=tts,
        vad=silero.VAD.load(),
        llm=None,
        allow_interruptions=True,
        min_interruption_duration=0.3,
        min_endpointing_delay=0.4,
        max_endpointing_delay=2.0,
        conn_options=SessionConnectOptions(
            stt_conn_options=APIConnectOptions(max_retry=0, timeout=12),
            tts_conn_options=APIConnectOptions(max_retry=0, timeout=12),
            max_unrecoverable_errors=0,
        ),
    )
    async with httpx.AsyncClient(
        follow_redirects=False, limits=httpx.Limits(max_connections=4)
    ) as http:
        controller = Controller(http, controller_url, secret)
        # Participant selection is explicit. Never attach a phone worker to a
        # staff browser or use the first arbitrary room participant.
        try:
            await asyncio.wait_for(
                ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY), 15
            )
            caller = await asyncio.wait_for(
                ctx.wait_for_participant(kind=rtc.ParticipantKind.PARTICIPANT_KIND_SIP),
                15,
            )
        except Exception:  # noqa: BLE001 - SDK errors can contain caller data or credentials.
            for component in (session, stt, tts):
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(component.aclose(), 8)
            raise WorkerError("The SIP caller could not be attached") from None
        attrs = caller.attributes or {}
        call_id = attrs.get("sip.callID", "")
        audio = LiveKitAudio(session, ctx, caller)
        if not call_id:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(audio.hangup(), 8)
            for component in (session, stt, tts):
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(component.aclose(), 8)
            raise WorkerError("The SIP participant has no call identifier")
        lifecycle = PhoneLifecycle(controller, audio)

        @ctx.room.on("participant_disconnected")
        def caller_left(participant):
            if participant.identity == caller.identity:
                lifecycle.stop("caller_disconnected")

        @ctx.room.on("disconnected")
        def room_left(*_):
            lifecycle.stop("room_disconnected")

        @session.on("user_input_transcribed")
        def transcribed(event):
            lifecycle.speech_activity()
            if event.is_final:
                lifecycle.transcript(event.transcript)

        @session.on("user_state_changed")
        def user_state(event):
            if event.new_state == "speaking":
                lifecycle.speech_activity()

        @session.on("error")
        def speech_failed(_):
            lifecycle.stop("provider_failure")

        @session.on("close")
        def session_closed(_):
            lifecycle.stop("session_closed")

        async def shutdown():
            lifecycle.stop("worker_shutdown")

        ctx.add_shutdown_callback(shutdown)
        try:
            call = await controller.start(
                {
                    "room": ctx.room.name,
                    "participant_identity": caller.identity,
                    "sip_call_id": call_id,
                    "worker_id": worker_id,
                    "provider_fingerprint": config.fingerprint,
                }
            )
            lifecycle.limits = Limits(
                duration_s=min(1800, float(call["max_duration_s"]))
            )
            await asyncio.wait_for(
                session.start(
                    room=ctx.room,
                    agent=Agent(
                        instructions="Use only the controller's spoken replies.",
                        llm=None,
                    ),
                    room_options=room_io.RoomOptions(
                        participant_identity=caller.identity,
                        participant_kinds=[rtc.ParticipantKind.PARTICIPANT_KIND_SIP],
                        text_input=False,
                        close_on_disconnect=False,
                    ),
                    record=False,
                ),
                15,
            )
            result = await lifecycle.run(call["greeting"])
            log.info("Phone worker finished: %s", result["reason"])
        except Exception:  # noqa: BLE001 - SDK errors can contain caller data or credentials.
            # No exception body is logged: SDK/network errors can include URLs,
            # tokens, caller metadata or provider text. Keep this path bounded.
            log.warning("Phone worker could not start")
            if controller.call:
                lifecycle.stop("startup_failure")
                await lifecycle.run("")
            else:
                try:
                    await asyncio.wait_for(audio.hangup(), 8)
                finally:
                    await asyncio.wait_for(session.aclose(), 8)
        finally:
            for provider in (stt, tts):
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(provider.aclose(), 8)


async def request_job(request) -> None:
    """Do not accept caller jobs merely because the worker has credentials."""
    try:
        config = SpeechConfig.load()
        async with httpx.AsyncClient(follow_redirects=False) as http:
            controller = Controller(
                http,
                os.environ.get("EXA_CONTROLLER_URL", "http://controller:8000"),
                os.environ.get("EXA_LIVEKIT_AGENT_SECRET", ""),
            )
            query = urlencode(
                {
                    "worker_id": os.environ.get(
                        "EXA_PHONE_WORKER_ID", "exacarib-phone"
                    ),
                    "provider_fingerprint": config.fingerprint,
                }
            )
            result = await asyncio.wait_for(
                controller.request("GET", "/readiness?" + query), 7
            )
            if result.get("ready") is True:
                await request.accept()
                return
    except (WorkerError, TimeoutError):
        pass
    await request.reject()


def main() -> None:
    if "download-files" not in sys.argv:
        SpeechConfig.load()
        if not os.environ.get("EXA_LIVEKIT_AGENT_SECRET"):
            raise WorkerError("Missing controller worker credential")
    from livekit.agents import WorkerOptions, cli
    from livekit.plugins import openai, silero  # noqa: F401 (register download-files plugins)

    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            request_fnc=request_job,
            agent_name="exacarib-phone",
        )
    )


if __name__ == "__main__":
    main()
