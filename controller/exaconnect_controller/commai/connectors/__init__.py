"""Integrations with business systems (ADR 0016).

Each connector lists the exact actions it supports, each with a kind (read,
create, update, cancel, refund, delete), whether it is sensitive (needs a
person's approval) and its input fields. AI and workflows never call a
connector directly: they go through commai.actions, which checks the
business allowed the action, validates inputs, gets approval where needed
and executes once with an idempotency key.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import psycopg

KINDS = ("read", "create", "update", "cancel", "refund", "delete")


@dataclass(frozen=True)
class Field:
    name: str
    label: str
    type: str = "string"  # string | datetime | email | phone | number | text
    required: bool = True


@dataclass(frozen=True)
class ActionSpec:
    name: str
    label: str
    kind: str
    sensitive: bool = False
    fields: tuple[Field, ...] = field(default_factory=tuple)


class ConnectorError(Exception):
    """The external system refused or failed. `cause` names it for repair:
    'permission', 'expired_signin', 'mapping', 'provider', 'input'."""

    def __init__(self, message: str, cause: str = "provider"):
        super().__init__(message)
        self.cause = cause


class Connector:
    app = ""
    label = ""
    description = ""
    auth = "none"  # none | oauth | token | credentials
    category = "other"  # see kit.CATEGORIES
    actions: dict[str, ActionSpec] = {}

    def validate(self, action: str, inputs: dict) -> dict:
        """Check and normalise inputs. Raise ValueError naming the problem."""
        spec = self.actions[action]
        out = {}
        for f in spec.fields:
            v = inputs.get(f.name)
            if v in (None, ""):
                if f.required:
                    raise ValueError(f"{f.label} is required.")
                continue
            out[f.name] = v
        return out

    def execute(self, conn: psycopg.Connection, connection: dict, action: str, inputs: dict, key: str) -> dict:
        """Run the action once. `key` is stable across retries: pass it to the
        provider as its idempotency key, or check for it before creating."""
        raise NotImplementedError

    def health(self, conn: psycopg.Connection, connection: dict) -> dict:
        """{"ok": bool, "cause": str, "detail": str}"""
        return {"ok": True, "cause": "", "detail": ""}

    def describe(self) -> dict:
        return {
            "app": self.app,
            "label": self.label,
            "description": self.description,
            "auth": self.auth,
            "actions": [
                {
                    "name": a.name,
                    "label": a.label,
                    "kind": a.kind,
                    "sensitive": a.sensitive,
                    "fields": [f.__dict__ for f in a.fields],
                }
                for a in self.actions.values()
            ],
        }


_registry: dict[str, Connector] = {}


def register(c: Connector) -> Connector:
    _registry[c.app] = c
    return c


# Connectors made at run time, such as a business's own REST app described by
# an OpenAPI document (ADR 0034): each resolver takes an app name and returns
# a Connector or None.
resolvers: list = []


def get(app: str) -> Connector:
    c = _registry.get(app)
    if c is None:
        for fn in resolvers:
            c = fn(app)
            if c is not None:
                return c
        raise KeyError(app)
    return c


def all_connectors() -> list[Connector]:
    return list(_registry.values())


def catalogue() -> list[dict]:
    return [c.describe() for c in sorted(_registry.values(), key=lambda c: c.label)]


def connection(conn: psycopg.Connection, customer_id: Any, app: str) -> dict | None:
    return conn.execute(
        "SELECT * FROM integration_connections WHERE customer_id = %s AND app = %s", (customer_id, app)
    ).fetchone()


from . import simulated  # noqa: E402,F401  - registers the simulated apps
