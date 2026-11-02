"""Review locks must not deadlock with the ordinary configuration editors."""

from __future__ import annotations

import concurrent.futures
import threading
import time

import psycopg
import pytest

from exaconnect_controller import db, internet
from exaconnect_controller.ai import act

from .conftest import DB_URL
from .test_flow import _seed


def test_row_locking_editor_returns_a_review_conflict_without_partial_locks(client):
    cid = _seed()["customer_id"]
    with psycopg.connect(DB_URL) as editor:
        editor.execute("SELECT id FROM sites WHERE customer_id = %s FOR UPDATE", (cid,))
        with db.tx() as assistant:
            assistant.execute("SET LOCAL lock_timeout = '1s'")
            with pytest.raises(act.ApprovalError, match="Configuration is being edited"):
                act._lock_configuration(assistant)
            # The failed NOWAIT statement is rolled back to a savepoint, so
            # the caller can read again and the editor can finish its write.
            assert assistant.execute("SELECT 1 AS ok").fetchone()["ok"] == 1
            editor.execute("SET LOCAL lock_timeout = '1s'")
            editor.execute("UPDATE customers SET name = name WHERE id = %s", (cid,))
            editor.commit()
            act._lock_configuration(assistant)


def test_internet_editor_can_finish_while_review_waits_for_the_policy_lock(client):
    cid = _seed()["customer_id"]
    policy_requested = threading.Event()
    reviewer: dict[str, int] = {}

    def review():
        with db.tx() as assistant:
            assistant.execute("SET LOCAL lock_timeout = '2s'")
            reviewer["pid"] = assistant.info.backend_pid
            policy_requested.set()
            act._lock_configuration(assistant)

    with psycopg.connect(DB_URL) as editor, concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        internet.lock_policy(editor)
        future = executor.submit(review)
        assert policy_requested.wait(timeout=2)
        deadline = time.monotonic() + 1
        while not editor.execute(
            "SELECT 1 FROM pg_locks WHERE pid = %s AND locktype = 'advisory' AND NOT granted",
            (reviewer["pid"],),
        ).fetchone():
            assert time.monotonic() < deadline, "Review did not wait on the editor's policy lock"
            time.sleep(0.01)
        # The editor already owns internet_policy. Review must wait for that
        # lock before claiming any configuration table locks, so its ordinary
        # write can finish. Reversed order raises DeadlockDetected here.
        editor.execute("SET LOCAL lock_timeout = '1s'")
        editor.execute("UPDATE customers SET name = name WHERE id = %s", (cid,))
        editor.commit()
        future.result(timeout=3)
