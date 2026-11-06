"""The upgrade preparation cannot change services or accept mutable image tags."""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

PATH = Path(__file__).resolve().parents[2] / "deploy/freeswitch/prepare_upgrade.py"
SPEC = importlib.util.spec_from_file_location("phone_upgrade_candidate", PATH)
candidate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(candidate)


@pytest.mark.parametrize("image", ["sha256:" + "a" * 64, "registry.example/phone@sha256:" + "b" * 64])
def test_candidate_changes_only_one_image_and_prohibits_pulling(image):
    assert candidate.candidate(image) == {"services": {"freeswitch": {"image": image, "pull_policy": "never"}}}


@pytest.mark.parametrize("image", ["latest", "freeswitch:1.11.3", "sha256:abc", "--pull", "x\nsha256:" + "a" * 64])
def test_mutable_malformed_or_command_like_input_is_refused(image):
    with pytest.raises(ValueError):
        candidate.candidate(image)


def test_unarmed_cli_requires_an_explicit_image_and_never_prints_a_default_override():
    result = subprocess.run([sys.executable, str(PATH)], capture_output=True, text=True, timeout=5)
    assert result.returncode == 2 and not result.stdout
