"""SQLite-Cache fuer RIS-Suchen und -Dokumente.

Drei Tabellen:

* ``search_cache`` -- gecachte Suchergebnisse (kurze TTL).
* ``document_cache`` -- gecachte Volltexte (lange TTL, oft permanent).
* ``meta`` -- HTTP-ETag/Last-Modified pro URL + Statistiken.

Zusaetzlich eine FTS5-Virtual-Table auf den Dokument-Volltexten fuer das
``ris_local_search``-Tool.

Sqlite3 ist synchron; wir wrappen alle Schreib-/Lesezugriffe in
``asyncio.to_thread`` damit nichts den FastMCP-Eventloop blockiert.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS search_cache (
    key TEXT PRIMARY KEY,
    controller TEXT NOT NULL,
    application TEXT NOT NULL,
    params_json TEXT NOT NULL,
    response_json TEXT NOT NULL,
    fetched_at INTEGER NOT NULL,
    expires_at INTEGER
);

CREATE INDEX IF NOT EXISTS idx_search_expires ON search_cache(expires_at);

CREATE TABLE IF NOT EXISTS document_cache (
    doc_id TEXT PRIMARY KEY,
    source_url TEXT,
    content_type TEXT,
    raw_content BLOB,
    text_content TEXT,
    metadata_json TEXT,
    fetched_at INTEGER NOT NULL,
    expires_at INTEGER
);

CREATE INDEX IF NOT EXISTS idx_document_expires ON document_cache(expires_at);

CREATE TABLE IF NOT EXISTS meta (
    url TEXT PRIMARY KEY,
    etag TEXT,
    last_modified TEXT,
    last_status INTEGER,
    last_seen_at INTEGER NOT NULL
);

-- Standalone FTS5-Tabelle (kein external-content). Speichert ``text_content``
-- ein zweites Mal -- der Speicherbedarf ist es uns wert, dafuer halten die
-- Trigger zuverlaessig synchron.
CREATE VIRTUAL TABLE IF NOT EXISTS document_fts USING fts5(
    doc_id UNINDEXED,
    text_content,
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS document_fts_insert
AFTER INSERT ON document_cache
WHEN new.text_content IS NOT NULL BEGIN
    INSERT INTO document_fts(doc_id, text_content)
    VALUES (new.doc_id, new.text_content);
END;

CREATE TRIGGER IF NOT EXISTS document_fts_delete
AFTER DELETE ON document_cache BEGIN
    DELETE FROM document_fts WHERE doc_id = old.doc_id;
END;

CREATE TRIGGER IF NOT EXISTS document_fts_update
AFTER UPDATE OF text_content ON document_cache BEGIN
    DELETE FROM document_fts WHERE doc_id = old.doc_id;
    INSERT INTO document_fts(doc_id, text_content)
    SELECT new.doc_id, new.text_content WHERE new.text_content IS NOT NULL;
END;
"""


def _stable_key(controller: str, application: str, params: Mapping[str, Any]) -> str:
    """Deterministischen Cache-Schluessel aus Endpoint + Params bilden."""
    payload = {
        "controller": controller,
        "application": application,
        "params": {k: params[k] for k in sorted(params)},
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class Cache:
    """Async-Fassade um eine SQLite-Datei.

    Wir oeffnen *eine* Connection mit ``check_same_thread=False`` und
    serialisieren Schreibzugriffe ueber ein asyncio.Lock. SQLite ist in
    WAL-Mode parallel-lesefaehig, das reicht fuer unsere Last (ein User,
    Tool-Aufrufe sequenziell vom LLM).
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._conn: sqlite3.Connection | None = None
        self._write_lock = asyncio.Lock()

    # ------------------------------------------------------------------ lifecycle
    async def connect(self) -> None:
        """DB oeffnen, Schema initialisieren, PRAGMAs setzen."""
        await asyncio.to_thread(self._connect_sync)

    def _connect_sync(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            self.db_path,
            check_same_thread=False,
            isolation_level=None,  # autocommit; wir steuern Transaktionen selbst
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.executescript(_SCHEMA)

        current = conn.execute("PRAGMA user_version;").fetchone()[0]
        if current == 0:
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION};")
        elif current != SCHEMA_VERSION:
            logger.warning(
                "cache.schema_mismatch",
                expected=SCHEMA_VERSION,
                actual=current,
                hint="Migration noch nicht implementiert.",
            )
        self._conn = conn
        logger.info("cache.connected", path=str(self.db_path))

    async def close(self) -> None:
        if self._conn is not None:
            await asyncio.to_thread(self._conn.close)
            self._conn = None

    def _require_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("Cache not connected. Call connect() first.")
        return self._conn

    # ------------------------------------------------------------------ search
    async def get_search(
        self,
        controller: str,
        application: str,
        params: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        key = _stable_key(controller, application, params)
        return await asyncio.to_thread(self._get_search_sync, key)

    def _get_search_sync(self, key: str) -> dict[str, Any] | None:
        conn = self._require_conn()
        row = conn.execute(
            "SELECT response_json, expires_at FROM search_cache WHERE key = ?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        if row["expires_at"] is not None and row["expires_at"] < int(time.time()):
            conn.execute("DELETE FROM search_cache WHERE key = ?", (key,))
            return None
        return json.loads(row["response_json"])

    async def put_search(
        self,
        controller: str,
        application: str,
        params: Mapping[str, Any],
        response: Mapping[str, Any],
        ttl_seconds: int | None,
    ) -> None:
        key = _stable_key(controller, application, params)
        now = int(time.time())
        expires = now + ttl_seconds if ttl_seconds else None
        async with self._write_lock:
            await asyncio.to_thread(
                self._put_search_sync,
                key,
                controller,
                application,
                dict(params),
                dict(response),
                now,
                expires,
            )

    def _put_search_sync(
        self,
        key: str,
        controller: str,
        application: str,
        params: dict[str, Any],
        response: dict[str, Any],
        fetched_at: int,
        expires_at: int | None,
    ) -> None:
        conn = self._require_conn()
        conn.execute(
            """
            INSERT INTO search_cache
                (key, controller, application, params_json, response_json,
                 fetched_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                response_json = excluded.response_json,
                fetched_at    = excluded.fetched_at,
                expires_at    = excluded.expires_at
            """,
            (
                key,
                controller,
                application,
                json.dumps(params, ensure_ascii=False, sort_keys=True, default=str),
                json.dumps(response, ensure_ascii=False, default=str),
                fetched_at,
                expires_at,
            ),
        )

    # ------------------------------------------------------------------ documents
    async def get_document(self, doc_id: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._get_document_sync, doc_id)

    def _get_document_sync(self, doc_id: str) -> dict[str, Any] | None:
        conn = self._require_conn()
        row = conn.execute(
            """
            SELECT source_url, content_type, raw_content, text_content,
                   metadata_json, fetched_at, expires_at
            FROM document_cache WHERE doc_id = ?
            """,
            (doc_id,),
        ).fetchone()
        if row is None:
            return None
        if row["expires_at"] is not None and row["expires_at"] < int(time.time()):
            conn.execute("DELETE FROM document_cache WHERE doc_id = ?", (doc_id,))
            return None
        return {
            "doc_id": doc_id,
            "source_url": row["source_url"],
            "content_type": row["content_type"],
            "raw_content": row["raw_content"],
            "text_content": row["text_content"],
            "metadata": json.loads(row["metadata_json"]) if row["metadata_json"] else {},
            "fetched_at": row["fetched_at"],
        }

    async def put_document(
        self,
        doc_id: str,
        source_url: str | None,
        content_type: str | None,
        raw_content: bytes | None,
        text_content: str | None,
        metadata: Mapping[str, Any] | None,
        ttl_seconds: int | None,
    ) -> None:
        now = int(time.time())
        expires = now + ttl_seconds if ttl_seconds else None
        async with self._write_lock:
            await asyncio.to_thread(
                self._put_document_sync,
                doc_id,
                source_url,
                content_type,
                raw_content,
                text_content,
                dict(metadata) if metadata else {},
                now,
                expires,
            )

    def _put_document_sync(
        self,
        doc_id: str,
        source_url: str | None,
        content_type: str | None,
        raw_content: bytes | None,
        text_content: str | None,
        metadata: dict[str, Any],
        fetched_at: int,
        expires_at: int | None,
    ) -> None:
        conn = self._require_conn()
        conn.execute(
            """
            INSERT INTO document_cache
                (doc_id, source_url, content_type, raw_content, text_content,
                 metadata_json, fetched_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(doc_id) DO UPDATE SET
                source_url    = excluded.source_url,
                content_type  = excluded.content_type,
                raw_content   = excluded.raw_content,
                text_content  = excluded.text_content,
                metadata_json = excluded.metadata_json,
                fetched_at    = excluded.fetched_at,
                expires_at    = excluded.expires_at
            """,
            (
                doc_id,
                source_url,
                content_type,
                raw_content,
                text_content,
                json.dumps(metadata, ensure_ascii=False, default=str),
                fetched_at,
                expires_at,
            ),
        )

    # ------------------------------------------------------------------ http meta
    async def get_http_meta(self, url: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._get_http_meta_sync, url)

    def _get_http_meta_sync(self, url: str) -> dict[str, Any] | None:
        conn = self._require_conn()
        row = conn.execute(
            "SELECT etag, last_modified, last_status, last_seen_at FROM meta WHERE url = ?",
            (url,),
        ).fetchone()
        if row is None:
            return None
        return dict(row)

    async def put_http_meta(
        self,
        url: str,
        etag: str | None,
        last_modified: str | None,
        last_status: int,
    ) -> None:
        async with self._write_lock:
            await asyncio.to_thread(
                self._put_http_meta_sync, url, etag, last_modified, last_status
            )

    def _put_http_meta_sync(
        self,
        url: str,
        etag: str | None,
        last_modified: str | None,
        last_status: int,
    ) -> None:
        conn = self._require_conn()
        conn.execute(
            """
            INSERT INTO meta (url, etag, last_modified, last_status, last_seen_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(url) DO UPDATE SET
                etag           = excluded.etag,
                last_modified  = excluded.last_modified,
                last_status    = excluded.last_status,
                last_seen_at   = excluded.last_seen_at
            """,
            (url, etag, last_modified, last_status, int(time.time())),
        )

    # ------------------------------------------------------------------ local FTS
    async def fts_search(self, query: str, limit: int = 25) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._fts_search_sync, query, limit)

    def _fts_search_sync(self, query: str, limit: int) -> list[dict[str, Any]]:
        conn = self._require_conn()
        rows = conn.execute(
            """
            SELECT f.doc_id, d.source_url, d.metadata_json,
                   snippet(document_fts, 1, '[', ']', '…', 12) AS snippet,
                   bm25(document_fts) AS rank
            FROM document_fts AS f
            LEFT JOIN document_cache d ON d.doc_id = f.doc_id
            WHERE document_fts MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (query, limit),
        ).fetchall()
        return [
            {
                "doc_id": r["doc_id"],
                "source_url": r["source_url"],
                "metadata": json.loads(r["metadata_json"]) if r["metadata_json"] else {},
                "snippet": r["snippet"],
                "rank": r["rank"],
            }
            for r in rows
        ]

    # ------------------------------------------------------------------ maintenance
    async def purge_expired(self) -> dict[str, int]:
        return await asyncio.to_thread(self._purge_expired_sync)

    def _purge_expired_sync(self) -> dict[str, int]:
        conn = self._require_conn()
        now = int(time.time())
        s = conn.execute(
            "DELETE FROM search_cache WHERE expires_at IS NOT NULL AND expires_at < ?",
            (now,),
        ).rowcount
        d = conn.execute(
            "DELETE FROM document_cache WHERE expires_at IS NOT NULL AND expires_at < ?",
            (now,),
        ).rowcount
        return {"search_deleted": s, "documents_deleted": d}

    async def stats(self) -> dict[str, Any]:
        return await asyncio.to_thread(self._stats_sync)

    def _stats_sync(self) -> dict[str, Any]:
        conn = self._require_conn()
        s_count = conn.execute("SELECT COUNT(*) FROM search_cache").fetchone()[0]
        d_count = conn.execute("SELECT COUNT(*) FROM document_cache").fetchone()[0]
        db_size = self.db_path.stat().st_size if self.db_path.exists() else 0
        return {
            "search_entries": s_count,
            "document_entries": d_count,
            "db_path": str(self.db_path),
            "db_size_bytes": db_size,
        }
