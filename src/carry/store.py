"""
Local SQLite store-and-forward persistence.

Every carrier maintains a local store. Parcels persist across reboots,
power cycles, and connectivity gaps. The store is the relay station —
parcels rest here until a carrier can take them forward.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

from .parcel import Parcel


# Priority ordering for forwarding (lower number = higher priority)
_PRIORITY_ORDER = {"urgent": 0, "normal": 1, "deferred": 2}

# Parcel statuses
STATUS_PENDING = "pending"      # Waiting to be forwarded
STATUS_IN_TRANSIT = "in_transit"  # Handed to next hop, awaiting confirmation
STATUS_DELIVERED = "delivered"   # Reached final destination
STATUS_HELD = "held"            # Held by fence (power, etc.)
STATUS_STALLED = "stalled"      # Too many failed attempts
STATUS_EXPIRED = "expired"      # Past TTL


class Store:
    """
    SQLite-backed store-and-forward queue for parcels.

    Thread-safe. Designed for edge devices — single file, no server,
    minimal overhead.
    """

    def __init__(self, db_path: str | Path = "carry_store.db"):
        self.db_path = str(db_path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS parcels (
                    id TEXT PRIMARY KEY,
                    origin TEXT NOT NULL,
                    destination TEXT NOT NULL,
                    priority TEXT NOT NULL DEFAULT 'normal',
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    hop_count INTEGER NOT NULL DEFAULT 0,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    last_attempt_at INTEGER,
                    next_attempt_at INTEGER,
                    delivered_to TEXT,
                    parcel_json TEXT NOT NULL,
                    stored_at INTEGER NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_status_priority
                    ON parcels(status, priority, created_at);

                CREATE INDEX IF NOT EXISTS idx_destination
                    ON parcels(destination);

                CREATE INDEX IF NOT EXISTS idx_expires
                    ON parcels(expires_at);
                """
            )
            self._conn.commit()

    def put(self, parcel: Parcel, status: str = STATUS_PENDING) -> None:
        """Store or update a parcel."""
        with self._lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO parcels
                    (id, origin, destination, priority, status,
                     created_at, expires_at, hop_count, attempt_count,
                     last_attempt_at, next_attempt_at, delivered_to,
                     parcel_json, stored_at)
                VALUES
                    (?, ?, ?, ?, ?,
                     ?, ?, ?, ?,
                     ?, ?, ?,
                     ?, ?)
                """,
                (
                    parcel.parcel_id,
                    parcel.envelope.origin,
                    parcel.envelope.destination,
                    parcel.envelope.priority,
                    status,
                    parcel.envelope.created_at,
                    parcel.envelope.expires_at,
                    parcel.envelope.hop_count,
                    0,
                    None,
                    None,
                    None,
                    parcel.to_json(),
                    int(time.time()),
                ),
            )
            self._conn.commit()

    def get(self, parcel_id: str) -> Parcel | None:
        """Retrieve a parcel by ID."""
        with self._lock:
            row = self._conn.execute(
                "SELECT parcel_json FROM parcels WHERE id = ?",
                (parcel_id,),
            ).fetchone()
            if row is None:
                return None
            return Parcel.from_json(row["parcel_json"])

    def get_pending(self, limit: int = 10, priority: str | None = None) -> list[Parcel]:
        """
        Get parcels pending forward, ordered by priority then age.

        Args:
            limit: Maximum number of parcels to return.
            priority: Filter by priority (urgent, normal, deferred).
        """
        with self._lock:
            now = int(time.time())

            if priority:
                rows = self._conn.execute(
                    """
                    SELECT parcel_json FROM parcels
                    WHERE status = ?
                      AND priority = ?
                      AND expires_at > ?
                      AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                    ORDER BY
                        CASE priority
                            WHEN 'urgent' THEN 0
                            WHEN 'normal' THEN 1
                            WHEN 'deferred' THEN 2
                        END,
                        created_at ASC
                    LIMIT ?
                    """,
                    (STATUS_PENDING, priority, now, now, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    """
                    SELECT parcel_json FROM parcels
                    WHERE status = ?
                      AND expires_at > ?
                      AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                    ORDER BY
                        CASE priority
                            WHEN 'urgent' THEN 0
                            WHEN 'normal' THEN 1
                            WHEN 'deferred' THEN 2
                        END,
                        created_at ASC
                    LIMIT ?
                    """,
                    (STATUS_PENDING, now, now, limit),
                ).fetchall()

            return [Parcel.from_json(r["parcel_json"]) for r in rows]

    def mark_in_transit(self, parcel_id: str, delivered_to: str) -> None:
        """Mark a parcel as handed off to the next hop."""
        with self._lock:
            self._conn.execute(
                """
                UPDATE parcels
                SET status = ?, delivered_to = ?,
                    last_attempt_at = ?, attempt_count = attempt_count + 1
                WHERE id = ?
                """,
                (STATUS_IN_TRANSIT, delivered_to, int(time.time()), parcel_id),
            )
            self._conn.commit()

    def mark_delivered(self, parcel_id: str) -> None:
        """Mark a parcel as delivered to its final destination."""
        with self._lock:
            self._conn.execute(
                "UPDATE parcels SET status = ? WHERE id = ?",
                (STATUS_DELIVERED, parcel_id),
            )
            self._conn.commit()

    def mark_held(self, parcel_id: str) -> None:
        """Mark a parcel as held by the fence."""
        with self._lock:
            self._conn.execute(
                "UPDATE parcels SET status = ? WHERE id = ?",
                (STATUS_HELD, parcel_id),
            )
            self._conn.commit()

    def mark_pending(self, parcel_id: str) -> None:
        """Return a held or stalled parcel to pending."""
        with self._lock:
            self._conn.execute(
                "UPDATE parcels SET status = ? WHERE id = ?",
                (STATUS_PENDING, parcel_id),
            )
            self._conn.commit()

    def record_attempt(self, parcel_id: str, next_attempt_delay_s: int) -> None:
        """
        Record a failed forwarding attempt and schedule next retry.

        Uses exponential backoff:
        Attempt 1: 30s, 2: 120s, 3: 480s, 4: 1800s, 5+: 7200s

        After 12 attempts, marks as stalled.
        """
        backoff_schedule = [30, 120, 480, 1800, 7200]

        with self._lock:
            row = self._conn.execute(
                "SELECT attempt_count FROM parcels WHERE id = ?",
                (parcel_id,),
            ).fetchone()
            if row is None:
                return

            attempts = row["attempt_count"] + 1

            if attempts >= 12:
                status = STATUS_STALLED
                next_attempt = None
            else:
                status = STATUS_PENDING
                idx = min(attempts - 1, len(backoff_schedule) - 1)
                delay = backoff_schedule[idx]
                next_attempt = int(time.time()) + delay

            self._conn.execute(
                """
                UPDATE parcels
                SET attempt_count = ?,
                    last_attempt_at = ?,
                    next_attempt_at = ?,
                    status = ?
                WHERE id = ?
                """,
                (attempts, int(time.time()), next_attempt, status, parcel_id),
            )
            self._conn.commit()

    def purge_expired(self) -> int:
        """Remove expired parcels. Returns count purged."""
        with self._lock:
            now = int(time.time())
            cursor = self._conn.execute(
                "DELETE FROM parcels WHERE expires_at <= ?",
                (now,),
            )
            self._conn.commit()
            return cursor.rowcount

    def count(self, status: str | None = None) -> int:
        """Count parcels, optionally filtered by status."""
        with self._lock:
            if status:
                row = self._conn.execute(
                    "SELECT COUNT(*) as c FROM parcels WHERE status = ?",
                    (status,),
                ).fetchone()
            else:
                row = self._conn.execute("SELECT COUNT(*) as c FROM parcels").fetchone()
            return row["c"]

    def update_parcel(self, parcel: Parcel) -> None:
        """Update a stored parcel (e.g., after modifying hop log)."""
        with self._lock:
            self._conn.execute(
                """
                UPDATE parcels SET
                    parcel_json = ?,
                    hop_count = ?,
                    status = CASE WHEN status = ? THEN status ELSE status END
                WHERE id = ?
                """,
                (
                    parcel.to_json(),
                    parcel.envelope.hop_count,
                    STATUS_DELIVERED,
                    parcel.parcel_id,
                ),
            )
            self._conn.commit()

    def close(self) -> None:
        """Close the database connection."""
        with self._lock:
            self._conn.close()
