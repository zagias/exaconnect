"""Zapier, Make and n8n app definitions (ADR 0029): every CommAI endpoint they
call exists in the published OpenAPI schema, and the Zapier signature check
agrees with how CommAI signs webhook deliveries."""

import json
import pathlib
import re
import shutil
import subprocess
import time

import pytest

from exaconnect_controller.commai import webhooks
from exaconnect_controller.main import app

ROOT = pathlib.Path(__file__).resolve().parents[2] / "integrations"
PREFIX = "/api/v1/commai/customers/{}"
CALL = re.compile(r"(?:api|commAiRequest)\((?:z, bundle|this), '(GET|POST|PUT|PATCH|DELETE)', [`'\"]([^`'\"]+)[`'\"]")


def _norm(path: str) -> str:
    path = re.sub(r"\$\{[^}]*\}", "{}", path)  # JavaScript template literal
    path = re.sub(r"\{\{[^}]*\}\}", "{}", path)  # Make IML
    path = re.sub(r"\{[^}]+\}", "{}", path)  # OpenAPI parameter
    return path.split("?")[0]


def _schema() -> set[tuple[str, str]]:
    out = set()
    for p, ops in app.openapi()["paths"].items():
        for m in ops:
            out.add((m.upper(), _norm(p)))
    return out


def _code_calls(folder: str, suffix: str) -> set[tuple[str, str]]:
    calls = set()
    for f in (ROOT / folder).rglob(f"*{suffix}"):
        text = f.read_text()
        for method, path in CALL.findall(text):
            calls.add((method, _norm(PREFIX + path)))
        if "/api/v1/auth/me" in text:
            calls.add(("GET", "/api/v1/auth/me"))
    return calls


def _make_calls() -> set[tuple[str, str]]:
    calls = set()

    def walk(node):
        if isinstance(node, dict):
            if isinstance(node.get("url"), str) and isinstance(node.get("method"), str):
                url = node["url"]
                full = url.split("}}", 1)[1] if url.startswith("{{parameters.baseUrl}}") else PREFIX + url
                calls.add((node["method"].upper(), _norm(full)))
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(json.loads((ROOT / "make" / "app.json").read_text()))
    return calls


CORE = {
    ("POST", PREFIX + "/webhooks"),
    ("DELETE", PREFIX + "/webhooks/{}"),
    ("POST", PREFIX + "/contacts"),
    ("POST", PREFIX + "/conversations"),
    ("POST", PREFIX + "/conversations/{}/messages"),
    ("POST", PREFIX + "/conversations/{}/notes"),
    ("POST", PREFIX + "/actions"),
    ("GET", "/api/v1/auth/me"),
}


@pytest.mark.parametrize(
    "name,calls",
    [
        ("zapier", lambda: _code_calls("zapier", ".js")),
        ("n8n", lambda: _code_calls("n8n", ".ts")),
        ("make", _make_calls),
    ],
)
def test_declared_endpoints_exist_in_openapi(name, calls):
    found = calls()
    assert CORE <= found, f"{name} is missing {CORE - found}"
    missing = found - _schema()
    assert not missing, f"{name} calls endpoints CommAI does not publish: {sorted(missing)}"
    assert (ROOT / name / "README.md").is_file()


def test_each_definition_is_well_formed():
    make = json.loads((ROOT / "make" / "app.json").read_text())
    assert {"base", "connection", "webhooks", "modules"} <= set(make)
    for pkg in ("zapier", "n8n"):
        json.loads((ROOT / pkg / "package.json").read_text())
    n8n = json.loads((ROOT / "n8n" / "package.json").read_text())
    for built in n8n["n8n"]["nodes"] + n8n["n8n"]["credentials"]:
        src = built.removeprefix("dist/").removesuffix(".js") + ".ts"
        assert (ROOT / "n8n" / src).is_file(), src
    for f in ROOT.rglob("*"):
        if f.is_file():
            assert not re.search(r"exa_[A-Za-z0-9]{16,}|whsec_[A-Za-z0-9]{8,}", f.read_text()), f


@pytest.mark.skipif(not shutil.which("node"), reason="Node.js is not installed")
def test_zapier_signature_check_matches_commai():
    secret = "whsec_" + "t" * 12
    body = b'{"id":"evt_1","type":"conversation.created"}'
    ts = int(time.time())
    good = webhooks.sign(secret, ts, body)
    script = (
        "const { verify } = require(process.argv[1]);"
        "const [secret, ts, sig, body] = process.argv.slice(2);"
        "const h = { 'X-ExaCarib-Timestamp': ts, 'X-ExaCarib-Signature': sig };"
        "process.stdout.write(String(verify(secret, h, body)));"
    )

    def run(sig: str, when: int = ts) -> str:
        args = ["node", "-e", script, str(ROOT / "zapier" / "lib.js"), secret, str(when), sig, body.decode()]
        return subprocess.run(args, capture_output=True, text=True, timeout=20, check=True).stdout

    assert run(good) == "true"
    assert run(good.replace("v1=", "v1=0")[:-1]) == "false"
    assert run(webhooks.sign(secret, ts - 600, body), ts - 600) == "false"  # too old
