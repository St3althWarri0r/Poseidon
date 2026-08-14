# CLAUDE.md — `poseidon.security`

Guidance for Claude Code when working in the security subsystem. Supplements the repository-root `CLAUDE.md`; this package *is* what backs its invariants #4 (audit consequential actions) and #6 (secrets only in the vault).

Two files, both safety-critical: `vault.py` (encrypted credential store) and `audit.py` (tamper-evident hash-chained log).

## `vault.py` — encrypted credential vault

All secrets (broker keys, data-provider keys, the Anthropic API key, SMTP/webhook tokens) live in one encrypted file, `<data_dir>/vault.bin`: a 19-byte magic+version header, a 16-byte random salt, then a Fernet token (AES-128-CBC + HMAC-SHA256) keyed by `scrypt(passphrase, salt, n=2**15, r=8, p=1)`. **Plaintext secrets never touch disk**; the decrypted mapping lives only in process memory while unlocked. Config files store credential *names*; the vault holds the *values* (root invariant #6).

Unlock paths (`unlock_from_environment`): interactive passphrase, `POSEIDON_VAULT_PASSPHRASE`, or a passphrase file (`POSEIDON_VAULT_PASSPHRASE_FILE` / a systemd `LoadCredential` under `$CREDENTIALS_DIRECTORY`) that **must be `chmod 600`** — a group/world-readable file is rejected.

Invariants to preserve when touching this file:
- **Never** log or return a secret value, and **never** persist plaintext. `names()` exposes credential names only — values are never enumerable.
- Keep the `_persist` write exactly as is: temp file created `0600` from the `os.open(..., O_CREAT, 0o600)` (not a post-hoc `chmod`, which leaves a brief umask-wide read window for another local user), `fsync` of both the file **and** the parent directory, then an atomic `replace`. Each step is deliberate (local-user exposure + crash durability).
- The passphrase minimum is 8 chars. The scrypt cost parameters and header format are load-bearing: changing the KDF params or the header layout breaks decryption of every existing vault. The header carries a version byte precisely so a format change can be a *versioned migration* — do it that way, don't mutate the constants in place.

## `audit.py` — tamper-evident audit log

Every consequential action (AI decisions, order submissions, approvals, risk rejections, config/vault/broker changes, circuit trips) is appended to the in-DB `audit` table as a hash chain: each row stores the SHA-256 over its canonicalized fields plus the previous row's hash. Records are never updated or deleted by application code.

- **`append(actor, action, payload)` is the only writer**, and it must stay inside `self._lock`: the whole `Database` shares one aiosqlite connection, so concurrent appends (event-bus handlers, guardian exits, approvals) could otherwise both read the same max `seq` and fork or abort the chain. Payloads are canonicalized (sorted keys); `_record_hash` uses a JSON-array encoding so field boundaries are unambiguous. Actors are short strings (`system`, `ai`, `human`, `claude`).
- `verify_chain()` recomputes the whole chain → `(ok, first_bad_seq)`. It runs at startup and nightly (`app.py`); a broken chain refuses startup or trips the circuit breaker.
- **Documented limitation — don't overstate the guarantee:** `verify_chain` detects modification or reordering of *retained* records, but **cannot** detect truncation of the most-recent record(s) or a full table wipe (a truncated chain re-verifies as internally consistent; an empty table is indistinguishable from a fresh DB). Detecting truncation needs an out-of-band head anchor, which is not implemented.

Invariants to preserve:
- **Never `UPDATE`/`DELETE` audit rows from application code.** The one exception is `migrate_legacy_chain`, which re-anchors a pre-2.4.0 `|`-join encoding to the current JSON-array encoding — and only after confirming the chain is fully intact under the known legacy hasher (a genuinely tampered log still fails and refuses startup).
- Any new consequential action gets an `append(...)` (root invariant #4).
- Changing the hash encoding (`_record_hash`) means adding the previous hasher to `_LEGACY_HASHERS` **and** shipping a migration — otherwise every existing log fails verification and startup refuses.
