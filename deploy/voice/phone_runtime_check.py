"""Direct CI evidence that a running PBX retained the SIP REFER mitigation.

Reads only an existing disposable CI container. Does not send SIP requests,
change configuration, print XML bodies or connect to speech providers.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import PurePosixPath
from xml.etree import ElementTree as ET

VERSION = re.compile(
    r"(?:FreeSWITCH [Vv]ersion:? )?(\d+)\.(\d+)\.(\d+)(?:-release)?(?:[+ ][A-Za-z0-9._~+() -]+)?"
)
PRERELEASE = re.compile(r"dev|alpha|beta|rc[0-9]*|snapshot", re.IGNORECASE)


class CheckError(RuntimeError):
    pass


def version(value: str) -> tuple[str, bool]:
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    if len(lines) != 1 or len(lines[0]) > 160:
        return "unknown", False
    match = VERSION.fullmatch(lines[0])
    if match is None or PRERELEASE.search(lines[0]):
        return "unknown", False
    normalised = re.sub(r"^FreeSWITCH [Vv]ersion:? ", "", lines[0])
    return normalised, tuple(map(int, match.groups())) >= (1, 11, 2)


def validate(
    actual_version: str, guard: str, sofia_status: str, profiles: dict[str, str]
) -> dict:
    actual, patched = version(actual_version)
    guard_lines = guard.splitlines()
    if len(guard_lines) != 2 or guard_lines[1] not in ("true", "false"):
        raise CheckError("The startup guard receipt is missing or malformed")
    guarded, _ = version(guard_lines[0])
    if guard_lines[0] != "unknown" and guarded == "unknown":
        raise CheckError("The startup guard version is malformed")
    # The startup receipt must belong to this running binary; the image tag is
    # deliberately not used to infer security or transfer support.
    if guarded != actual:
        raise CheckError("The startup guard does not match the running PBX version")
    if not patched and guard_lines[1] != "false":
        raise CheckError("An affected or unknown PBX reports SIP transfers enabled")
    active = set()
    for line in sofia_status.splitlines():
        match = re.match(r"^\s*([A-Za-z0-9_.-]+)\s+profile\s+.*\bRUNNING\b", line)
        if match:
            active.add(match[1])
    if not active:
        raise CheckError("No currently running Sofia profiles were found")
    parsed = {}
    for path, xml in profiles.items():
        file = PurePosixPath(path)
        if (
            file.parent != PurePosixPath("/etc/freeswitch/sip_profiles")
            or file.suffix != ".xml"
        ):
            raise CheckError("The PBX returned an unexpected SIP profile path")
        try:
            root = ET.fromstring(xml)
        except ET.ParseError:
            raise CheckError("A SIP profile is not valid XML") from None
        name = root.get("name", "")
        if (
            root.tag != "profile"
            or not re.fullmatch(r"[A-Za-z0-9_.-]+", name)
            or name in parsed
        ):
            raise CheckError("A SIP profile has an invalid or duplicate identity")
        settings = root.findall("settings")
        if len(settings) != 1:
            raise CheckError("A SIP profile has ambiguous settings")
        transfers = settings[0].findall("param[@name='disable-transfer']")
        disabled = len(transfers) == 1 and transfers[0].get("value") == "true"
        if not patched and not disabled:
            raise CheckError(
                "An affected or unknown PBX profile does not disable SIP transfers"
            )
        parsed[name] = disabled
    if not active.issubset(parsed):
        raise CheckError(
            "A loaded Sofia profile is missing from the validated on-disk configuration"
        )
    return {
        "running_version": actual,
        "guard_version": guarded,
        "version_secure": patched,
        "guard_transfer_enabled": guard_lines[1] == "true",
        "loaded_profiles": [
            {"name": name, "disable_transfer": parsed[name]} for name in sorted(active)
        ],
    }


def docker(container: str, *args: str) -> str:
    try:
        result = subprocess.run(
            ["docker", "exec", container, *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.SubprocessError, OSError):
        raise CheckError("The bounded PBX runtime evidence read failed") from None
    if len(result.stdout) > 1_000_000:
        raise CheckError("PBX runtime evidence exceeded the bounded size")
    return result.stdout


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container", required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", args.container):
        raise SystemExit("Invalid CI container identity")
    try:
        actual = docker(args.container, "fs_cli", "-x", "version")
        guard = docker(args.container, "cat", "/run/xakore-phone-runtime/guard")
        status = docker(args.container, "fs_cli", "-x", "sofia status")
        paths = docker(
            args.container,
            "sh",
            "-c",
            "for profile in /etc/freeswitch/sip_profiles/*.xml; do "
            '[ ! -f "$profile" ] || printf "%s\\n" "$profile"; done',
        ).splitlines()
        if not paths or len(paths) > 100:
            raise CheckError("The SIP profile inventory is missing or oversized")
        profiles = {}
        for path in paths:
            file = PurePosixPath(path)
            if (
                file.parent != PurePosixPath("/etc/freeswitch/sip_profiles")
                or file.suffix != ".xml"
            ):
                raise CheckError("Unexpected SIP profile path")
            profiles[path] = docker(args.container, "cat", path)
        print(
            "PBX transfer mitigation: "
            + json.dumps(validate(actual, guard, status, profiles), sort_keys=True)
        )
    except CheckError as error:
        raise SystemExit(str(error)) from None


if __name__ == "__main__":
    main()
