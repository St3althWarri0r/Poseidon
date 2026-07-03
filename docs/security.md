# Security model

## Scope

Single-user desktop/server deployment. The threats considered: credential
theft from disk, log/DB leakage of secrets, tampering with the action
history, accidental network exposure of the dashboard, and unattended
compromise amplification (an attacker with the box should not also get
clean, invisible control of your brokerage).

Out of scope: multi-user isolation, an attacker with your running
process's memory, and brokerage-side account security (use the brokers'
own MFA — see below).

## Credential vault

- All secrets (Anthropic key, provider keys, broker credentials, SMTP/bot
  tokens) live in `~/.local/share/aegis-trader/vault.bin`.
- Format: versioned header + 16-byte random salt + Fernet token
  (AES-128-CBC + HMAC-SHA256). The key derives from your passphrase with
  scrypt (n=2¹⁵, r=8, p=1).
- Plaintext secrets exist only in process memory while unlocked; the file
  is written atomically with mode 0600.
- Unlock paths: interactive prompt, `AEGIS_VAULT_PASSPHRASE`,
  `AEGIS_VAULT_PASSPHRASE_FILE` (must be 0600), or a systemd encrypted
  credential (preferred for the service):

```bash
systemd-creds encrypt --user --name=aegis-vault-passphrase \
    <(printf '%s' 'YOUR-PASSPHRASE') \
    ~/.config/aegis-trader/vault-passphrase.cred
```

The unit's `LoadCredentialEncrypted=` decrypts it at start (bound to this
machine's TPM/host key where available) and exposes it only to the service
process.

- `aegis vault list` shows names only; values are never enumerable.

## Multi-factor authentication

Enable MFA on every upstream account: your brokerage(s), data providers,
and Anthropic console. Broker plugins use token/OAuth mechanisms that
survive MFA (Schwab's consent flow, tastytrade remember tokens, IBKR's
gateway login) — Aegis never asks you to weaken account security to
automate it.

## Logs & database

- A structlog processor redacts any field whose name looks like
  key/secret/token/password before rendering, on both console and file
  sinks.
- The SQLite database (orders, decisions, audit) is created 0600. It holds
  trading history but no credentials.
- For at-rest encryption of the whole data directory, use your platform's
  filesystem encryption (LUKS full-disk, or fscrypt on the directory) —
  documented deliberately instead of a homegrown DB cipher.

## Tamper-evident audit log

Every consequential action (AI decisions, approvals, submissions,
rejections, cancellations, mode changes, halts, startup/shutdown) is
appended to an audit table where each record embeds the SHA-256 hash of
the previous record. The application never updates or deletes audit rows.

- Startup verifies the whole chain and **refuses to boot** on a mismatch.
- A nightly job re-verifies and force-opens the circuit breaker on
  failure.
- `aegis audit verify` / `aegis audit tail` for manual inspection.

This makes silent history rewriting detectable: an attacker (or a bug)
that alters a record breaks every subsequent hash.

## Network posture

- The dashboard binds to `127.0.0.1` by default. Exposing it on any other
  host **requires** `dashboard.auth_token_credential` (startup refuses
  otherwise): a vault-stored bearer token checked (constant-time) on every
  API request and websocket connect; static assets only are exempt. Prefer
  an SSH tunnel (`ssh -L 8321:127.0.0.1:8321 host`) or an authenticated
  reverse proxy with TLS even so — the token is sent in clear over plain
  HTTP.
- Outbound connections: Anthropic API, configured data providers,
  configured broker endpoints, notification services — all TLS.
- The IBKR plugin accepts the local gateway's self-signed certificate only
  for localhost by default (`verify_ssl` option to force verification).

## systemd hardening

The shipped unit sets `NoNewPrivileges`, `PrivateTmp`,
`ProtectSystem=strict`, `ProtectHome=read-only` (with a write exception
for the data dir), `LockPersonality`, and `RestrictRealtime`, plus a
watchdog so a hung process is restarted rather than left half-alive.
