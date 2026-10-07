"""What the AI reads from attachments and voice notes (ADR 0026).

A file a customer sends (website chat today; channel_files) is read once and
the result kept in attachment_readings:

- plain text: decoded as UTF-8;
- PDF: the text in its content streams, pulled out by a small parser here
  (no PDF library, nothing is rendered or executed);
- images: described by a vision model behind `Vision` (simulated without an
  AI service: it reports the format and size and says it can't see the
  picture);
- voice notes: transcribed by the speech interface (`voice.SpeechProvider`).
  Server speech is not live (ADR 0019), so `SimulatedSpeech` answers unless
  EXA_COMMAI_SERVER_SPEECH is on.

Safe handling: types are decided by sniffing the bytes, never by the name or
the declared type alone; the declared type must match; executables, archives,
HTML and SVG are refused; size limits per kind; files live in the database,
never under a web root; nothing is ever executed; decompression is capped.
A voice note's transcript becomes the customer's words for the AI. Other files
are given to the AI as data in the context ("attachments"), never as
instructions.
"""

from __future__ import annotations

import os
import re
import struct
import zlib
from typing import Any, Protocol

import psycopg

from ..channels import widget
from . import voice

MAX_TEXT_CHARS = 8000
LIMITS = {"text": 1 * 1024 * 1024, "pdf": 5 * 1024 * 1024, "image": 5 * 1024 * 1024, "voice": 5 * 1024 * 1024}

# mime -> kind
KINDS = {
    "text/plain": "text",
    "text/csv": "text",
    "application/pdf": "pdf",
    "image/png": "image",
    "image/jpeg": "image",
    "image/gif": "image",
    "image/webp": "image",
    "audio/ogg": "voice",
    "audio/webm": "voice",
    "audio/mpeg": "voice",
    "audio/wav": "voice",
    "audio/mp4": "voice",
}

# Voice notes from the website chat: the widget accepts these too, checked by
# the same magic numbers (website visitors record webm or ogg).
widget.FILE_TYPES.update(
    {
        "audio/ogg": [b"OggS"],
        "audio/webm": [b"\x1a\x45\xdf\xa3"],
        "audio/mpeg": [b"ID3", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"],
        "audio/wav": [b"RIFF"],
    }
)

DANGEROUS = (
    (b"MZ", "a Windows program"),
    (b"\x7fELF", "a program"),
    (b"#!", "a script"),
    (b"PK\x03\x04", "an archive"),
    (b"Rar!", "an archive"),
    (b"\x1f\x8b", "an archive"),
    (b"7z\xbc\xaf", "an archive"),
    (b"\xca\xfe\xba\xbe", "a program"),
    (b"\xcf\xfa\xed\xfe", "a program"),
)


class AttachmentError(Exception):
    def __init__(self, message: str, code: int = 415):
        super().__init__(message)
        self.code = code


def sniff(data: bytes) -> str | None:
    """The type the bytes really are, or None when unknown."""
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"RIFF") and data[8:12] == b"WAVE":
        return "audio/wav"
    if data.startswith(b"OggS"):
        return "audio/ogg"
    if data.startswith(b"\x1a\x45\xdf\xa3"):
        return "audio/webm"
    if data.startswith(b"ID3") or data[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return "audio/mpeg"
    if data[4:8] == b"ftyp":
        return "audio/mp4"
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    head = text.lstrip()[:200].lower()
    if head.startswith(("<!doctype", "<html", "<svg", "<?xml", "<script")) or "<script" in text[:4096].lower():
        return None  # markup can carry script: never accepted as text
    if "\x00" in text:
        return None
    return "text/plain"


def check(name: str, declared: str, data: bytes, max_bytes: int | None = None) -> str:
    """Refuse anything unsafe; return the sniffed type."""
    if not data:
        raise AttachmentError("The file is empty.", 422)
    if not name or len(name) > 120 or "/" in name or "\\" in name or name.startswith("."):
        raise AttachmentError("Give the file a short name.", 422)
    for magic, what in DANGEROUS:
        if data.startswith(magic):
            raise AttachmentError(f"The file is {what}; only documents, images and voice notes are accepted.")
    real = sniff(data)
    if real is None:
        raise AttachmentError("You can send text, CSV, PDF, images (PNG, JPEG, GIF, WebP) and voice notes.")
    declared = (declared or "").split(";")[0].strip().lower()
    if declared == "text/csv" and real == "text/plain":
        real = "text/csv"
    if declared and declared != real and not (declared == "audio/x-wav" and real == "audio/wav"):
        raise AttachmentError("The file's contents don't match its type.")
    limit = max_bytes or LIMITS[KINDS[real]]
    if len(data) > limit:
        raise AttachmentError(f"Files of this kind can be up to {limit // (1024 * 1024)} MB.", 413)
    return real


# ---- PDF text -----------------------------------------------------------------------------

_STREAM = re.compile(rb"<<(.*?)>>\s*stream\r?\n(.*?)\r?\nendstream", re.S)
_TEXT_OP = re.compile(rb"\((?:\\.|[^\\)])*\)\s*(?:Tj|'|\")|\[(?:[^\]]*)\]\s*TJ|T\*|ET", re.S)
_STRING = re.compile(rb"\((?:\\.|[^\\)])*\)")
_ESC = {b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b", b"f": b"\f", b"(": b"(", b")": b")", b"\\": b"\\"}
MAX_INFLATED = 4 * 1024 * 1024


def _unescape(s: bytes) -> str:
    out = bytearray()
    i = 0
    while i < len(s):
        c = s[i : i + 1]
        if c == b"\\" and i + 1 < len(s):
            n = s[i + 1 : i + 2]
            if n in _ESC:
                out += _ESC[n]
                i += 2
                continue
            m = re.match(rb"[0-7]{1,3}", s[i + 1 : i + 4])
            if m:
                out.append(int(m.group(0), 8) & 0xFF)
                i += 1 + len(m.group(0))
                continue
            i += 1
            continue
        out += c
        i += 1
    return out.decode("latin-1")


def pdf_text(data: bytes) -> str:
    """Text shown by a PDF's content streams (uncompressed or Flate), read as
    data only. Good for simple text PDFs; scanned pages have no text."""
    parts: list[str] = []
    budget = MAX_INFLATED
    for m in _STREAM.finditer(data):
        head, raw = m.group(1), m.group(2)
        if b"/FlateDecode" in head:
            d = zlib.decompressobj()
            try:
                raw = d.decompress(raw, budget)
            except zlib.error:
                continue
            budget -= len(raw)
        elif b"/Filter" in head:
            continue  # images and other encodings: not text
        if budget <= 0:
            break
        line: list[str] = []
        for op in _TEXT_OP.finditer(raw):
            tok = op.group(0)
            if tok in (b"T*", b"ET"):
                if line:
                    parts.append("".join(line))
                    line = []
                continue
            line.extend(_unescape(s.group(0)[1:-1]) for s in _STRING.finditer(tok))
        if line:
            parts.append("".join(line))
        if sum(len(p) for p in parts) > MAX_TEXT_CHARS:
            break
    text = "\n".join(" ".join(p.split()) for p in parts if p.strip())
    return text[:MAX_TEXT_CHARS]


# ---- images -------------------------------------------------------------------------------


def image_size(data: bytes, mime: str) -> tuple[int, int] | None:
    try:
        if mime == "image/png" and len(data) >= 24:
            return struct.unpack(">II", data[16:24])
        if mime == "image/gif" and len(data) >= 10:
            return struct.unpack("<HH", data[6:10])
        if mime == "image/jpeg":
            i = 2
            while i + 9 < len(data):
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if marker in (0xC0, 0xC1, 0xC2):
                    h, w = struct.unpack(">HH", data[i + 5 : i + 9])
                    return w, h
                i += 2 + struct.unpack(">H", data[i + 2 : i + 4])[0]
    except struct.error:
        return None
    return None


class Vision(Protocol):
    name: str
    live: bool

    def describe(self, data: bytes, mime: str) -> str: ...


class SimulatedVision:
    """Deterministic and honest: it reads the image's format and size, and says
    it cannot see what the picture shows."""

    name = "simulated-vision"
    live = False

    def describe(self, data: bytes, mime: str) -> str:
        fmt = mime.split("/")[1].upper()
        size = image_size(data, mime)
        dims = f", {size[0]} × {size[1]} pixels" if size else ""
        return f"An image ({fmt}{dims}). The simulated vision model can't see what it shows; a person should look."


class OpenAICompatibleVision:
    """A vision model on an OpenAI-compatible API. Not live: it needs
    EXA_COMMAI_VISION=on, EXA_VISION_MODEL and the AI key, and Dudley's yes to the spend."""

    name = "vision"

    def __init__(self, api_key: str, base_url: str, model: str):
        self.api_key, self.base_url, self.model = api_key, base_url.rstrip("/"), model
        self.live = bool(api_key and model)

    def describe(self, data: bytes, mime: str) -> str:  # pragma: no cover - needs a live service
        import base64
        import json
        import urllib.request

        body = {
            "model": self.model,
            "max_tokens": 300,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Describe this image for a customer-service agent in two sentences."},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime};base64,{base64.b64encode(data).decode()}"},
                        },
                    ],
                }
            ],
        }
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 (configured endpoint)
            return str(json.loads(r.read(1_000_000))["choices"][0]["message"]["content"])[:1000]


class SimulatedSpeech:
    """Speech-to-text stand-in for tests and the lab. Real audio has no words it
    can recognise; a test recording carries its words after a SIMTEXT: marker."""

    name = "simulated-speech"
    live = False

    def transcribe(self, audio: bytes, mime: str, language: str = "") -> str:
        i = audio.find(b"SIMTEXT:")
        if i < 0:
            return ""
        return audio[i + 8 : i + 8 + 2000].split(b"\x00")[0].decode("utf-8", "replace").strip()

    def synthesise(self, text: str, voice: str = "") -> bytes:
        raise voice_error("The simulated speech provider does not speak.")


def voice_error(msg: str) -> Exception:
    return voice.SpeechError(msg)


_vision: Vision | None = None
_speech: Any = None


def set_vision(v: Vision | None) -> None:
    global _vision
    _vision = v


def set_speech(s: Any) -> None:
    global _speech
    _speech = s


def vision() -> Vision:
    if _vision is not None:
        return _vision
    if os.environ.get("EXA_COMMAI_VISION", "").lower() in ("on", "1", "true", "yes"):
        from ...settings import get_settings

        s = get_settings()
        real = OpenAICompatibleVision(s.llm_api_key, s.llm_base_url, os.environ.get("EXA_VISION_MODEL", ""))
        if real.live:
            return real
    return SimulatedVision()


def speech() -> Any:
    if _speech is not None:
        return _speech
    return voice.server_speech() or SimulatedSpeech()


# ---- reading ------------------------------------------------------------------------------


def read_file(conn: psycopg.Connection, customer_id: Any, file_id: Any, *, again: bool = False) -> dict | None:
    """Read one customer file once (kept in attachment_readings)."""
    if not again:
        row = conn.execute(
            "SELECT * FROM attachment_readings WHERE file_id = %s AND customer_id = %s", (file_id, customer_id)
        ).fetchone()
        if row:
            return row
    f = conn.execute(
        "SELECT id, name, content_type, data FROM channel_files WHERE id = %s AND customer_id = %s",
        (file_id, customer_id),
    ).fetchone()
    if f is None:
        return None
    data = bytes(f["data"])
    kind, status, text, reason, reader = "other", "read", "", "", ""
    try:
        mime = check(f["name"], f["content_type"], data)
        kind = KINDS[mime]
        if kind == "text":
            text, reader = data.decode("utf-8")[:MAX_TEXT_CHARS], "utf-8"
        elif kind == "pdf":
            text, reader = pdf_text(data), "pdf-text"
            if not text:
                status, reason = "failed", "The PDF has no text the AI can read (it may be a scan)."
        elif kind == "image":
            v = vision()
            text, reader = v.describe(data, mime), v.name
        else:
            s = speech()
            text, reader = s.transcribe(data, mime), s.name
            if not text:
                status, reason = "failed", "No words could be made out in the voice note."
    except AttachmentError as e:
        status, reason = "refused", str(e)
    except Exception as e:  # noqa: BLE001 - a reader failing is recorded, the conversation goes on
        status, reason = "failed", f"The file could not be read ({type(e).__name__})."
    return conn.execute(
        """INSERT INTO attachment_readings (file_id, customer_id, kind, status, text, reason, reader)
           VALUES (%s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (file_id) DO UPDATE SET kind = EXCLUDED.kind, status = EXCLUDED.status, text = EXCLUDED.text,
             reason = EXCLUDED.reason, reader = EXCLUDED.reader, created_at = now()
           RETURNING *""",
        (f["id"], customer_id, kind, status, text[:MAX_TEXT_CHARS], reason[:300], reader),
    ).fetchone()


def _file_ids(message: dict) -> list[str]:
    return [str(a["id"]) for a in (message.get("attachments") or []) if isinstance(a, dict) and a.get("id")]


def for_message(conn: psycopg.Connection, customer_id: Any, message: dict) -> dict:
    """{"transcript": voice-note words, "files": [{name, kind, text}], "unreadable": [reasons]}"""
    out: dict = {"transcript": "", "files": [], "unreadable": []}
    names = {str(a.get("id")): a.get("name", "") for a in message.get("attachments") or [] if isinstance(a, dict)}
    words = []
    for fid in _file_ids(message):
        r = read_file(conn, customer_id, fid)
        if r is None:
            continue
        if r["status"] != "read":
            out["unreadable"].append(f"{names.get(fid) or 'A file'}: {r['reason']}")
            continue
        if r["kind"] == "voice":
            words.append(r["text"])
        else:
            out["files"].append({"name": names.get(fid, ""), "kind": r["kind"], "text": r["text"][:4000]})
    out["transcript"] = " ".join(words).strip()
    return out


def transcripts(conn: psycopg.Connection, customer_id: Any, messages: list[dict]) -> dict[str, str]:
    """Voice-note words per message id, for messages already read."""
    ids = [fid for m in messages for fid in _file_ids(m)]
    if not ids:
        return {}
    rows = {
        str(r["file_id"]): r["text"]
        for r in conn.execute(
            """SELECT file_id, text FROM attachment_readings WHERE customer_id = %s AND file_id = ANY(%s::uuid[])
               AND kind = 'voice' AND status = 'read'""",
            (customer_id, ids),
        ).fetchall()
    }
    out = {}
    for m in messages:
        words = " ".join(rows[f] for f in _file_ids(m) if f in rows).strip()
        if words:
            out[str(m["id"])] = words
    return out


def readings(conn: psycopg.Connection, customer_id: Any, conversation_id: Any) -> list[dict]:
    """What the AI read from each file in a conversation, for staff."""
    return conn.execute(
        """SELECT f.id AS file_id, f.name, f.content_type, f.size, r.kind, r.status, r.text, r.reason, r.reader,
                  r.created_at
           FROM channel_files f LEFT JOIN attachment_readings r ON r.file_id = f.id
           WHERE f.customer_id = %s AND f.conversation_id = %s ORDER BY f.created_at""",
        (customer_id, conversation_id),
    ).fetchall()
