"""Bounded grammar for observed FreeSWITCH release output, never image tags.

The standalone deployment checker and BusyBox guard mirror this adapter. Their
shared fixture matrix tests all three, including official package build output.
Source: signalwire/freeswitch build/print_git_revision.c, switch_version.h.template,
src/switch.c and mod_commands.c. Unknown/custom/prerelease formats fail closed.
"""

from __future__ import annotations

import re

VERSION = re.compile(
    r"(?P<major>0|[1-9][0-9]{0,2})\.(?P<minor>0|[1-9][0-9]{0,2})\."
    r"(?P<patch>0|[1-9][0-9]{0,5})"
    r"(?P<release>-release(?:-[0-9]{1,20}-[0-9a-f]{7,40})?)?"
    r"(?:\+git~(?P<date>20[0-9]{6}T[0-9]{6}Z)~(?P<sha>[0-9a-f]{7,40}))?"
    r"(?:~(?P<bits>32bit|64bit))?"
)


def version(value: str) -> tuple[str, bool]:
    """Return the validated version text and patched-release status."""
    lines = value.splitlines()
    if len(lines) != 1 or len(lines[0]) > 320:
        return "unknown", False
    normalised = re.sub(r"^FreeSWITCH [Vv]ersion:? ", "", lines[0])
    compact, separator, human = normalised.partition(" (")
    if separator:
        if not human.endswith(")") or "(" in human or ")" in human[:-1]:
            return "unknown", False
        human = human[:-1]
    match = VERSION.fullmatch(compact)
    if match is None:
        return "unknown", False
    release, date, sha, bits = (match[name] or "" for name in ("release", "date", "sha", "bits"))
    if date:
        # Keep this date check identical to the portable awk adapter (2000-2099).
        year, month, day = int(date[:4]), int(date[4:6]), int(date[6:8])
        if not 1 <= month <= 12:
            return "unknown", False
        days = (31, 29 if year % 4 == 0 else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
        if not 1 <= day <= days[month - 1] or int(date[9:11]) > 23 or int(date[11:13]) > 59 or int(date[13:15]) > 59:
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
            candidates = [human[len(prefix) :] for prefix in suffixes if human.startswith(prefix + "git ")]
            valid = False
            for candidate in candidates:
                words = candidate.split(" ")
                if len(words) == 5 and re.fullmatch(r"[0-9a-f]{7,40}", words[1]) and sha.startswith(words[1]):
                    valid = candidate == f"git {words[1]} {stamp} {bits}"
                    if valid:
                        break
            if not valid:
                return "unknown", False
        elif human not in {prefix + " " + bits for prefix in suffixes}:
            return "unknown", False
    numbers = tuple(int(match[name]) for name in ("major", "minor", "patch"))
    return normalised, numbers >= (1, 11, 2)
