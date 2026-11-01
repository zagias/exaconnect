"""Credentials must stop before audit, data reads or external model requests."""

from dataclasses import replace

import pytest

from exaconnect_controller.api import ai
from exaconnect_controller.commai.automation.assistant import SECRET_HINT

from .test_assistant_permissions import assistant  # noqa: F401
from .test_hub_assistant import shared_api  # noqa: F401

CREDENTIALS = [
    '{"password": "fictional-value"}',
    "{'api_key': 'fictional-value'}",
    '{"client_secret" : "fictional-value"}',
    '{"refresh_token": "fictional-value"}',
    "Authorization: Bearer fictional-access-value",
    "authorization: Basic ZmljdGlvbmFsOnRlc3Q=",
    "-----BEGIN PRIVATE KEY-----\nfictional\n-----END PRIVATE KEY-----",
    "-----BEGIN RSA PRIVATE KEY-----\nfictional\n-----END RSA PRIVATE KEY-----",
    "-----BEGIN OPENSSH PRIVATE KEY-----\nfictional\n-----END OPENSSH PRIVATE KEY-----",
    "postgresql://fictional:fictional-value@database.invalid/test",
]


@pytest.mark.parametrize("text", CREDENTIALS)
@pytest.mark.parametrize("history", [False, True])
def test_quoted_credentials_and_private_keys_never_leave_input_boundary(request, monkeypatch, text, history):
    state = request.getfixturevalue("shared_api")
    audits = []
    monkeypatch.setattr(ai.audit, "record", lambda *_: audits.append(True))
    body = {"question": "Help me choose a feature.", "history": []}
    if history:
        body["history"] = [{"q": text, "a": "Use secure entry"}]
    else:
        body["question"] = text
    response = state.client.post("/api/v1/assistant/ask", json=body)
    assert response.status_code == 200 and "not stored" in response.json()["answer"]
    assert state.model_calls == state.data_reads == state.plans == audits == []


@pytest.mark.parametrize(
    "text",
    [
        "How do I reset my password?",
        "Where do I add an API key?",
        "Does a bearer circuit offer a backup path?",
        "Can we rotate access tokens?",
    ],
)
def test_normal_product_questions_still_reach_advice(text):
    assert SECRET_HINT.search(text) is None


@pytest.mark.parametrize("via,scopes", [("session", ("connect:read",)), ("key", ("connect",))])
def test_read_only_sessions_and_keys_cannot_cancel_drafts(request, monkeypatch, via, scopes):
    state = request.getfixturevalue("assistant")
    state.user = replace(state.user, org_role="owner", via=via, scopes=scopes)
    writes = []
    monkeypatch.setattr(ai.act, "cancel", lambda *_: writes.append(True))
    response = state.client.post("/api/v1/assistant/plans/1/cancel")
    assert response.status_code == 403 and writes == []
