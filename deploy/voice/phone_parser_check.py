"""Exercise the exact guard functions in an existing disposable PBX image.

Only private temporary version-command stubs are created and removed. No profile,
service, provider, real call, environment value or credential is read or changed.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def script() -> tuple[str, str]:
    source = (ROOT / "deploy/freeswitch/phone-runtime.sh").read_text()
    functions = (
        "parse_version() {"
        + source.split("parse_version() {", 1)[1].split("# This intentionally", 1)[0]
    )
    bodies = json.loads(
        (ROOT / "controller/tests/phone_runtime_versions.json").read_text()
    )
    frames = json.loads(
        (ROOT / "controller/tests/phone_runtime_cli_versions.json").read_text()
    )
    if not 1 <= len(bodies) <= 100 or not 1 <= len(frames) <= 100:
        raise ValueError("Unexpected parser fixture inventory")
    summary = f"PBX image parser matrix: passed ({len(bodies)} bodies, {len(frames)} CLI frames)"
    lines = [
        "set -eu",
        "umask 077",
        "LC_ALL=C; export LC_ALL",
        functions,
        "fixture_dir=$(mktemp -d /tmp/xakore-version-fixture.XXXXXX)",
        "trap 'rm -rf \"$fixture_dir\"' 0 1 2 15",
        "printf '%s\\n' '#!/bin/sh' 'printf \"%s\" \"$EXA_PHONE_PARSER_FIXTURE\"' > \"$fixture_dir/freeswitch\"",
        'cp "$fixture_dir/freeswitch" "$fixture_dir/fs_cli"',
        'chmod 700 "$fixture_dir/freeswitch" "$fixture_dir/fs_cli"',
        'PATH="$fixture_dir:$PATH"; export PATH',
    ]
    for index, case in enumerate([*bodies, *frames]):
        raw = shlex.quote(case.get("value", case.get("stdout", "")))
        if "command" in case:
            command = (
                "fs_cli -x version"
                if case["command"] == "fs_cli"
                else "freeswitch -version"
            )
            lines.extend(
                [
                    f"EXA_PHONE_PARSER_FIXTURE={raw}; export EXA_PHONE_PARSER_FIXTURE",
                    f"if value=$(runtime_version {command}); then :; else value=unknown; fi",
                ]
            )
        else:
            lines.append(
                f"if value=$(printf '%s' {raw} | parse_version); then :; else value=unknown; fi"
            )
        lines.extend(
            [
                "secure=false",
                'if [ "$value" != unknown ] && patched "$value"; then secure=true; fi',
                f'if [ "$value" != {shlex.quote(case["normalised"])} ] || [ "$secure" != {str(case["secure"]).lower()} ]; then',
                f"  printf '%s\\n' 'PBX image parser case {index} failed' >&2; exit 1",
                "fi",
            ]
        )
    lines.append("printf '%s\\n' " + shlex.quote(summary))
    return "\n".join(lines) + "\n", summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--container")
    mode.add_argument("--emit-script", action="store_true")
    args = parser.parse_args()
    body, summary = script()
    if args.emit_script:
        print(body, end="")
        return
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", args.container):
        raise SystemExit("Invalid disposable PBX identity")
    try:
        result = subprocess.run(
            ["docker", "exec", "-i", args.container, "/bin/sh", "-s"],
            input=body,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        raise SystemExit(
            "The bounded PBX image parser matrix did not complete"
        ) from None
    if result.returncode or result.stdout != summary + "\n":
        # No environment values or arbitrary container output may escape.
        raise SystemExit(
            "The PBX image parser matrix failed; no runtime readiness was established"
        )
    print(summary)


if __name__ == "__main__":
    main()
