"""Encrypted credential vault.

All secrets (broker keys, data-provider keys, the Anthropic API key, SMTP
passwords, webhook tokens) live in a single encrypted file:

    <data_dir>/vault.bin

Format: 16-byte magic+version header, 16-byte random salt, then a Fernet
token (AES-128-CBC + HMAC-SHA256) whose key is derived from the user's
passphrase with scrypt (n=2**15, r=8, p=1). Plaintext secrets never touch
disk; the decrypted mapping is held only in process memory while unlocked.

The passphrase is supplied interactively (``poseidon vault unlock``), via the
``POSEIDON_VAULT_PASSPHRASE`` environment variable (for the systemd service,
where it should be injected through systemd credentials), or via a
passphrase file with 0600 permissions.
"""

from __future__ import annotations

import base64
import json
import os
import stat
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from ..core.errors import VaultError, VaultLockedError

_MAGIC = b"POSEIDONVLT"
_VERSION = b"\x01"
_HEADER = _MAGIC + _VERSION + b"\x00" * 7  # pad header to 16 bytes
_SALT_LEN = 16
_SCRYPT_N = 2**15
_SCRYPT_R = 8
_SCRYPT_P = 1


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    kdf = Scrypt(salt=salt, length=32, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P)
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))


class Vault:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._secrets: dict[str, str] | None = None
        self._key: bytes | None = None
        self._salt: bytes | None = None

    # -- lifecycle -----------------------------------------------------------

    @property
    def exists(self) -> bool:
        return self._path.exists()

    @property
    def unlocked(self) -> bool:
        return self._secrets is not None

    def create(self, passphrase: str) -> None:
        if self.exists:
            raise VaultError(f"vault already exists at {self._path}")
        if len(passphrase) < 8:
            raise VaultError("vault passphrase must be at least 8 characters")
        self._salt = os.urandom(_SALT_LEN)
        self._key = _derive_key(passphrase, self._salt)
        self._secrets = {}
        self._persist()

    def unlock(self, passphrase: str) -> None:
        blob = self._read_file()
        salt = blob[len(_HEADER) : len(_HEADER) + _SALT_LEN]
        token = blob[len(_HEADER) + _SALT_LEN :]
        key = _derive_key(passphrase, salt)
        try:
            plaintext = Fernet(key).decrypt(token)
        except InvalidToken as exc:
            raise VaultError("wrong passphrase or corrupt vault") from exc
        self._salt, self._key = salt, key
        self._secrets = json.loads(plaintext.decode("utf-8"))

    def unlock_from_environment(self) -> bool:
        """Try non-interactive unlock. Returns True on success, False when no
        passphrase source is configured (caller decides how to proceed)."""
        passphrase = os.environ.get("POSEIDON_VAULT_PASSPHRASE")
        pass_file = os.environ.get("POSEIDON_VAULT_PASSPHRASE_FILE")
        # systemd LoadCredential exposes files under $CREDENTIALS_DIRECTORY.
        cred_dir = os.environ.get("CREDENTIALS_DIRECTORY")
        if not passphrase and not pass_file and cred_dir:
            candidate = Path(cred_dir) / "poseidon-vault-passphrase"
            if candidate.exists():
                pass_file = str(candidate)
        if pass_file:
            p = Path(pass_file)
            mode = p.stat().st_mode
            if mode & (stat.S_IRGRP | stat.S_IROTH):
                raise VaultError(f"{p} must not be group/world readable (chmod 600)")
            passphrase = p.read_text(encoding="utf-8").strip()
        if not passphrase:
            return False
        self.unlock(passphrase)
        return True

    def lock(self) -> None:
        self._secrets = None
        self._key = None

    # -- secrets --------------------------------------------------------------

    def get(self, name: str) -> str:
        secrets = self._require_unlocked()
        try:
            return secrets[name]
        except KeyError:
            raise VaultError(
                f"credential '{name}' not found in vault — add it with: poseidon vault set {name}"
            ) from None

    def get_json(self, name: str) -> dict[str, str]:
        raw = self.get(name)
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise VaultError(f"credential '{name}' is not valid JSON") from exc
        if not isinstance(value, dict):
            raise VaultError(f"credential '{name}' must be a JSON object")
        return value

    def set(self, name: str, value: str) -> None:
        secrets = self._require_unlocked()
        secrets[name] = value
        self._persist()

    def delete(self, name: str) -> None:
        secrets = self._require_unlocked()
        secrets.pop(name, None)
        self._persist()

    def names(self) -> list[str]:
        """Credential names only — values are never enumerable."""
        return sorted(self._require_unlocked())

    # -- internals -------------------------------------------------------------

    def _require_unlocked(self) -> dict[str, str]:
        if self._secrets is None:
            raise VaultLockedError("vault is locked — run: poseidon vault unlock")
        return self._secrets

    def _read_file(self) -> bytes:
        if not self.exists:
            raise VaultError(f"no vault at {self._path} — create one with: poseidon vault init")
        blob = self._path.read_bytes()
        if len(blob) < len(_HEADER) + _SALT_LEN or not blob.startswith(_MAGIC):
            raise VaultError("vault file is corrupt or not an Poseidon vault")
        if blob[len(_MAGIC) : len(_MAGIC) + 1] != _VERSION:
            raise VaultError("unsupported vault version")
        return blob

    def _persist(self) -> None:
        assert self._secrets is not None and self._key is not None and self._salt is not None
        token = Fernet(self._key).encrypt(json.dumps(self._secrets).encode("utf-8"))
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_bytes(_HEADER + self._salt + token)
        tmp.chmod(0o600)
        tmp.replace(self._path)  # atomic on POSIX
