"""Backups: encrypted dump, then a restore into a scratch database with row counts compared."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from .commai_helpers import business
from .conftest import DB_URL

SCRIPTS = Path(__file__).resolve().parents[2] / "deploy" / "backup"
PASS = "a test passphrase, not a real one"

pytestmark = pytest.mark.skipif(
    not (shutil.which("pg_dump") and shutil.which("pg_restore") and shutil.which("psql") and shutil.which("openssl")),
    reason="needs pg_dump, pg_restore, psql and openssl",
)


def _run(script: str, *args: str, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run([str(SCRIPTS / script), *args], env=env, capture_output=True, text=True, timeout=300)


def test_backup_then_restore_test(client, tmp_path):
    business(client)  # some rows worth restoring
    env = {
        **os.environ,
        "EXA_BACKUP_DIR": str(tmp_path / "backups"),
        "EXA_BACKUP_PASSPHRASE": PASS,
        "EXA_BACKUP_DATABASE_URL": DB_URL,
        "EXA_BACKUP_KEEP_DAYS": "14",
    }
    env.pop("EXA_BACKUP_COMPOSE_FILE", None)
    env.pop("EXA_BACKUP_RCLONE_REMOTE", None)

    r = _run("backup.sh", env=env)
    assert r.returncode == 0, r.stderr + r.stdout
    out = Path(r.stdout.strip().splitlines()[-1])
    assert out.exists() and out.with_name(out.name + ".sha256").exists()
    counts = out.with_name(out.name + ".counts").read_text()
    assert "users=" in counts and "customers=1" in counts
    blob = out.read_bytes()
    assert blob.startswith(b"Salted__") and b"password_hash" not in blob and b"PGDMP" not in blob
    assert oct(out.stat().st_mode & 0o777) == "0o600"

    r = _run("restore-test.sh", env=env)
    assert r.returncode == 0, r.stderr + r.stdout
    assert "restore test passed" in r.stdout and "ok    users" in r.stdout

    # A wrong count is caught.
    manifest = out.with_name(out.name + ".counts")
    manifest.write_text(counts.replace("customers=1", "customers=2"))
    r = _run("restore-test.sh", str(out), env=env)
    assert r.returncode == 1 and "FAIL  customers backup=2 restored=1" in r.stdout

    # The wrong passphrase can't read it; restoring over a live database is refused.
    r = _run("restore-test.sh", str(out), env={**env, "EXA_BACKUP_PASSPHRASE": "the wrong passphrase!!"})
    assert r.returncode != 0 and "could not decrypt" in r.stderr
    db_name = DB_URL.split("?")[0].rsplit("/", 1)[1]
    r = _run("restore.sh", str(out), db_name, env=env)
    assert r.returncode != 0 and "is not empty" in r.stderr


def test_backup_needs_a_passphrase(tmp_path):
    env = {**os.environ, "EXA_BACKUP_DIR": str(tmp_path), "EXA_BACKUP_DATABASE_URL": DB_URL or "postgresql://x/y"}
    env.pop("EXA_BACKUP_PASSPHRASE", None)
    r = _run("backup.sh", env=env)
    assert r.returncode != 0 and "EXA_BACKUP_PASSPHRASE" in r.stderr
