"""Unavailable readers and deferred attachment writes, without external services."""

from types import SimpleNamespace

import pytest

from exaconnect_controller.commai.ai import attachments


@pytest.fixture(autouse=True)
def disconnected(monkeypatch):
    monkeypatch.delenv("EXA_COMMAI_AI_TEST_MODE", raising=False)
    monkeypatch.delenv("EXA_COMMAI_VISION", raising=False)
    monkeypatch.setattr(attachments, "_speech", None)
    monkeypatch.setattr(attachments, "_vision", None)
    monkeypatch.setattr(attachments.voice, "server_speech", lambda: None)


@pytest.mark.parametrize("setting", [None, "0", "true"])
@pytest.mark.parametrize("reader", [attachments.speech, attachments.vision])
def test_missing_reader_never_substitutes_simulation(monkeypatch, setting, reader):
    monkeypatch.setenv("EXA_ENV", "lab")
    if setting is not None:
        monkeypatch.setenv("EXA_COMMAI_AI_TEST_MODE", setting)
    with pytest.raises(attachments.AttachmentError, match="not connected") as error:
        reader()
    assert error.value.code == 503


def test_simulation_requires_explicit_test_flag(monkeypatch):
    monkeypatch.setenv("EXA_COMMAI_AI_TEST_MODE", "1")
    assert attachments.speech().transcribe(b"OggS SIMTEXT:test words", "audio/ogg") == "test words"
    assert isinstance(attachments.vision(), attachments.SimulatedVision)


def test_connected_speech_remains_available_without_test_mode(monkeypatch):
    live = SimpleNamespace(live=True, name="connected-speech")
    monkeypatch.setattr(attachments.voice, "server_speech", lambda: live)
    assert attachments.speech() is live


class Result:
    def __init__(self, row=None, rows=None):
        self.row, self.rows = row, rows or []

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows


class Files:
    def __init__(self, *, audio=False, cached=None):
        self.file = {
            "id": "file-1",
            "name": "note.ogg" if audio else "note.txt",
            "content_type": "audio/ogg" if audio else "text/plain",
            "data": b"OggS SIMTEXT:fictional transcript" if audio else b"actual file text",
        }
        self.cached = cached
        self.writes = []

    def execute(self, sql, params):
        if sql.startswith("SELECT * FROM attachment_readings"):
            return Result(self.cached)
        if sql.startswith("SELECT id, name, content_type, data"):
            return Result(self.file)
        if sql.startswith("INSERT INTO attachment_readings"):
            self.writes.append((sql, params))
            return Result({"file_id": self.file["id"]} if self.file else None)
        raise AssertionError(f"Unexpected query: {sql}")


def message():
    return {"id": "message-1", "attachments": [{"id": "file-1", "name": "note"}]}


def test_attachment_cache_waits_for_publication_phase():
    conn = Files()
    files = attachments.for_message(conn, "tenant-1", message(), defer_cache=True)
    assert files["files"] == [{"name": "note", "kind": "text", "text": "actual file text"}]
    assert conn.writes == []
    assert len(files["pending"]) == 1
    attachments.persist_pending(conn, files)
    assert len(conn.writes) == 1
    assert conn.writes[0][1][-2:] == ("file-1", "tenant-1")
    assert "pending" not in files
    attachments.persist_pending(conn, files)
    assert len(conn.writes) == 1


def test_erased_file_cannot_be_recreated_by_deferred_cache():
    conn = Files()
    files = attachments.for_message(conn, "tenant-1", message(), defer_cache=True)
    conn.file = None
    with pytest.raises(attachments.AttachmentError, match="no longer available") as error:
        attachments.persist_pending(conn, files)
    assert error.value.code == 409


def test_cached_simulated_voice_is_not_reused_in_production():
    conn = Files(
        audio=True,
        cached={
            "file_id": "file-1",
            "reader": "simulated-speech",
            "status": "read",
            "kind": "voice",
            "text": "fictional transcript",
        },
    )
    files = attachments.for_message(conn, "tenant-1", message(), defer_cache=True)
    assert files["transcript"] == ""
    assert "not connected" in files["unreadable"][0]
    assert files["pending"][0]["status"] == "refused"
    assert conn.writes == []


def test_old_simulated_readings_are_excluded_from_history_and_staff_claims():
    rows = [
        {"file_id": "file-1", "reader": "simulated-speech", "text": "fictional", "status": "read"},
        {"file_id": "file-2", "reader": "connected-speech", "text": "real transcript", "status": "read"},
    ]
    conn = SimpleNamespace(execute=lambda *_: Result(rows=rows))
    messages = [message(), {"id": "message-2", "attachments": [{"id": "file-2"}]}]
    assert attachments.transcripts(conn, "tenant-1", messages) == {"message-2": "real transcript"}
    exposed = attachments.readings(conn, "tenant-1", "conversation-1")
    assert exposed[0]["status"] == "refused" and exposed[0]["text"] == ""
    assert exposed[1] == rows[1]
