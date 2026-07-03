"""Tamper-evident audit log.

Every consequential action (AI decisions, order submissions, approvals,
risk rejections, config changes, vault operations) is appended to an
audit table where each record carries the SHA-256 hash of the previous
record — an in-database hash chain. Records are never updated or deleted
by the application; ``verify_chain`` detects any external tampering.
"""

from __future__ import annotations

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
    material = f"{seq}|{at}|{actor}|{action}|{payload_json}|{prev_hash}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class AuditLog:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def append(self, actor: str, action: str, payload: dict[str, Any] | None = None) -> AuditRecord:
        payload = payload or {}
        payload_json = _canonical(payload)
        at = datetime.now(UTC)
        at_iso = at.isoformat()
        async with self._db.transaction() as conn:
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
        rows = await self._db.fetch_all(
            "SELECT seq, at, actor, action, payload, prev_hash, hash FROM audit ORDER BY seq ASC"
        )
        prev = GENESIS_HASH
        for r in rows:
            expected = _record_hash(r[0], r[1], r[2], r[3], r[4], prev)
            if r[5] != prev or r[6] != expected:
                return False, int(r[0])
            prev = r[6]
        return True, None
