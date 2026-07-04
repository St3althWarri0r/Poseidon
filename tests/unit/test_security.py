"""Vault and audit-chain tests."""

from __future__ import annotations

import pytest

from poseidon.core.errors import VaultError, VaultLockedError
from poseidon.security.audit import AuditLog
from poseidon.security.vault import Vault
from poseidon.storage.db import Database


class TestVault:
    def test_create_set_get_roundtrip(self, tmp_path) -> None:
        vault = Vault(tmp_path / "vault.bin")
        vault.create("correct horse battery")
        vault.set("api_key", "sk-secret-123")
        vault.lock()
        vault.unlock("correct horse battery")
        assert vault.get("api_key") == "sk-secret-123"
        assert vault.names() == ["api_key"]

    def test_wrong_passphrase(self, tmp_path) -> None:
        vault = Vault(tmp_path / "vault.bin")
        vault.create("passphrase-1")
        vault.lock()
        with pytest.raises(VaultError):
            vault.unlock("wrong-passphrase")

    def test_locked_access_raises(self, tmp_path) -> None:
        vault = Vault(tmp_path / "vault.bin")
        vault.create("passphrase-1")
        vault.lock()
        with pytest.raises(VaultLockedError):
            vault.get("anything")

    def test_missing_credential_message_names_it(self, tmp_path) -> None:
        vault = Vault(tmp_path / "vault.bin")
        vault.create("passphrase-1")
        with pytest.raises(VaultError, match="polygon_api_key"):
            vault.get("polygon_api_key")

    def test_get_json(self, tmp_path) -> None:
        vault = Vault(tmp_path / "vault.bin")
        vault.create("passphrase-1")
        vault.set("alpaca", '{"key_id": "k", "secret_key": "s"}')
        assert vault.get_json("alpaca") == {"key_id": "k", "secret_key": "s"}
        vault.set("bad", "not json")
        with pytest.raises(VaultError):
            vault.get_json("bad")

    def test_short_passphrase_rejected(self, tmp_path) -> None:
        with pytest.raises(VaultError):
            Vault(tmp_path / "vault.bin").create("short")

    def test_file_permissions(self, tmp_path) -> None:
        path = tmp_path / "vault.bin"
        Vault(path).create("passphrase-1")
        assert (path.stat().st_mode & 0o777) == 0o600

    def test_plaintext_not_on_disk(self, tmp_path) -> None:
        path = tmp_path / "vault.bin"
        vault = Vault(path)
        vault.create("passphrase-1")
        vault.set("k", "super-secret-value")
        assert b"super-secret-value" not in path.read_bytes()


class TestAuditChain:
    async def test_append_and_verify(self, tmp_path) -> None:
        db = Database(tmp_path / "a.db")
        await db.open()
        try:
            audit = AuditLog(db)
            await audit.append("system", "startup", {"v": 1})
            await audit.append("ai", "decision", {"id": "d1"})
            await audit.append("human", "approval", {"ok": True})
            ok, bad = await audit.verify_chain()
            assert ok and bad is None
            tail = await audit.tail(10)
            assert len(tail) == 3
            assert tail[0].seq == 3  # newest first
        finally:
            await db.close()

    async def test_tampering_detected(self, tmp_path) -> None:
        db = Database(tmp_path / "a.db")
        await db.open()
        try:
            audit = AuditLog(db)
            await audit.append("system", "startup", {})
            await audit.append("ai", "decision", {"qty": 10})
            # Tamper with record 2's payload directly.
            await db.execute("UPDATE audit SET payload = ? WHERE seq = 2", ('{"qty":9999}',))
            ok, bad = await audit.verify_chain()
            assert not ok and bad == 2
        finally:
            await db.close()
