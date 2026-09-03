"""Transactional checkpoints and non-lossy raw response history."""

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import SCHEMA_VERSION


def now():
    return datetime.now(timezone.utc).isoformat()


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def read_document(db, run_id, name, encoded):
    value = json.loads(encoded)
    if isinstance(value, dict) and value.get("_storage") == "chunked-index-v1":
        expected = value.pop("_chunk_count")
        value.pop("_storage")
        value["items"] = []
        chunks = db.execute("SELECT chunk_number,data FROM document_chunks WHERE run_id=? AND name=? ORDER BY chunk_number", (run_id, name))
        count = 0
        for number, content in chunks:
            if number != count:
                raise ValueError("Missing index chunk")
            value["items"].extend(json.loads(content))
            count += 1
        if count != expected:
            raise ValueError("Incomplete chunked index")
    return value


class Store:
    INDEX_CHUNK_SIZE = 500
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.directory / "archive.sqlite3")
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY, settings TEXT NOT NULL, started_at TEXT NOT NULL,
                finished_at TEXT, status TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS responses (
                id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, cache_key TEXT NOT NULL,
                method TEXT NOT NULL, endpoint TEXT NOT NULL, request_body TEXT,
                status INTEGER NOT NULL, headers TEXT NOT NULL, body BLOB NOT NULL,
                sha256 TEXT NOT NULL, fetched_at TEXT NOT NULL, reusable INTEGER NOT NULL);
            CREATE INDEX IF NOT EXISTS response_cache ON responses(run_id, cache_key, reusable);
            CREATE TABLE IF NOT EXISTS documents (
                run_id TEXT NOT NULL, name TEXT NOT NULL, data TEXT NOT NULL,
                PRIMARY KEY(run_id, name));
            CREATE TABLE IF NOT EXISTS document_chunks (
                run_id TEXT NOT NULL, name TEXT NOT NULL, chunk_number INTEGER NOT NULL,
                data TEXT NOT NULL, PRIMARY KEY(run_id, name, chunk_number));
        """)
        old = self.db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
        if old and int(old[0]) != SCHEMA_VERSION:
            raise ValueError("Unsupported archive schema version")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO metadata VALUES ('schema_version', ?)",
                            (str(SCHEMA_VERSION),))

    def close(self):
        self.db.close()

    def new_run(self, settings):
        run_id = uuid.uuid4().hex
        with self.db:
            self.db.execute("INSERT INTO runs VALUES (?, ?, ?, NULL, 'running')",
                            (run_id, dumps(settings), now()))
        return run_id

    def run(self, run_id):
        row = self.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise ValueError("Unknown run ID")
        result = dict(row)
        result["settings"] = json.loads(result["settings"])
        return result

    def finish(self, run_id, status):
        with self.db:
            self.db.execute("UPDATE runs SET status=?, finished_at=? WHERE id=?",
                            (status, now(), run_id))

    def cooldown(self, until=None):
        if until is not None:
            with self.db:
                self.db.execute("INSERT OR REPLACE INTO metadata VALUES ('api_cooldown', ?)", (str(until),))
        row = self.db.execute("SELECT value FROM metadata WHERE key='api_cooldown'").fetchone()
        return float(row[0]) if row else 0

    def put(self, run_id, name, value):
        with self.db:
            self.db.execute("DELETE FROM document_chunks WHERE run_id=? AND name=?", (run_id, name))
            if name.startswith("index/") and isinstance(value, dict) and len(value.get("items", [])) > self.INDEX_CHUNK_SIZE:
                # Large repositories exceed SQLite's 1 GB single-value limit.
                # Commit all chunks + metadata atomically, without one giant JSON string.
                rows = value["items"]
                for number, start in enumerate(range(0, len(rows), self.INDEX_CHUNK_SIZE)):
                    self.db.execute("INSERT INTO document_chunks VALUES (?, ?, ?, ?)",
                                    (run_id, name, number, dumps(rows[start:start + self.INDEX_CHUNK_SIZE])))
                value = {k: v for k, v in value.items() if k != "items"}
                value.update(_storage="chunked-index-v1", _chunk_count=(len(rows) + self.INDEX_CHUNK_SIZE - 1) // self.INDEX_CHUNK_SIZE)
            self.db.execute("INSERT OR REPLACE INTO documents VALUES (?, ?, ?)",
                            (run_id, name, dumps(value)))

    def get(self, run_id, name):
        row = self.db.execute("SELECT data FROM documents WHERE run_id=? AND name=?",
                              (run_id, name)).fetchone()
        return read_document(self.db, run_id, name, row[0]) if row else None

    def documents(self, run_id):
        return {r[0]: read_document(self.db, run_id, r[0], r[1]) for r in self.db.execute(
            "SELECT name, data FROM documents WHERE run_id=? ORDER BY name", (run_id,))}

    def cached(self, run_id, key):
        row = self.db.execute(
            "SELECT * FROM responses WHERE run_id=? AND cache_key=? AND reusable=1 ORDER BY id DESC LIMIT 1",
            (run_id, key)).fetchone()
        return dict(row) if row else None

    def response(self, run_id, key, method, endpoint, payload, status, headers, body, reusable):
        digest = hashlib.sha256(body).hexdigest()
        with self.db:
            cursor = self.db.execute("""INSERT INTO responses
                (run_id,cache_key,method,endpoint,request_body,status,headers,body,sha256,fetched_at,reusable)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""", (run_id, key, method, endpoint,
                dumps(payload) if payload is not None else None, status, dumps(headers), body,
                digest, now(), int(reusable)))
        return dict(self.db.execute("SELECT * FROM responses WHERE id=?", (cursor.lastrowid,)).fetchone())
