"""The PBX wrapper mitigates vulnerable builds before startup and signs real probes.

All FreeSWITCH/HTTP commands are local fakes; no PBX or network listener is used.
The optional curl branch exercises the installed OpenSSL hash primitive only.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy/freeswitch/phone-runtime.sh"
START = ROOT / "deploy/freeswitch/start.sh"
SECRET = "fictional-runtime-signing-secret-for-tests"
PROFILE = """<profile name="internal">
  <!-- <param name="disable-transfer" value="false"/> -->
  <settings>
    <param name="sip-port" value="5060"/>
  </settings>
</profile>
"""


def _executable(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(0o755)


@pytest.fixture
def runtime(tmp_path):
    binary = tmp_path / "bin"
    binary.mkdir()
    config = tmp_path / "config"
    profiles = config / "sip_profiles"
    profiles.mkdir(parents=True)
    (config / "freeswitch.xml").write_text("<include/>\n")
    vanilla = tmp_path / "vanilla"
    profile = profiles / "internal.xml"
    profile.write_text(PROFILE)
    profile.chmod(0o640)
    state = tmp_path / "state"
    capture = tmp_path / "capture.json"
    commands = tmp_path / "commands"
    env = {
        **os.environ,
        "PATH": str(binary) + os.pathsep + "/usr/bin:/bin",
        "TEST_FS_VERSION": "FreeSWITCH Version 1.10.12-release+git~test~64bit",
        "TEST_RUNNING_VERSION": "FreeSWITCH Version 1.10.12-release+git~test~64bit",
        "TEST_CAPTURE": str(capture),
        "TEST_COMMANDS": str(commands),
        "EXA_PBX_SECRET": SECRET,
        # Deliberately hostile transport settings must never affect the probe.
        "EXA_PBX_CONTROLLER_URL": "https://outside.example.invalid",
        "http_proxy": "http://outside.example.invalid",
        "https_proxy": "http://outside.example.invalid",
    }
    _executable(binary / "freeswitch", '#!/bin/sh\nprintf "%s\\n" "$TEST_FS_VERSION"\n')
    _executable(
        binary / "fs_cli",
        '#!/bin/sh\nprintf "%s\\n" "$*" >> "$TEST_COMMANDS"\nprintf "%s\\n" "$TEST_RUNNING_VERSION"\n',
    )
    _executable(binary / "timeout", '#!/bin/sh\nshift\nexec "$@"\n')
    return SimpleNamespace(
        binary=binary,
        config=config,
        profile=profile,
        state=state,
        capture=capture,
        commands=commands,
        env=env,
        tmp=tmp_path,
        vanilla=vanilla,
    )


def _run(runtime, mode):
    args = ["/bin/sh", str(SCRIPT), mode]
    args += [str(runtime.config), str(runtime.state), str(runtime.vanilla)] if mode == "guard" else [str(runtime.state)]
    return subprocess.run(args, env=runtime.env, text=True, capture_output=True, timeout=10)


@pytest.mark.parametrize(
    "version",
    [
        "FreeSWITCH Version 1.10.12-release+git~test~64bit",
        "1.11.1",
        "unknown",
        "1.11.2-rc1",
        "1.11.2-dev",
        "1.11.2+snapshot",
        "1.11.20\n1.10.12",
    ],
)
def test_unsafe_and_unknown_binaries_disable_every_profile_before_start(runtime, version):
    runtime.env["TEST_FS_VERSION"] = version
    (runtime.profile.parent / "external.xml").write_text(PROFILE.replace("internal", "external"))
    result = _run(runtime, "guard")
    assert result.returncode == 0, result.stderr
    for profile in runtime.profile.parent.glob("*.xml"):
        assert '    <param name="disable-transfer" value="true"/>\n  </settings>' in profile.read_text()
    assert runtime.state.joinpath("guard").read_text().splitlines()[1] == "false"
    assert runtime.profile.stat().st_mode & 0o777 == 0o640
    # Repeat startup is idempotent: one active mitigation per profile.
    assert _run(runtime, "guard").returncode == 0
    assert runtime.profile.read_text().count('value="true"') == 1


@pytest.mark.parametrize("version", ["1.11.2", "FreeSWITCH Version 1.11.3-release+git~abc~64bit", "2.0.0"])
def test_patched_versions_preserve_configuration(runtime, version):
    runtime.env["TEST_FS_VERSION"] = version
    before = runtime.profile.read_bytes()
    result = _run(runtime, "guard")
    assert result.returncode == 0, result.stderr
    assert runtime.profile.read_bytes() == before
    assert runtime.state.joinpath("guard").read_text().splitlines()[1] == "true"


@pytest.mark.parametrize(
    "xml",
    [
        PROFILE.replace("  </settings>", ""),
        PROFILE.replace("</profile>", ""),
        PROFILE.replace('<param name="sip-port" value="5060"/>', '<param name="disable-transfer"\n value="false"/>'),
    ],
)
def test_ambiguous_or_invalid_xml_refuses_startup_without_guard_marker(runtime, xml):
    runtime.profile.write_text(xml)
    before = runtime.profile.read_bytes()
    assert _run(runtime, "guard").returncode != 0
    assert not runtime.state.joinpath("guard").exists()
    assert runtime.profile.read_bytes() == before


def test_existing_transfer_permission_is_replaced_and_read_only_failure_aborts(runtime):
    runtime.profile.write_text(
        PROFILE.replace(
            '    <param name="sip-port" value="5060"/>', '    <param name="disable-transfer" value="false"/>'
        )
    )
    assert _run(runtime, "guard").returncode == 0
    assert '    <param name="disable-transfer" value="false"/>' not in runtime.profile.read_text()
    _executable(runtime.binary / "mv", "#!/bin/sh\nexit 1\n")
    assert _run(runtime, "guard").returncode != 0
    assert not runtime.state.joinpath("guard").exists()


def _python_transport(runtime):
    """Patch only the HTTP transport; execute the exact embedded stdlib signer."""
    hooks = runtime.tmp / "hooks"
    hooks.mkdir()
    (hooks / "sitecustomize.py").write_text("""
import json, os, urllib.request
class Response:
    status = 200
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def read(self, size): return b'{"pbx_transfer_ready":true,"expires_in_s":300}'
class Opener:
    def open(self, request, timeout):
        with open(os.environ["TEST_CAPTURE"], "w") as f:
            json.dump({"url": request.full_url, "headers": dict(request.header_items()),
                       "body": request.data.decode(), "timeout": timeout}, f)
        return Response()
def build_opener(*handlers):
    assert any(isinstance(h, urllib.request.ProxyHandler) and h.proxies == {} for h in handlers)
    redirect = next(h for h in handlers if isinstance(h, urllib.request.HTTPRedirectHandler))
    assert redirect.redirect_request(None, None, None, None, None, None) is None
    return Opener()
urllib.request.build_opener = build_opener
""")
    runtime.env["PYTHONPATH"] = str(hooks)
    _executable(runtime.binary / "python3", f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')


def _verify_receipt(runtime):
    captured = json.loads(runtime.capture.read_text())
    body = captured["body"].encode()
    payload = json.loads(body)
    assert body == json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    assert captured["url"] == "http://controller:8000/api/v1/commai/internal/voice/phone-runtime"
    headers = {k.lower(): v for k, v in captured["headers"].items()}
    expected = hmac.new(runtime.env["EXA_PBX_SECRET"].encode(), body, hashlib.sha256).hexdigest()
    assert headers["x-exa-pbx-auth"] == expected
    assert str(uuid.UUID(payload["nonce"])) == payload["nonce"]
    assert payload["instance_id"] == "main"
    assert payload["checked_at"].endswith(("Z", "+00:00"))
    assert SECRET not in runtime.capture.read_text()
    assert runtime.commands.read_text().strip() == "-x version"
    return payload


def test_python_probe_signs_actual_running_version_and_guard_marker(runtime):
    runtime.env["TEST_FS_VERSION"] = runtime.env["TEST_RUNNING_VERSION"] = "FreeSWITCH Version 1.11.2-release+git~test"
    _python_transport(runtime)
    assert _run(runtime, "guard").returncode == 0
    result = _run(runtime, "probe")
    assert result.returncode == 0, result.stderr
    payload = _verify_receipt(runtime)
    assert payload["version"] == "1.11.2-release+git~test" and payload["transfer_enabled"] is True
    assert SECRET not in result.stdout + result.stderr


def test_a_new_running_version_cannot_erase_startup_mitigation(runtime):
    _python_transport(runtime)
    assert _run(runtime, "guard").returncode == 0
    runtime.env["TEST_RUNNING_VERSION"] = "1.11.3"
    assert _run(runtime, "probe").returncode == 0
    assert _verify_receipt(runtime)["transfer_enabled"] is False


@pytest.mark.parametrize("problem", ["no_guard", "no_secret", "bad_running_version"])
def test_missing_evidence_never_sends_a_receipt(runtime, problem):
    _python_transport(runtime)
    if problem != "no_guard":
        assert _run(runtime, "guard").returncode == 0
    if problem == "no_secret":
        runtime.env["EXA_PBX_SECRET"] = ""
    if problem == "bad_running_version":
        runtime.env["TEST_RUNNING_VERSION"] = "ERROR"
    result = _run(runtime, "probe")
    assert result.returncode != 0 and not runtime.capture.exists()
    assert SECRET not in result.stdout + result.stderr


@pytest.mark.parametrize("secret", [SECRET, "a" * 64, "long-unicode-secret-" + "é" * 70])
def test_curl_fallback_hmac_matches_stdlib_without_secret_arguments(runtime, secret):
    if not shutil.which("openssl"):
        pytest.skip("OpenSSL CLI is needed only for the optional fallback test")
    # A BusyBox-like PATH without Python exercises the second signing path.
    for name in ("awk", "sed", "mkdir", "mktemp", "mv", "rm", "cp", "cat", "od", "wc", "tr", "date", "openssl"):
        target = shutil.which(name)
        assert target, name
        (runtime.binary / name).symlink_to(target)
    runtime.env["PATH"] = str(runtime.binary)
    runtime.env["EXA_PBX_SECRET"] = secret
    runtime.env["TEST_FS_VERSION"] = runtime.env["TEST_RUNNING_VERSION"] = "1.11.3"
    _executable(
        runtime.binary / "curl",
        f"""#!{sys.executable}
import json, os, sys
args=sys.argv[1:]
assert args[0] == "-q" and "--location" not in args
assert args[args.index("--max-time")+1] == "5"
assert args[args.index("--max-redirs")+1] == "0"
assert os.environ["EXA_PBX_SECRET"] not in " ".join(args)
headers={{}}
for i,a in enumerate(args):
    if a == "--header":
        k,v=args[i+1].split(": ",1); headers[k]=v
with open(args[args.index("--data-binary")+1][1:]) as f: body=f.read()
with open(os.environ["TEST_CAPTURE"],"w") as f:
    json.dump({{"url":args[-1],"headers":headers,"body":body}},f)
print("200",end="")
""",
    )
    assert _run(runtime, "guard").returncode == 0
    result = _run(runtime, "probe")
    assert result.returncode == 0, result.stderr
    assert _verify_receipt(runtime)["transfer_enabled"] is True
    assert secret not in result.stdout + result.stderr


def test_start_wrapper_guards_before_stock_entrypoint_and_refreshes_every_minute():
    wrapper = START.read_text()
    script = SCRIPT.read_text()
    assert wrapper.index("/exacarib-phone-runtime.sh guard || exit 1") < wrapper.index("exec /docker-entrypoint.sh")
    assert "/exacarib-phone-runtime.sh loop &" in wrapper
    assert "sleep 60" in script
    assert "EXA_FREESWITCH_VERSION" not in script  # an environment assertion is not runtime evidence


def test_busybox_only_guard_works_but_signing_dependencies_fail_closed(runtime):
    for name in ("awk", "sed", "mkdir", "mktemp", "mv", "rm", "cp"):
        (runtime.binary / name).symlink_to(shutil.which(name))
    runtime.env["PATH"] = str(runtime.binary)
    assert _run(runtime, "guard").returncode == 0
    result = _run(runtime, "probe")
    assert result.returncode != 0 and not runtime.capture.exists()
    assert "readiness stays unavailable" in result.stderr


def test_guard_failure_prevents_starting_stock_pbx_or_background_watchers(runtime):
    helper = runtime.tmp / "helper"
    stock = runtime.tmp / "stock"
    _executable(helper, '#!/bin/sh\nprintf "%s\\n" "$*" >> "$TEST_COMMANDS"\nexit 1\n')
    _executable(stock, '#!/bin/sh\necho stock >> "$TEST_COMMANDS"\n')
    wrapper = runtime.tmp / "start.sh"
    wrapper.write_text(
        START.read_text()
        .replace("/exacarib-phone-runtime.sh", str(helper))
        .replace("/docker-entrypoint.sh", str(stock))
    )
    result = subprocess.run(["/bin/sh", str(wrapper)], env=runtime.env, text=True, capture_output=True, timeout=5)
    assert result.returncode == 1
    assert runtime.commands.read_text().splitlines() == ["guard"]


def test_stock_first_boot_uses_guarded_templates_without_copying_defaults(runtime):
    (runtime.config / "freeswitch.xml").unlink()
    runtime.profile.unlink()
    (runtime.vanilla / "sip_profiles").mkdir(parents=True)
    template = runtime.vanilla / "sip_profiles" / "external.xml"
    template.write_text(PROFILE.replace("internal", "external"))
    provided = runtime.config / "vars.xml"
    provided.write_text("provided configuration must stay unchanged")
    assert _run(runtime, "guard").returncode == 0
    assert 'value="true"' in template.read_text()
    assert not runtime.profile.exists() and not (runtime.config / "freeswitch.xml").exists()
    assert provided.read_text() == "provided configuration must stay unchanged"
    assert runtime.state.joinpath("guard").read_text().splitlines()[1] == "false"


def test_stock_first_boot_refuses_missing_or_unwritable_guard_templates(runtime):
    (runtime.config / "freeswitch.xml").unlink()
    assert _run(runtime, "guard").returncode != 0
    assert not runtime.state.joinpath("guard").exists()
