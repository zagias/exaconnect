"""Exercise the real worker HTTP client and lifecycle with audio at its adapter."""

import asyncio
import importlib.util
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("xakore_phone_runtime", ROOT / "deploy/livekit/agent/runtime.py")
worker = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = worker
SPEC.loader.exec_module(worker)


class Audio:
    def __init__(self):
        self.spoken = []
        self.interrupted = 0
        self.hungup = False
        self.closed = False
        self.after_say = lambda _: None

    async def say(self, text):
        self.spoken.append(text)
        self.after_say(text)

    def interrupt(self):
        self.interrupted += 1

    async def hangup(self):
        self.hungup = True

    async def close(self):
        self.closed = True


async def setup(handler):
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
    controller = worker.Controller(http, "http://controller", "bootstrap-secret")
    controller.call = {"session_token": "one-call-only", "customer_id": "tenant-a", "conversation_id": "call-a"}
    audio = Audio()
    lifecycle = worker.PhoneLifecycle(
        controller,
        audio,
        worker.Limits(
            silence_s=1,
            duration_s=2,
            speech_s=0.2,
            poll_s=0.005,
            transfer_s=0.1,
            cleanup_s=0.1,
        ),
    )
    return http, lifecycle, audio


def test_inbox_human_takeover_transfers_without_waiting_for_caller_speech():
    requests = []

    def handler(request):
        requests.append(request)
        assert request.headers["Authorization"] == "Bearer one-call-only"
        assert request.url.path.startswith("/api/v1/commai/voice/livekit/calls/tenant-a/call-a")
        if request.method == "GET":
            return httpx.Response(200, json={"handed_over": True})
        return httpx.Response(200, json={"state": "accepted", "human_answered": False})

    async def run():
        http, lifecycle, audio = await setup(handler)
        async with http:
            result = await lifecycle.run("Hello")
        assert result == {"reason": "human_handover", "transfer_state": "accepted"}
        assert [r.url.path.rsplit("/", 1)[-1] for r in requests] == ["call-a", "handover", "end"]
        assert audio.closed and not audio.hungup

    asyncio.run(run())


def test_transfer_failure_never_claims_success_and_does_not_strand_answered_caller():
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"handed_over": True})
        if request.url.path.endswith("handover"):
            return httpx.Response(502, text="provider secret must never escape")
        return httpx.Response(200, json={"ended_at": "now"})

    async def run():
        http, lifecycle, audio = await setup(handler)
        async with http:
            result = await lifecycle.run("Hello")
        assert result["transfer_state"] == "unconfirmed"
        assert audio.hungup and audio.closed
        assert "unable to connect" in audio.spoken[-1]

    asyncio.run(run())


def test_barge_in_discards_old_reply_but_does_not_retry_mutating_turn():
    first_started, release_first = asyncio.Event(), asyncio.Event()
    turns = []

    async def handler(request):
        if request.url.path.endswith("turns"):
            import json

            body = json.loads(request.content)
            turns.append(body)
            if len(turns) == 1:
                first_started.set()
                await release_first.wait()
            return httpx.Response(200, json={"reply": "reply " + body["text"]})
        return httpx.Response(200, json={"handed_over": False})

    async def run():
        http, lifecycle, audio = await setup(handler)
        audio.after_say = lambda text: lifecycle.stop("caller_disconnected") if text == "reply second" else None
        async with http:
            task = asyncio.create_task(lifecycle.run("Hello"))
            lifecycle.transcript("first")
            await first_started.wait()
            lifecycle.transcript("second")
            release_first.set()
            result = await asyncio.wait_for(task, 1)
        assert audio.spoken == ["Hello", "reply second"]
        assert audio.interrupted >= 2 and audio.closed and not audio.hungup
        assert len(turns) == 2 and turns[0]["request_id"] != turns[1]["request_id"]
        assert result["reason"] == "caller_disconnected"

    asyncio.run(run())


@pytest.mark.parametrize("reason", ["silence_timeout", "duration_limit"])
def test_idle_and_absolute_duration_limits_are_bounded(reason):
    def handler(request):
        return httpx.Response(200, json={"state": "accepted"})

    async def run():
        http, lifecycle, audio = await setup(handler)
        lifecycle.limits = worker.Limits(
            silence_s=0.02 if reason == "silence_timeout" else 10,
            duration_s=0.02 if reason == "duration_limit" else 10,
            poll_s=0.01,
        )
        # Watchdog always wakes at least every second; the actual limit is
        # independent of provider/controller input and cannot extend forever.
        async with http:
            result = await asyncio.wait_for(lifecycle.run("Hello"), 1.2)
        assert result["reason"] == reason and audio.closed

    asyncio.run(run())


def test_shutdown_cancels_and_awaits_inflight_work_then_transfers_and_ends():
    started, stopped = asyncio.Event(), asyncio.Event()
    paths = []

    async def handler(request):
        paths.append(request.url.path)
        if request.url.path.endswith("turns"):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        return httpx.Response(200, json={"state": "accepted"})

    async def run():
        http, lifecycle, audio = await setup(handler)
        async with http:
            task = asyncio.create_task(lifecycle.run("Hello"))
            lifecycle.transcript("help")
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert stopped.is_set() and audio.closed
        assert paths[-2].endswith("handover") and paths[-1].endswith("end")

    asyncio.run(run())


def test_controller_does_not_follow_redirects_or_expose_provider_body():
    async def run():
        def handler(_):
            return httpx.Response(307, headers={"location": "https://other.example/steal"}, text="SECRET")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False) as http:
            controller = worker.Controller(http, "http://controller", "secret")
            with pytest.raises(worker.WorkerError) as error:
                await controller.start({})
            assert "SECRET" not in str(error.value)

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["greeting", "turn", "transfer_timeout", "hangup_timeout", "end_timeout", "close"])
def test_provider_and_cleanup_failures_are_bounded_and_never_retry_a_transfer(failure):
    """A timed-out REFER is unknown, not permission to issue a second REFER."""
    paths = []
    cancelled = set()

    async def hang(label):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.add(label)

    async def handler(request):
        assert request.headers["Authorization"] == "Bearer one-call-only"
        paths.append(request.url.path.rsplit("/", 1)[-1])
        if request.url.path.endswith("turns"):
            raise httpx.ReadTimeout("PRIVATE-PROVIDER-BODY", request=request)
        if request.method == "GET":
            return httpx.Response(200, json={"handed_over": failure != "turn"})
        if request.url.path.endswith("handover"):
            if failure == "transfer_timeout":
                await hang("transfer")
            return httpx.Response(502, text="PRIVATE-PROVIDER-BODY")
        if request.url.path.endswith("end") and failure == "end_timeout":
            await hang("end")
        return httpx.Response(200, json={})

    async def run():
        http, lifecycle, audio = await setup(handler)
        lifecycle.limits = worker.Limits(
            poll_s=0.005, silence_s=0.5, duration_s=1, speech_s=0.03, transfer_s=0.03, cleanup_s=0.03
        )
        if failure == "greeting":

            async def fail_say(_):
                raise RuntimeError("PRIVATE-PROVIDER-BODY")

            audio.say = fail_say
        if failure == "turn":
            audio.after_say = lambda _: lifecycle.transcript("A fictional request")
        if failure == "hangup_timeout":
            audio.hangup = lambda: hang("hangup")
        if failure == "close":

            async def fail_close():
                audio.closed = True
                raise RuntimeError("PRIVATE-PROVIDER-BODY")

            audio.close = fail_close
        async with http:
            result = await asyncio.wait_for(lifecycle.run("Hello"), 0.6)
        assert result["transfer_state"] == "unconfirmed"
        assert paths.count("handover") == 1 and paths.count("end") == 1
        assert "PRIVATE" not in str(result) and audio.closed
        if failure == "greeting" or failure == "turn":
            assert result["reason"] == "service_failure"
        if failure == "transfer_timeout":
            assert "transfer" in cancelled
        if failure == "hangup_timeout":
            assert "hangup" in cancelled
        else:
            assert audio.hungup
        if failure == "end_timeout":
            assert "end" in cancelled

    asyncio.run(run())
