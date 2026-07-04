"""Tamper-evident audit log.

Every consequential action (AI decisions, order submissions, approvals,
risk rejections, config changes, vault operations) is appended to an
audit table where each record carries the SHA-256 hash of the previous
record — an in-database hash chain. Records are never updated or deleted
by the application.

``verify_chain`` detects any modification or reordering of retained
records. It cannot, by itself, detect deletion of the most-recent
record(s) or a full table wipe — a truncated chain re-verifies as
internally consistent, and an empty table is indistinguishable from a
fresh database. Detecting truncation would require a head anchor
(expected max seq + last hash) persisted outside this database; that is
not currently implemented.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from ..core.models import AuditRecord
from ..storage.db import Database

GENESIS_HASH = "0" * 64


def _canonical(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _record_hash(seq: int, at: str, actor: str, action: str, payload_json: str, prev_hash: str) -> str:
    # JSON-array encoding escapes delimiters, making field boundaries unambiguous.
    material = json.dumps([seq, at, actor, action, payload_json, prev_hash], separators=(",", ":"))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _record_hash_v1(seq: int, at: str, actor: str, action: str, payload_json: str, prev_hash: str) -> str:
    # Legacy (<=2.3.x) encoding: unescaped '|'-join. Retained only to
    # recognise and migrate pre-upgrade audit logs to the current encoding.
    material = f"{seq}|{at}|{actor}|{action}|{payload_json}|{prev_hash}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


# Prior hash encodings, newest-first. Used only by migrate_legacy_chain to
# recognise a chain written by an older version before re-anchoring it.
_LEGACY_HASHERS = (_record_hash_v1,)


class AuditLog:
    def __init__(self, db: Database) -> None:
        self._db = db
        # Serializes the read-max-seq / insert-next-seq critical section.
        # The whole Database shares one aiosqlite connection, so concurrent
        # appends (event-bus handlers, guardian exits, approvals) could
        # otherwise both read the same max seq and collide on the seq PK
        # — forking or aborting the chain. The lock makes appends atomic.
        self._lock = asyncio.Lock()

    async def append(self, actor: str, action: str, payload: dict[str, Any] | None = None) -> AuditRecord:
        payload = payload or {}
        payload_json = _canonical(payload)
        at = datetime.now(UTC)
        at_iso = at.isoformat()
        async with self._lock, self._db.transaction() as conn:
            row = await (
                await conn.execute("SELECT seq, hash FROM audit ORDER BY seq DESC LIMIT 1")
            ).fetchone()
            seq = (row[0] + 1) if row else 1
            prev_hash = row[1] if row else GENESIS_HASH
            digest = _record_hash(seq, at_iso, actor, action, payload_json, prev_hash)
            await conn.execute(
                "INSERT INTO audit (seq, at, actor, action, payload, prev_hash, hash) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (seq, at_iso, actor, action, payload_json, prev_hash, digest),
            )
        return AuditRecord(
            seq=seq, at=at, actor=actor, action=action, payload=payload,
            prev_hash=prev_hash, hash=digest,
        )

    async def tail(self, limit: int = 100) -> list[AuditRecord]:
        rows = await self._db.fetch_all(
            "SELECT seq, at, actor, action, payload, prev_hash, hash "
            "FROM audit ORDER BY seq DESC LIMIT ?",
            (limit,),
        )
        return [
            AuditRecord(
                seq=r[0], at=datetime.fromisoformat(r[1]), actor=r[2], action=r[3],
                payload=json.loads(r[4]), prev_hash=r[5], hash=r[6],
            )
            for r in rows
        ]

    async def verify_chain(self) -> tuple[bool, int | None]:
        """Recompute the whole chain. Returns (ok, first_bad_seq)."""
        prev = GENESIS_HASH
        async with self._db.conn.execute(
            "SELECT seq, at, actor, action, payload, prev_hash, hash FROM audit ORDER BY seq ASC"
        ) as cursor:
            async for r in cursor:
                expected = _record_hash(r[0], r[1], r[2], r[3], r[4], prev)
                if r[5] != prev or r[6] != expected:
                    return False, int(r[0])
                prev = r[6]
        return True, None

    def _chain_valid_under(self, rows: list[tuple[Any, ...]], hasher: Any) -> bool:
        """True if every row's stored prev_hash/hash matches ``hasher`` in seq
        order — i.e. the chain is intact under that encoding."""
        prev = GENESIS_HASH
        for r in rows:
            if r[5] != prev or r[6] != hasher(r[0], r[1], r[2], r[3], r[4], prev):
                return False
            prev = r[6]
        return True

    async def migrate_legacy_chain(self) -> bool:
        """Upgrade an audit log written by an older version to the current
        hash encoding. Only re-anchors a chain that is fully intact under a
        known legacy encoding (so a genuinely tampered log still fails);
        returns True if a migration was performed, False otherwise."""
        rows = await self._db.fetch_all(
            "SELECT seq, at, actor, action, payload, prev_hash, hash FROM audit ORDER BY seq ASC"
        )
        if not rows:
            return False
        hasher = next((h for h in _LEGACY_HASHERS if self._chain_valid_under(rows, h)), None)
        if hasher is None:
            return False  # not a recognisable legacy chain — real corruption/tamper
        # Re-anchor: recompute prev_hash + hash for every row under the current
        # encoding, threading the new hashes forward.
        async with self._lock, self._db.transaction() as conn:
            prev = GENESIS_HASH
            for r in rows:
                new_hash = _record_hash(r[0], r[1], r[2], r[3], r[4], prev)
                await conn.execute(
                    "UPDATE audit SET prev_hash = ?, hash = ? WHERE seq = ?",
                    (prev, new_hash, r[0]),
                )
                prev = new_hash
        return True
