"""SQLite database persistence module with facts_current, fact_history, runs, checkpoints, and http_validators (Rule 9).

Responsibility: Handles storage, upserting by (orgnr, field, period), history archiving,
checkpointing, HTTP conditional validators, and run reporting.
"""

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, List, Optional, Sequence, Tuple, Union

from profiler.models import ErrorRecord, Fact, Profile


class Storage:
    """SQLite database manager enforcing upserts by (orgnr, field, period), history tracking, and checkpoints."""

    def __init__(self, db_path: Union[str, Path] = "norway_profiles.db") -> None:
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self) -> None:
        with self._conn:
            # Current Facts table
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS facts_current (
                    orgnr TEXT NOT NULL,
                    field TEXT NOT NULL,
                    period TEXT NOT NULL DEFAULT 'current',
                    value TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    source_name TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    retrieved_at TEXT NOT NULL,
                    method TEXT NOT NULL,
                    match_basis TEXT NOT NULL,
                    confidence REAL NOT NULL DEFAULT 1.0,
                    evidence_quote TEXT,
                    PRIMARY KEY (orgnr, field, period)
                )
                """
            )

            # Fact History table
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS fact_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    orgnr TEXT NOT NULL,
                    field TEXT NOT NULL,
                    period TEXT NOT NULL DEFAULT 'current',
                    value TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    source_name TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    retrieved_at TEXT NOT NULL,
                    method TEXT NOT NULL,
                    match_basis TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    evidence_quote TEXT,
                    archived_at TEXT NOT NULL
                )
                """
            )

            # Runs table
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    status TEXT NOT NULL,
                    config_json TEXT,
                    report_json TEXT
                )
                """
            )

            # Checkpoints table
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS checkpoints (
                    checkpoint_id TEXT PRIMARY KEY,
                    orgnr TEXT NOT NULL,
                    status TEXT NOT NULL,
                    saved_at TEXT NOT NULL
                )
                """
            )

            # HTTP Validators table
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS http_validators (
                    url TEXT PRIMARY KEY,
                    etag TEXT,
                    last_modified TEXT,
                    updated_at TEXT NOT NULL
                )
                """
            )

            # Profiles cache table
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS profiles (
                    orgnr TEXT PRIMARY KEY,
                    data TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    def upsert_fact(self, fact: Fact, period: str = "current") -> Optional[Fact]:
        """Upsert fact into facts_current by (orgnr, field, period), archiving old version to fact_history if changed."""
        val_str = json.dumps(fact.value) if not isinstance(fact.value, str) else fact.value
        mb_str = ",".join(fact.match_basis)
        now_str = datetime.now(timezone.utc).isoformat()
        retrieved_str = (fact.retrieved_at or datetime.now(timezone.utc)).isoformat()

        cursor = self._conn.cursor()
        cursor.execute(
            "SELECT * FROM facts_current WHERE orgnr = ? AND field = ? AND period = ?",
            (fact.orgnr, fact.field, period),
        )
        existing = cursor.fetchone()

        with self._conn:
            if existing:
                if existing["content_hash"] != fact.content_hash or existing["value"] != val_str:
                    self._conn.execute(
                        """
                        INSERT INTO fact_history (
                            orgnr, field, period, value, content_hash, source_name, source_url,
                            retrieved_at, method, match_basis, confidence, evidence_quote, archived_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            existing["orgnr"],
                            existing["field"],
                            existing["period"],
                            existing["value"],
                            existing["content_hash"],
                            existing["source_name"],
                            existing["source_url"],
                            existing["retrieved_at"],
                            existing["method"],
                            existing["match_basis"],
                            existing["confidence"],
                            existing["evidence_quote"],
                            now_str,
                        ),
                    )

            self._conn.execute(
                """
                INSERT OR REPLACE INTO facts_current (
                    orgnr, field, period, value, content_hash, source_name, source_url,
                    retrieved_at, method, match_basis, confidence, evidence_quote
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fact.orgnr,
                    fact.field,
                    period,
                    val_str,
                    fact.content_hash or "",
                    fact.source_name,
                    fact.source_url,
                    retrieved_str,
                    fact.method,
                    mb_str,
                    fact.confidence,
                    fact.evidence_quote,
                ),
            )
        return fact

    def get_current_facts(self, orgnr: str) -> List[Fact]:
        """Get all current facts for an orgnr."""
        cursor = self._conn.cursor()
        cursor.execute("SELECT * FROM facts_current WHERE orgnr = ?", (orgnr,))
        rows = cursor.fetchall()
        facts: List[Fact] = []
        for r in rows:
            val = r["value"]
            try:
                val = json.loads(val)
            except Exception:
                pass

            dt = datetime.fromisoformat(r["retrieved_at"]) if r["retrieved_at"] else datetime.now(timezone.utc)
            facts.append(
                Fact(
                    orgnr=r["orgnr"],
                    field=r["field"],
                    value=val,
                    source_name=r["source_name"],
                    source_url=r["source_url"],
                    retrieved_at=dt,
                    method=r["method"],
                    match_basis=r["match_basis"].split(",") if r["match_basis"] else [],
                    confidence=r["confidence"],
                    evidence_quote=r["evidence_quote"],
                    content_hash=r["content_hash"],
                )
            )
        return facts

    def get_fact_history(self, orgnr: str, field: Optional[str] = None) -> List[dict]:
        """Get historical facts for an orgnr."""
        cursor = self._conn.cursor()
        if field:
            cursor.execute(
                "SELECT * FROM fact_history WHERE orgnr = ? AND field = ? ORDER BY archived_at DESC",
                (orgnr, field),
            )
        else:
            cursor.execute(
                "SELECT * FROM fact_history WHERE orgnr = ? ORDER BY archived_at DESC",
                (orgnr,),
            )
        return [dict(r) for r in cursor.fetchall()]

    def save_http_validator(self, url: str, etag: Optional[str], last_modified: Optional[str]) -> None:
        """Save ETag / Last-Modified validators for HTTP URL."""
        now_str = datetime.now(timezone.utc).isoformat()
        with self._conn:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO http_validators (url, etag, last_modified, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (url, etag, last_modified, now_str),
            )

    def get_http_validator(self, url: str) -> Optional[dict]:
        """Get stored HTTP validators for a URL."""
        cursor = self._conn.cursor()
        cursor.execute("SELECT * FROM http_validators WHERE url = ?", (url,))
        row = cursor.fetchone()
        return dict(row) if row else None

    def save_checkpoint(self, checkpoint_id: str, orgnr: str, status: str) -> None:
        """Save pipeline checkpoint."""
        now_str = datetime.now(timezone.utc).isoformat()
        with self._conn:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO checkpoints (checkpoint_id, orgnr, status, saved_at)
                VALUES (?, ?, ?, ?)
                """,
                (checkpoint_id, orgnr, status, now_str),
            )

    def save_profile(self, profile: Profile) -> None:
        """Save a company profile and upsert its facts into SQLite."""
        data_json = profile.model_dump_json()
        now_str = profile.facts[0].retrieved_at.isoformat() if profile.facts else datetime.now(timezone.utc).isoformat()
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO profiles (orgnr, data, updated_at) VALUES (?, ?, ?)",
                (profile.orgnr, data_json, now_str),
            )
            for fact in profile.facts:
                period = "current"
                if fact.field in ("financials", "revenue", "operating_profit") and isinstance(fact.value, dict):
                    period = str(fact.value.get("year", "current"))
                self.upsert_fact(fact, period=period)

    def get_profile(self, orgnr: str) -> Optional[Profile]:
        """Retrieve stored profile by orgnr."""
        cursor = self._conn.cursor()
        cursor.execute("SELECT data FROM profiles WHERE orgnr = ?", (orgnr,))
        row = cursor.fetchone()
        if not row:
            return None
        return Profile.model_validate_json(row["data"])

    def close(self) -> None:
        self._conn.close()
