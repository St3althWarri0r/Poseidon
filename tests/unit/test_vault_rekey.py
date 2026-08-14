"""Vault passphrase rotation.

Until now there was no way to change a vault passphrase. `vault.py` implemented
create/unlock/get/set/delete/names and the CLI exposed only
`init|unlock-check|set|rm|list`, so rotating meant deleting `vault.bin` and
re-entering every credential by hand — undocumented, and irreversible if you
mistyped one. That made "rotate after an exposure" advice the operator could not
act on, which the 2026-08 audit flagged as a runbook gap (F-042).

Rekey re-encrypts the SAME secrets under a NEW key derived from a NEW random
salt, through the existing atomic `_persist`. The scrypt parameters and the
header layout are deliberately untouched: `security/CLAUDE.md` records that they
are load-bearing and that a format change must be a versioned migration.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from poseidon.core.errors import VaultError, VaultLockedError
from poseidon.security.vault import Vault

OLD = "old-passphrase-123"
NEW = "new-passphrase-456"


def _vault(tmp_path: Path) -> Vault:
    v = Vault(tmp_path / "vault.bin")
    v.create(OLD)
    v.set("alpaca_paper_keys", '{"key_id": "AK", "secret_key": "S"}')
    v.set("anthropic_api_key", "sk-ant-xyz")
    return v


def test_rekey_preserves_every_secret(tmp_path: Path) -> None:
    v = _vault(tmp_path)
    before = {n: v.get(n) for n in v.names()}
    v.rekey(NEW)

    reopened = Vault(tmp_path / "vault.bin")
    reopened.unlock(NEW)
    assert {n: reopened.get(n) for n in reopened.names()} == before


def test_the_old_passphrase_stops_working(tmp_path: Path) -> None:
    v = _vault(tmp_path)
    v.rekey(NEW)
    with pytest.raises(VaultError):
        Vault(tmp_path / "vault.bin").unlock(OLD)


def test_rekey_draws_a_fresh_salt(tmp_path: Path) -> None:
    """Re-deriving under the same salt would weaken rotation — the point is that
    the new key is unrelated to the old one."""
    path = tmp_path / "vault.bin"
    v = _vault(tmp_path)
    header_len, salt_len = 19, 16
    salt_before = path.read_bytes()[header_len:header_len + salt_len]
    v.rekey(NEW)
    salt_after = path.read_bytes()[header_len:header_len + salt_len]
    assert salt_before != salt_after


def test_rekey_requires_an_unlocked_vault(tmp_path: Path) -> None:
    v = _vault(tmp_path)
    locked = Vault(v._path)  # noqa: SLF001 - constructing a second, locked handle
    with pytest.raises(VaultLockedError):
        locked.rekey(NEW)


def test_rekey_enforces_the_same_minimum_as_create(tmp_path: Path) -> None:
    v = _vault(tmp_path)
    with pytest.raises(VaultError, match="8 characters"):
        v.rekey("short")


def test_a_rejected_rekey_leaves_the_vault_usable(tmp_path: Path) -> None:
    """The failure that would actually hurt: a refused rotation must not have
    half-written the file. The OLD passphrase must still open it."""
    v = _vault(tmp_path)
    with pytest.raises(VaultError):
        v.rekey("short")
    reopened = Vault(tmp_path / "vault.bin")
    reopened.unlock(OLD)
    assert reopened.get("anthropic_api_key") == "sk-ant-xyz"


def test_rekey_keeps_the_file_private_and_leaves_no_temp(tmp_path: Path) -> None:
    v = _vault(tmp_path)
    v.rekey(NEW)
    mode = stat.S_IMODE(v._path.stat().st_mode)  # noqa: SLF001
    assert mode == 0o600, f"vault must stay 0600 after rotation, got {mode:o}"
    assert not (tmp_path / "vault.tmp").exists(), "atomic write left a temp file behind"


def test_rekeyed_handle_stays_usable_without_reunlocking(tmp_path: Path) -> None:
    v = _vault(tmp_path)
    v.rekey(NEW)
    assert v.unlocked
    v.set("added_after_rotation", "value")
    reopened = Vault(tmp_path / "vault.bin")
    reopened.unlock(NEW)
    assert reopened.get("added_after_rotation") == "value"
