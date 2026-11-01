"""Capability discovery must agree with the existing scoped POST policy."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from exaconnect_controller.api.ai import status


@pytest.mark.parametrize(
    "role,scopes,enabled",
    [
        ("customer", None, True),
        ("customer", ("connect",), True),
        ("customer", ("connect:read",), False),
        ("customer", (), False),
        ("carrier", None, False),
        ("admin", ("commai:read",), False),
    ],
)
def test_ask_readiness_respects_effective_scope(settings, role, scopes, enabled):
    settings = replace(settings, llm_api_key="test-key", llm_model="test")
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=settings)))
    result = status(request, SimpleNamespace(role=role, scopes=scopes))
    assert result["ask_enabled"] is enabled
    assert bool(result["ask_unavailable_reason"]) is not enabled
