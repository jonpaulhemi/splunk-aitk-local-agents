"""Runtime records: one per agent AI Toolkit has asked us to create.

Kept in SQLite under $SPLUNK_HOME/var/lib/aitk_local/, not in process memory,
because splunkd spreads requests across several handler processes (spike #2).
"""

import contextlib
import os
import sqlite3
import time
import uuid

READY = "ready"
ACTIVE = "active"
NOT_FOUND = "not_found"


def _db_path():
    directory = os.path.join(os.environ["SPLUNK_HOME"], "var", "lib", "aitk_local")
    os.makedirs(directory, exist_ok=True)
    return os.path.join(directory, "runtime.db")


@contextlib.contextmanager
def _connect():
    db = sqlite3.connect(_db_path(), timeout=30, isolation_level=None)
    try:
        _ensure_schema(db)
        yield db
    finally:
        db.close()


def _ensure_schema(db):
    db.execute("PRAGMA journal_mode=WAL")
    db.execute(
        """CREATE TABLE IF NOT EXISTS runtimes (
            runtime_id TEXT PRIMARY KEY,
            version TEXT NOT NULL,
            memory_id TEXT NOT NULL UNIQUE,
            agent_name TEXT NOT NULL,
            agent_type TEXT,
            app_version TEXT,
            created_at REAL NOT NULL
        )"""
    )


def create(agent_name, agent_type, app_version):
    """A new runtime for `agent_name`; returns (runtime_id, version, memory_id)."""
    runtime_id = f"aitk_local_{uuid.uuid4().hex}"
    memory_id = f"aitk_local_mem_{uuid.uuid4().hex}"
    version = "1"
    with _connect() as db:
        db.execute(
            "INSERT INTO runtimes VALUES (?, ?, ?, ?, ?, ?, ?)",
            (runtime_id, version, memory_id, agent_name, agent_type, app_version, time.time()),
        )
    return runtime_id, version, memory_id


def delete(runtime_id):
    """Remove a runtime; False if there was none."""
    with _connect() as db:
        return db.execute("DELETE FROM runtimes WHERE runtime_id = ?", (runtime_id,)).rowcount > 0


def runtime_status(runtime_id):
    return READY if _exists("runtime_id", runtime_id) else NOT_FOUND


def memory_status(memory_id):
    return ACTIVE if _exists("memory_id", memory_id) else NOT_FOUND


def _exists(column, value):
    with _connect() as db:
        return db.execute(f"SELECT 1 FROM runtimes WHERE {column} = ?", (value,)).fetchone() is not None
