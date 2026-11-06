"""Direct CI evidence that a running PBX retained the SIP REFER mitigation.

Reads only an existing disposable CI container. Does not send SIP requests,
change configuration, print XML bodies or connect to speech providers.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import PurePosixPath
from xml.etree import ElementTree as ET


class CheckError(RuntimeError):
    pass


VERSION = re.compile(
    r"(?P<major>0|[1-9][0-9]{0,2})\.(?P<minor>0|[1-9][0-9]{0,2})\."
    r"(?P<patch>0|[1-9][0-9]{0,5})"
    r"(?P<release>-release(?:-[0-9]{1,20}-[0-9a-f]{7,40})?)?"
    r"(?:\+git~(?P<date>20[0-9]{6}T[0-9]{6}Z)~(?P<sha>[0-9a-f]{7,40}))?"
    r"(?:~(?P<bits>32bit|64bit))?"
)


def version(value: str) -> tuple[str, bool]:
    """Return the validated version text and patched-release status."""
    if len(value) > 320:
        return "unknown", False
    normalised = re.sub(r"^FreeSWITCH [Vv]ersion:? ", "", value)
    compact, separator, human = normalised.partition(" (")
    if separator:
        if not human.endswith(")") or "(" in human or ")" in human[:-1]:
            return "unknown", False
        human = human[:-1]
    match = VERSION.fullmatch(compact)
    if match is None:
        return "unknown", False
    release, date, sha, bits = (
        match[name] or "" for name in ("release", "date", "sha", "bits")
    )
    if date:
        # Keep this date check identical to the portable awk adapter (2000-2099).
        year, month, day = int(date[:4]), int(date[4:6]), int(date[6:8])
        if not 1 <= month <= 12:
            return "unknown", False
        days = (31, 29 if year % 4 == 0 else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
        if (
            not 1 <= day <= days[month - 1]
            or int(date[9:11]) > 23
            or int(date[11:13]) > 59
            or int(date[13:15]) > 59
        ):
            return "unknown", False
        if release.startswith("-release-") and release.rsplit("-", 1)[1] != sha:
            return "unknown", False
    if separator:
        if not bits:
            return "unknown", False
        # Depending on whether -release is in MICRO or REVISION, the human
        # revision may repeat that suffix or begin with git. Neither is free text.
        suffixes = {"", release}
        if date:
            stamp = f"{date[:4]}-{date[4:6]}-{date[6:8]} {date[9:11]}:{date[11:13]}:{date[13:15]}Z"
            candidates = [
                human[len(prefix) :]
                for prefix in suffixes
                if human.startswith(prefix + "git ")
            ]
            valid = False
            for candidate in candidates:
                words = candidate.split(" ")
                if (
                    len(words) == 5
                    and re.fullmatch(r"[0-9a-f]{7,40}", words[1])
                    and sha.startswith(words[1])
                ):
                    valid = candidate == f"git {words[1]} {stamp} {bits}"
                    if valid:
                        break
            if not valid:
                return "unknown", False
        elif human not in {prefix + " " + bits for prefix in suffixes}:
            return "unknown", False
    numbers = tuple(int(match[name]) for name in ("major", "minor", "patch"))
    return normalised, numbers >= (1, 11, 3)


def cli_record(value: str, *, fs_cli: bool) -> str:
    """Remove only the proven command framing, never a second version record."""
    endings = ("\r\n\r\n", "\n\n", "\r\n", "\n") if fs_cli else ("\r\n", "\n")
    for ending in endings:
        if value.endswith(ending):
            return value[: -len(ending)]
    return value


def version_diagnostic(value: str) -> dict:
    """Print only bounded, version-shaped command output, with framing escaped."""
    safe = len(value) <= 640 and re.fullmatch(
        r"FreeSWITCH [Vv]ersion:? [0-9]+\.[0-9]+\.[0-9]+[A-Za-z0-9._~+() :/-]*[\r\n]*",
        value,
    )
    return {
        "stdout_bytes": len(value.encode()),
        "stdout": value if safe else "suppressed: unexpected version output",
    }


def validate(
    actual_version: str, guard: str, sofia_status: str, profiles: dict[str, str]
) -> dict:
    actual, patched = version(cli_record(actual_version, fs_cli=True))
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
        chat = settings[0].findall("param[@name='enable-chat']")
        chat_disabled = len(chat) == 1 and chat[0].get("value") == "false"
        if not patched and not chat_disabled:
            raise CheckError(
                "An affected or unknown PBX profile does not disable inbound SIP chat"
            )
        proto = settings[0].findall("param[@name='enable-chat-api-proto']")
        if patched and (len(proto) > 1 or (proto and proto[0].get("value") != "false")):
            raise CheckError(
                "A patched PBX profile unsafely enables or ambiguously configures the SIP chat API"
            )
        parsed[name] = {"disable_transfer": disabled, "disable_chat": chat_disabled}
    if not active.issubset(parsed):
        raise CheckError(
            "A loaded Sofia profile is missing from the validated on-disk configuration"
        )
    return {
        "running_version": actual,
        "guard_version": guarded,
        "version_secure": patched,
        "guard_transfer_enabled": guard_lines[1] == "true",
        "loaded_profiles": [{"name": name, **parsed[name]} for name in sorted(active)],
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


def version_command(container: str, *args: str) -> tuple[str | None, dict]:
    """Retain exit status and safe framing evidence before a proof can fail."""
    if args not in (("fs_cli", "-x", "version"), ("freeswitch", "-version")):
        raise CheckError("Unexpected PBX version command")
    try:
        result = subprocess.run(
            ["docker", "exec", container, *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.SubprocessError, OSError, UnicodeError):
        return None, {"exit_code": None, "stdout": "unavailable"}
    evidence = {
        **version_diagnostic(result.stdout),
        "exit_code": result.returncode,
        "stderr_bytes": len(result.stderr.encode()),
    }
    return (
        result.stdout
        if result.returncode == 0 and len(result.stdout) <= 1_000_000
        else None
    ), evidence


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container", required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", args.container):
        raise SystemExit("Invalid CI container identity")
    try:
        actual, cli_evidence = version_command(
            args.container, "fs_cli", "-x", "version"
        )
        _, binary_evidence = version_command(args.container, "freeswitch", "-version")
        print(
            "PBX version command evidence: "
            + json.dumps(
                {"fs_cli": cli_evidence, "binary": binary_evidence}, sort_keys=True
            ),
            file=sys.stderr,
        )
        if actual is None:
            raise CheckError("The bounded running PBX version command failed")
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
        proof = validate(actual, guard, status, profiles)
        print("PBX transfer mitigation: " + json.dumps(proof, sort_keys=True))
    except CheckError as error:
        raise SystemExit(str(error)) from None


if __name__ == "__main__":
    main()
