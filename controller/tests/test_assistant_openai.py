"""Offline provider boundaries. No database or real credential is used."""

import json
from dataclasses import replace
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

import pytest

from exaconnect_controller.ai import ask
from exaconnect_controller.api.ai import _assistant_llm, status
from exaconnect_controller.settings import Settings


def connection(**kw):
    base = Settings(
        llm_api_key="fictional-legacy-key",
        llm_base_url="https://api.deepinfra.com/v1/openai",
        llm_model="legacy-model",
        assistant_llm_api_key="",
        assistant_llm_base_url="",
        assistant_llm_model="",
    )
    return replace(base, **kw)


def openai_settings(**kw):
    return connection(
        assistant_llm_api_key="fictional-openai-key",
        assistant_llm_base_url=ask.OPENAI_BASE_URL,
        assistant_llm_model=ask.OPENAI_ASSISTANT_MODEL,
        **kw,
    )


def completed(text="Fixture answer"):
    return {
        "status": "completed",
        "output": [
            {"type": "reasoning", "summary": []},
            {
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": text}],
            },
        ],
    }


class Reply:
    status = 200

    def __init__(self, value):
        self.raw = value if isinstance(value, bytes) else json.dumps(value).encode()

    def geturl(self):
        return ask.OPENAI_BASE_URL + "/responses"

    def read(self, size):
        return self.raw[:size]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def transport(monkeypatch, value):
    calls, handlers = [], []

    class Opener:
        def open(self, request, timeout):
            calls.append((request, timeout))
            if isinstance(value, Exception):
                raise value
            return Reply(value)

    def build(*args):
        handlers.extend(args)
        return Opener()

    monkeypatch.setattr(ask.urllib.request, "build_opener", build)
    monkeypatch.setattr(
        ask.urllib.request, "urlopen", lambda *a, **k: pytest.fail("Unexpected legacy/fallback request")
    )
    return calls, handlers


def request():
    return ask.chat(
        "Fictional instructions",
        "Fictional question",
        api_key="fictional-openai-key",
        base_url=ask.OPENAI_BASE_URL,
        model=ask.OPENAI_ASSISTANT_MODEL,
        max_tokens=1400,
    )


def test_complete_provider_triples_are_independent():
    s = openai_settings()
    c = _assistant_llm(s)
    assert (c.provider, c.base_url, c.model, c.enabled) == (
        "openai",
        ask.OPENAI_BASE_URL,
        ask.OPENAI_ASSISTANT_MODEL,
        True,
    )
    assert c.api_key == "fictional-openai-key" and s.llm_api_key == "fictional-legacy-key"
    assert s.llm_model == "legacy-model" and "fictional-openai-key" not in repr(s) + repr(c)
    legacy = _assistant_llm(connection())
    assert legacy.api_key == "fictional-legacy-key" and legacy.provider == "deepinfra"


@pytest.mark.parametrize(
    "values",
    [
        {"assistant_llm_api_key": "fictional"},
        {"assistant_llm_base_url": ask.OPENAI_BASE_URL},
        {"assistant_llm_model": ask.OPENAI_ASSISTANT_MODEL},
        {
            "assistant_llm_api_key": "fictional",
            "assistant_llm_base_url": "https://api.openai.com.evil.invalid/v1",
            "assistant_llm_model": ask.OPENAI_ASSISTANT_MODEL,
        },
        {
            "assistant_llm_api_key": "fictional",
            "assistant_llm_base_url": ask.OPENAI_BASE_URL,
            "assistant_llm_model": "unapproved",
        },
        {
            "assistant_llm_api_key": "fictional\nkey",
            "assistant_llm_base_url": ask.OPENAI_BASE_URL,
            "assistant_llm_model": ask.OPENAI_ASSISTANT_MODEL,
        },
    ],
)
def test_partial_or_unsupported_override_never_borrows_legacy(values):
    c = _assistant_llm(connection(**values))
    assert not c.enabled and not c.api_key and c.unavailable_reason


def test_context_status_reports_the_resolved_model():
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=openai_settings())))
    result = status(req, SimpleNamespace(role="customer", scopes=None))
    assert result["ask_enabled"] and result["model"] == ask.OPENAI_ASSISTANT_MODEL and result["provider"] == "openai"


def test_exact_openai_request_and_reasoning_is_not_displayed(monkeypatch):
    calls, handlers = transport(monkeypatch, completed())
    assert request() == "Fixture answer"
    assert len(calls) == 1
    req, timeout = calls[0]
    assert req.full_url == ask.OPENAI_BASE_URL + "/responses" and timeout == 60
    assert json.loads(req.data) == {
        "model": ask.OPENAI_ASSISTANT_MODEL,
        "instructions": "Fictional instructions",
        "input": "Fictional question",
        "reasoning": {"effort": "low"},
        "max_output_tokens": 1400,
        "store": False,
    }
    assert req.get_header("Authorization") == "Bearer fictional-openai-key"
    assert handlers[0].proxies == {} and isinstance(handlers[1], ask._NoRedirect)
    assert handlers[1].redirect_request(None, None, 302, "redirect", None, "https://foreign.invalid") is None


@pytest.mark.parametrize(
    "body",
    [
        [],
        {},
        {"status": "incomplete", "output": []},
        {"status": "failed", "output": []},
        {"status": "completed", "error": {"message": "fixture"}, "output": []},
        {"status": "completed", "output": []},
        {"status": "completed", "output": [{"type": "function_call", "name": "unsafe"}]},
        {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "refusal", "refusal": "no"}],
                }
            ],
        },
        {"status": "completed", "output": [{"type": "message", "role": "user", "status": "completed", "content": []}]},
        {"status": "completed", "output": completed()["output"] + [completed()["output"][1]]},
        b"not-json",
        b"x" * (ask.MAX_RESPONSE_BYTES + 1),
    ],
)
def test_unusable_outputs_fail_without_retry_or_fallback(monkeypatch, body):
    calls, _ = transport(monkeypatch, body)
    with pytest.raises(ask.AskError):
        request()
    assert len(calls) == 1


@pytest.mark.parametrize(
    "error",
    [
        HTTPError(ask.OPENAI_BASE_URL, 400, "fixture", None, None),
        HTTPError(ask.OPENAI_BASE_URL, 429, "fixture", None, None),
        HTTPError(ask.OPENAI_BASE_URL, 500, "fixture", None, None),
        URLError("fixture"),
        TimeoutError(),
    ],
)
def test_provider_errors_are_single_safe_failures(monkeypatch, error):
    calls, _ = transport(monkeypatch, error)
    with pytest.raises(ask.AskError) as result:
        request()
    assert len(calls) == 1 and "fictional-openai-key" not in str(result.value)


def test_legacy_wire_contract_is_unchanged(monkeypatch):
    calls = []

    def urlopen(req, timeout):
        calls.append((req, timeout))
        return Reply({"choices": [{"message": {"content": "Legacy answer"}}]})

    monkeypatch.setattr(ask.urllib.request, "urlopen", urlopen)
    assert (
        ask.chat(
            "system",
            "user",
            api_key="fictional-legacy-key",
            base_url="https://api.deepinfra.com/v1/openai",
            model="legacy-model",
        )
        == "Legacy answer"
    )
    assert len(calls) == 1
    req, timeout = calls[0]
    assert req.full_url == "https://api.deepinfra.com/v1/openai/chat/completions"
    assert json.loads(req.data) == {
        "model": "legacy-model",
        "temperature": 0.2,
        "max_tokens": 700,
        "messages": [{"role": "system", "content": "system"}, {"role": "user", "content": "user"}],
    }
