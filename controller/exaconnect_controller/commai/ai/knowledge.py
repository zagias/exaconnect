"""Knowledge base: the business's approved content with its sources (ADR 0019).

A source (a page, a policy, a price list) is split into chunks. Only approved
sources are searched; editing a source withdraws its approval until someone
approves it again. Retrieval sits behind `Retriever`, so pgvector (or any
other search) can replace Postgres full-text search later without touching
the agent.

When the AI can't answer from knowledge (nothing relevant, or sources that
disagree) it escalates and the question is recorded as a knowledge gap, so
the business sees what to write next.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Protocol

import psycopg

from .. import events

events.register("knowledge.gap")

CHUNK_CHARS = 900
# A chunk is relevant when it covers at least this share of the question's terms.
MIN_COVERAGE = 0.5


@dataclass
class Hit:
    chunk_id: int
    source_id: str
    title: str
    source_url: str
    text: str
    rank: float
    coverage: float

    def as_dict(self) -> dict:
        return {
            "chunk_id": self.chunk_id,
            "source_id": self.source_id,
            "title": self.title,
            "source_url": self.source_url,
            "text": self.text,
            "coverage": round(self.coverage, 2),
        }


class Retriever(Protocol):
    def search(self, conn: psycopg.Connection, customer_id: Any, query: str, limit: int = 5) -> list[Hit]: ...


class PgFullTextRetriever:
    """Postgres full-text search over approved chunks (GIN index on a tsvector).
    Matches any of the question's terms, then orders by how many of them a
    chunk covers and by rank."""

    def search(self, conn: psycopg.Connection, customer_id: Any, query: str, limit: int = 5) -> list[Hit]:
        if not query.strip():
            return []
        rows = conn.execute(
            """WITH q AS (
                 SELECT lex, (SELECT string_agg(quote_literal(l), ' | ') FROM unnest(lex) l
                         WHERE position(chr(92) in l) = 0)::tsquery AS tsq
                 FROM (SELECT array(SELECT DISTINCT unnest(tsvector_to_array(to_tsvector('english', %(q)s))))
                       AS lex) x)
               SELECT k.id, k.source_id, k.text, s.title, s.source_url,
                      ts_rank_cd(k.tsv, q.tsq) AS rank,
                      (SELECT count(*) FROM unnest(q.lex) l WHERE l = ANY(tsvector_to_array(k.tsv)))::float
                        / greatest(cardinality(q.lex), 1) AS coverage
               FROM knowledge_chunks k JOIN knowledge_sources s ON s.id = k.source_id, q
               WHERE k.customer_id = %(c)s AND s.approved AND q.tsq IS NOT NULL AND k.tsv @@ q.tsq
               ORDER BY coverage DESC, rank DESC, k.id LIMIT %(n)s""",
            {"q": query[:2000], "c": customer_id, "n": limit},
        ).fetchall()
        return [
            Hit(r["id"], str(r["source_id"]), r["title"], r["source_url"], r["text"], r["rank"], r["coverage"])
            for r in rows
        ]


retriever: Retriever = PgFullTextRetriever()


# ---- sources ------------------------------------------------------------------------


def chunk(body: str, size: int = CHUNK_CHARS) -> list[str]:
    """Split on paragraphs, then sentences, into chunks of about `size` characters."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", body or "") if p.strip()]
    pieces: list[str] = []
    for p in paras:
        if len(p) <= size:
            pieces.append(p)
            continue
        cur = ""
        for s in re.split(r"(?<=[.!?])\s+", p):
            if cur and len(cur) + len(s) + 1 > size:
                pieces.append(cur)
                cur = ""
            cur = f"{cur} {s}".strip()
        if cur:
            pieces.append(cur)
    out: list[str] = []
    for p in pieces:
        if out and len(out[-1]) + len(p) + 2 <= size // 2:
            out[-1] = out[-1] + "\n\n" + p  # keep very short paragraphs together
        else:
            out.append(p)
    return out


def _write_chunks(conn: psycopg.Connection, source: dict) -> int:
    conn.execute("DELETE FROM knowledge_chunks WHERE source_id = %s", (source["id"],))
    parts = chunk(source["body"])
    for i, text in enumerate(parts):
        conn.execute(
            """INSERT INTO knowledge_chunks (customer_id, source_id, ordinal, heading, text)
               VALUES (%s, %s, %s, %s, %s)""",
            (source["customer_id"], source["id"], i, source["title"], text),
        )
    return len(parts)


def _check(title: str, body: str, source_url: str) -> None:
    if not title.strip() or len(title) > 200:
        raise ValueError("Give the source a title of 1 to 200 characters.")
    if not body.strip():
        raise ValueError("The source has no text.")
    if len(body) > 200_000:
        raise ValueError("A source can hold up to 200,000 characters; split it into several.")
    if source_url and not re.match(r"^https?://", source_url):
        raise ValueError("The source link must start with http:// or https://.")


def add_source(
    conn: psycopg.Connection,
    customer_id: Any,
    *,
    title: str,
    body: str,
    source_url: str = "",
    approved: bool = False,
    created_by: str = "",
) -> dict:
    """Add business content. It is used by the AI only once approved.
    Returns the source row with `chunks` (how many pieces it was split into)."""
    _check(title, body, source_url)
    row = conn.execute(
        """INSERT INTO knowledge_sources (customer_id, title, body, source_url, approved, approved_by, approved_at,
                                          created_by)
           VALUES (%s, %s, %s, %s, %s, %s, CASE WHEN %s THEN now() END, %s) RETURNING *""",
        (
            customer_id,
            title.strip(),
            body.strip(),
            source_url.strip(),
            approved,
            created_by if approved else "",
            approved,
            created_by,
        ),
    ).fetchone()
    return {**row, "chunks": _write_chunks(conn, row)}


def update_source(
    conn: psycopg.Connection,
    customer_id: Any,
    source_id: Any,
    *,
    title: str | None = None,
    body: str | None = None,
    source_url: str | None = None,
) -> dict | None:
    """Edit a source. A changed source needs approving again."""
    cur = get_source(conn, customer_id, source_id)
    if cur is None:
        return None
    t = cur["title"] if title is None else title
    b = cur["body"] if body is None else body
    u = cur["source_url"] if source_url is None else source_url
    _check(t, b, u)
    row = conn.execute(
        """UPDATE knowledge_sources SET title = %s, body = %s, source_url = %s, approved = false, approved_by = '',
                  approved_at = NULL, updated_at = now()
           WHERE id = %s RETURNING *""",
        (t.strip(), b.strip(), u.strip(), source_id),
    ).fetchone()
    return {**row, "chunks": _write_chunks(conn, row)}


def set_approved(conn: psycopg.Connection, customer_id: Any, source_id: Any, approved: bool, actor: str) -> dict | None:
    return conn.execute(
        """UPDATE knowledge_sources SET approved = %s, approved_by = CASE WHEN %s THEN %s ELSE '' END,
                  approved_at = CASE WHEN %s THEN now() END, updated_at = now()
           WHERE id = %s AND customer_id = %s RETURNING *""",
        (approved, approved, actor, approved, source_id, customer_id),
    ).fetchone()


def get_source(conn: psycopg.Connection, customer_id: Any, source_id: Any) -> dict | None:
    return conn.execute(
        "SELECT * FROM knowledge_sources WHERE id = %s AND customer_id = %s", (source_id, customer_id)
    ).fetchone()


def list_sources(conn: psycopg.Connection, customer_id: Any) -> list[dict]:
    return conn.execute(
        """SELECT s.id, s.title, s.source_url, s.approved, s.approved_by, s.approved_at, s.created_by,
                  s.created_at, s.updated_at, length(s.body) AS characters,
                  (SELECT count(*) FROM knowledge_chunks k WHERE k.source_id = s.id) AS chunks
           FROM knowledge_sources s WHERE s.customer_id = %s ORDER BY s.approved, s.title""",
        (customer_id,),
    ).fetchall()


def delete_source(conn: psycopg.Connection, customer_id: Any, source_id: Any) -> dict | None:
    return conn.execute(
        "DELETE FROM knowledge_sources WHERE id = %s AND customer_id = %s RETURNING id, title",
        (source_id, customer_id),
    ).fetchone()


# ---- retrieval with checks ----------------------------------------------------------

_FIGURE = re.compile(r"\b\d+(?:[:.]\d+)?\s*(?:am|pm|%)?", re.I)


def _figures(text: str) -> set[str]:
    return {re.sub(r"\s+", "", m.group(0).lower()) for m in _FIGURE.finditer(text)}


def _relevant(conn: psycopg.Connection, customer_id: Any, text: str, limit: int) -> list[Hit]:
    return [h for h in retriever.search(conn, customer_id, text, limit) if h.coverage >= MIN_COVERAGE]


def lookup(conn: psycopg.Connection, customer_id: Any, question: str, limit: int = 4) -> dict:
    """{"hits": [...relevant hits...], "contradictory": bool, "contradiction": str}.
    Two relevant chunks from different sources that both state figures (times,
    prices, numbers) and share none of them are treated as a contradiction."""
    hits = _relevant(conn, customer_id, question, limit)
    if not hits:
        # A message often holds several things ("I prefer email. When are you
        # open?"): try each sentence on its own, questions first.
        parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+|\n+", question) if len(p.strip()) > 3]
        parts.sort(key=lambda p: not p.endswith("?"))
        seen: set[int] = set()
        for p in parts[:6] if len(parts) > 1 else []:
            for h in _relevant(conn, customer_id, p, limit):
                if h.chunk_id not in seen:
                    seen.add(h.chunk_id)
                    hits.append(h)
            if hits:
                break
    contradictory, why = False, ""
    if len(hits) > 1:
        best = hits[0]
        for other in hits[1:]:
            if other.source_id == best.source_id or other.coverage < best.coverage - 0.15:
                continue
            a, b = _figures(best.text), _figures(other.text)
            if a and b and not (a & b):
                contradictory = True
                why = f'"{best.title}" says {", ".join(sorted(a))}; "{other.title}" says {", ".join(sorted(b))}.'
                break
    return {"hits": [h.as_dict() for h in hits], "contradictory": contradictory, "contradiction": why}


# ---- gaps ---------------------------------------------------------------------------


def _key(question: str) -> str:
    norm = " ".join(re.findall(r"[^\W_]+", question.lower()))
    return hashlib.sha256(norm.encode()).hexdigest()[:32]


def record_gap(
    conn: psycopg.Connection,
    customer_id: Any,
    question: str,
    reason: str,
    *,
    detail: str = "",
    conversation_id: Any = None,
) -> dict:
    question = " ".join(question.split())[:500] or "(empty question)"
    row = conn.execute(
        """INSERT INTO knowledge_gaps (customer_id, question_key, question, reason, detail, conversation_id)
           VALUES (%s, %s, %s, %s, %s, %s)
           ON CONFLICT (customer_id, question_key) DO UPDATE SET times = knowledge_gaps.times + 1,
             last_seen = now(), reason = EXCLUDED.reason, detail = EXCLUDED.detail, status = 'open',
             conversation_id = EXCLUDED.conversation_id
           RETURNING *""",
        (customer_id, _key(question), question, reason, detail[:1000], conversation_id),
    ).fetchone()
    events.emit(
        conn,
        customer_id,
        "knowledge.gap",
        {"gap_id": str(row["id"]), "reason": reason, "times": row["times"]},
        row["id"],
    )
    return row


def list_gaps(conn: psycopg.Connection, customer_id: Any, status: str = "open") -> list[dict]:
    return conn.execute(
        """SELECT id, question, reason, detail, conversation_id, times, status, resolved_by, first_seen, last_seen
           FROM knowledge_gaps WHERE customer_id = %s AND (%s = 'all' OR status = %s)
           ORDER BY times DESC, last_seen DESC LIMIT 200""",
        (customer_id, status, status),
    ).fetchall()


def resolve_gap(conn: psycopg.Connection, customer_id: Any, gap_id: Any, actor: str) -> dict | None:
    return conn.execute(
        """UPDATE knowledge_gaps SET status = 'resolved', resolved_by = %s WHERE id = %s AND customer_id = %s
           RETURNING *""",
        (actor, gap_id, customer_id),
    ).fetchone()
