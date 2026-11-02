"""Bounded phone lifecycle; LiveKit and speech providers are adapters.

There are no retries of billable/mutating controller requests. A timed out
turn has an unknown outcome, so the worker transfers instead of duplicating it.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

import httpx


class WorkerError(RuntimeError):
    """Safe error text only: never include HTTP bodies, headers or transcripts."""


class Audio(Protocol):
    async def say(self, text: str) -> None: ...
    def interrupt(self) -> None: ...
    async def hangup(self) -> None: ...
    async def close(self) -> None: ...


class Controller:
    def __init__(self, http: httpx.AsyncClient, base_url: str, secret: str):
        url = urlsplit(base_url)
        if (
            url.scheme not in ("http", "https")
            or not url.hostname
            or url.username
            or url.password
        ):
            raise WorkerError("Invalid controller endpoint")
        if not secret:
            raise WorkerError("Missing controller worker credential")
        self.http, self.base, self.secret = (
            http,
            base_url.rstrip("/") + "/api/v1/commai/voice/livekit",
            secret,
        )
        self.call: dict | None = None

    async def request(
        self, method: str, path: str, body: dict | None = None, *, scoped: bool = False
    ) -> dict:
        if scoped and self.call is None:
            raise WorkerError("No controller call capability")
        key = self.call["session_token"] if scoped and self.call else self.secret
        try:
            response = await self.http.request(
                method,
                self.base + path,
                json=body,
                headers={"Authorization": f"Bearer {key}"},
                timeout=50 if path.endswith("/handover") else 15,
            )
            response.raise_for_status()
            if len(response.content) > 65_536:
                raise ValueError("Oversized response")
            out = response.json()
            if not isinstance(out, dict):
                raise TypeError("Invalid response")
            return out
        except (httpx.HTTPError, ValueError, TypeError):
            raise WorkerError("Controller operation was not confirmed") from None

    def path(self, suffix: str = "") -> str:
        if self.call is None:
            raise WorkerError("No controller call session")
        return (
            f"/calls/{self.call['customer_id']}/{self.call['conversation_id']}{suffix}"
        )

    async def start(self, metadata: dict) -> dict:
        call = await self.request("POST", "/calls", metadata)
        required = (
            "session_token",
            "conversation_id",
            "customer_id",
            "greeting",
            "max_duration_s",
        )
        if any(k not in call for k in required):
            raise WorkerError("Incomplete controller call contract")
        self.call = call
        return self.call

    async def turn(self, text: str) -> dict:
        return await self.request(
            "POST",
            self.path("/turns"),
            {"text": text[:2000], "request_id": str(uuid.uuid4())},
            scoped=True,
        )

    async def state(self) -> dict:
        return await self.request("GET", self.path(), scoped=True)

    async def handover(self) -> dict:
        return await self.request("POST", self.path("/handover"), scoped=True)

    async def end(self, reason: str) -> dict:
        return await self.request(
            "POST", self.path("/end"), {"reason": reason}, scoped=True
        )


@dataclass(frozen=True)
class Limits:
    silence_s: float = 60
    duration_s: float = 1800
    speech_s: float = 30
    poll_s: float = 2
    transfer_s: float = 50
    cleanup_s: float = 8


class PhoneLifecycle:
    def __init__(
        self, controller: Controller, audio: Audio, limits: Limits | None = None
    ):
        self.controller, self.audio, self.limits = controller, audio, limits or Limits()
        self.queue: asyncio.Queue[tuple[int, str]] = asyncio.Queue(maxsize=8)
        self.done = asyncio.Event()
        self.epoch = 0
        self.last_input = time.monotonic()
        self.reason = "worker_shutdown"
        self.transferred = False

    def stop(self, reason: str) -> None:
        if not self.done.is_set():
            self.reason = reason
            self.done.set()

    def speech_activity(self) -> None:
        self.last_input = time.monotonic()

    def transcript(self, text: str) -> None:
        text = " ".join(text.split())
        if not text or self.done.is_set():
            return
        self.speech_activity()
        self.epoch += 1
        self.audio.interrupt()  # Drop buffered audio immediately on barge-in.
        try:
            self.queue.put_nowait((self.epoch, text[:2000]))
        except asyncio.QueueFull:
            self.stop("input_overflow")

    async def _turns(self) -> None:
        while True:
            epoch, text = await self.queue.get()
            out = await self.controller.turn(text)
            if out.get("ended"):
                self.stop("controller_ended")
                return
            if out.get("handed_over"):
                self.stop("human_handover")
                return
            if epoch == self.epoch and out.get("reply"):
                await asyncio.wait_for(
                    self.audio.say(out["reply"][:4000]), self.limits.speech_s
                )

    async def _poll(self) -> None:
        while True:
            await asyncio.sleep(self.limits.poll_s)
            state = await self.controller.state()
            if state.get("ended"):
                self.stop("controller_ended")
                return
            if state.get("handed_over"):
                self.stop("human_handover")
                return

    async def _watchdog(self) -> None:
        started = time.monotonic()
        while True:
            await asyncio.sleep(min(1, self.limits.silence_s / 4))
            if time.monotonic() - started >= self.limits.duration_s:
                self.stop("duration_limit")
                return
            if time.monotonic() - self.last_input >= self.limits.silence_s:
                self.stop("silence_timeout")
                return

    async def run(self, greeting: str) -> dict:
        tasks: list[asyncio.Task] = []

        def finished(task: asyncio.Task) -> None:
            if not task.cancelled() and task.exception() is not None:
                self.stop("service_failure")

        try:
            # Watchdogs cover greeting synthesis and playout too.
            tasks = [
                asyncio.create_task(self._watchdog()),
                asyncio.create_task(self._poll()),
            ]
            for task in tasks:
                task.add_done_callback(finished)
            await asyncio.wait_for(
                self.audio.say(greeting[:4000]), self.limits.speech_s
            )
            actor = asyncio.create_task(self._turns())
            actor.add_done_callback(finished)
            tasks.append(actor)
            await self.done.wait()
        except asyncio.CancelledError:
            self.stop("worker_shutdown")
            raise
        except Exception:  # noqa: BLE001 - SDK errors can contain caller data or credentials.
            self.stop("service_failure")
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.audio.interrupt()
            try:
                # Caller/room disconnect requires cleanup only. Every other
                # stop attempts the real call transfer before leaving audio.
                if self.reason not in ("caller_disconnected", "room_disconnected"):
                    try:
                        result = await asyncio.wait_for(
                            self.controller.handover(), self.limits.transfer_s
                        )
                        self.transferred = result.get("state") == "accepted"
                    except (WorkerError, TimeoutError):
                        self.transferred = False
                    if not self.transferred:
                        with contextlib.suppress(Exception):
                            await asyncio.wait_for(
                                self.audio.say(
                                    "I'm unable to connect your call. Please try again shortly."
                                ),
                                self.limits.cleanup_s,
                            )
                        # Disconnecting only the worker would strand an
                        # answered SIP caller in an empty room.
                        with contextlib.suppress(Exception):
                            await asyncio.wait_for(
                                self.audio.hangup(), self.limits.cleanup_s
                            )
            finally:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(
                        self.controller.end(self.reason), self.limits.cleanup_s
                    )
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(self.audio.close(), self.limits.cleanup_s)
        return {
            "reason": self.reason,
            "transfer_state": "accepted" if self.transferred else "unconfirmed",
        }
