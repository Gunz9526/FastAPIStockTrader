"""Single Linux host, shared data volume: atomic pacing across all REST clients."""

import asyncio
import math
import sqlite3
import time
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path


def retry_delay(value, fallback):
    try:
        seconds = float(value)
    except TypeError, ValueError:
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=UTC)
            seconds = (date - datetime.now(UTC)).total_seconds()
        except TypeError, ValueError, OverflowError:
            return float(fallback)
    return max(1.0, seconds) if math.isfinite(seconds) else float(fallback)


class SharedRequestBudget:
    def __init__(self, path):
        self.path = Path(path)
        self.boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS budget (bucket TEXT PRIMARY KEY, boot TEXT NOT NULL, "
                "last REAL NOT NULL, blocked REAL NOT NULL, attempts INTEGER NOT NULL, "
                "limited INTEGER NOT NULL, waits INTEGER NOT NULL)"
            )

    @contextmanager
    def _connect(self):
        with closing(sqlite3.connect(self.path, timeout=5, isolation_level=None)) as db:
            yield db

    def _rows(self, db):
        for bucket in ("combined", "data", "trading"):
            db.execute("INSERT OR IGNORE INTO budget VALUES (?, ?, 0, 0, 0, 0, 0)", (bucket, self.boot))
        db.execute("UPDATE budget SET boot=?, last=0, blocked=0 WHERE boot<>?", (self.boot, self.boot))
        return {r[0]: r[1:] for r in db.execute("SELECT bucket, last, blocked FROM budget")}

    def reserve(self, bucket):
        if bucket not in ("data", "trading"):
            raise ValueError("unknown API bucket")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                rows = self._rows(db)
                now = time.monotonic()
                wait = max(
                    0.0,
                    rows["combined"][0] + 0.4 - now,
                    rows[bucket][0] + (0.8 if bucket == "data" else 0.4) - now,
                    rows[bucket][1] - now,
                )
                if wait == 0:
                    db.execute(
                        "UPDATE budget SET last=?, attempts=attempts+1 WHERE bucket IN ('combined', ?)",
                        (now, bucket),
                    )
                else:
                    db.execute("UPDATE budget SET waits=waits+1 WHERE bucket=?", (bucket,))
                db.execute("COMMIT")
                return wait
            except Exception:
                db.execute("ROLLBACK")
                raise

    async def acquire(self, bucket):
        while (delay := await asyncio.to_thread(self.reserve, bucket)) > 0:
            await asyncio.sleep(delay)

    def cooldown(self, bucket, seconds):
        if bucket not in ("data", "trading") or not math.isfinite(seconds) or seconds < 0:
            raise ValueError("invalid API cooldown")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                self._rows(db)
                db.execute(
                    "UPDATE budget SET blocked=MAX(blocked, ?), limited=limited+1 WHERE bucket=?",
                    (time.monotonic() + seconds, bucket),
                )
                db.execute("UPDATE budget SET limited=limited+1 WHERE bucket='combined'")
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise

    def snapshot(self):
        with self._connect() as db:
            rows = db.execute("SELECT bucket, attempts, limited, waits, blocked, boot FROM budget")
            now = time.monotonic()
            return {
                bucket: {
                    "request_attempts": attempts,
                    "http_429": limited,
                    "wait_events": waits,
                    "cooldown_seconds": max(0.0, blocked - now) if boot == self.boot else 0.0,
                }
                for bucket, attempts, limited, waits, blocked, boot in rows
            }
