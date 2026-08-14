# Operator runbook

Procedures for the things that go wrong. `docs/troubleshooting.md` is a
symptom→cause index; this is what to *do*, in order, when you are the one who
has to fix it — often at an hour when you are not at your best.

Every command assumes the deployment checkout (`~/Poseidon` by default) and its
venv. Paths come from your config: `data_dir` defaults to
`~/.local/share/poseidon`, config to `~/.config/poseidon`.

---

## 1. Backup and restore

**What is irreplaceable, in order:**

| File | Why it matters | Recoverable without a backup? |
|---|---|---|
| `<data_dir>/vault.bin` | every broker credential and API key | **No.** Only by re-entering each credential by hand |
| `<data_dir>/poseidon.db` | decisions, orders, fills, equity marks, the audit chain | No — trading history is gone |
| `~/.config/poseidon/poseidon.yaml` | your configuration | Rebuildable from `poseidon config example`, but you lose your tuning |
| `<data_dir>/poseidon.local.yaml` | dashboard-managed broker + settings overlay | Rebuildable from the Account/Settings views |

`vault.bin` is the one that ends the conversation. It is small (~1.5 KB) and
changes only when you add or rotate a credential.

### Take a backup

Stop the engine first if you want a guaranteed-consistent DB snapshot; the vault
can be copied at any time.

```bash
BK=~/poseidon-backup-$(date +%Y%m%d)
mkdir -p "$BK" && chmod 700 "$BK"
cp ~/.local/share/poseidon/vault.bin "$BK"/
cp ~/.config/poseidon/poseidon.yaml "$BK"/
cp ~/.local/share/poseidon/poseidon.local.yaml "$BK"/ 2>/dev/null || true
# SQLite: use the backup API rather than cp — it is safe on a live DB and
# will not capture a half-written page.
.venv/bin/python -c "
import sqlite3, pathlib
src = sqlite3.connect(pathlib.Path.home()/'.local/share/poseidon/poseidon.db')
dst = sqlite3.connect('$BK/poseidon.db')
src.backup(dst); dst.close(); src.close(); print('db backed up')
"
chmod 600 "$BK"/*
```

**Verify the backup before you trust it** — an unverified backup is a belief,
not a backup:

```bash
.venv/bin/python -c "
from poseidon.security.vault import Vault
import getpass, pathlib
v = Vault(pathlib.Path('$BK/vault.bin'))
v.unlock(getpass.getpass('Vault passphrase: '))
print('vault OK —', len(v.names()), 'credential(s):', ', '.join(v.names()))
"
```

### Restore

```bash
systemctl --user stop poseidon      # or close the desktop window
cp "$BK"/vault.bin ~/.local/share/poseidon/
cp "$BK"/poseidon.db ~/.local/share/poseidon/
cp "$BK"/poseidon.yaml ~/.config/poseidon/
chmod 600 ~/.local/share/poseidon/vault.bin
.venv/bin/poseidon doctor           # confirm before starting
```

---

## 2. Rotate the vault passphrase

Use this after any suspected exposure — a passphrase typed into a chat window,
a shared screen, a leaked backup.

```bash
.venv/bin/poseidon vault rekey
```

It asks for the current passphrase, then the new one twice, and re-encrypts the
same credentials under a fresh key and a fresh salt. **No credential is
re-entered.** The write is atomic, so an interruption leaves either the old
vault or the new one.

Afterwards:

- If you use `POSEIDON_VAULT_PASSPHRASE` or a passphrase file (systemd
  `LoadCredential`), **update it** — the service will not start otherwise.
- `poseidon vault unlock-check` to confirm.

**`rekey` rotates a vault you can currently open. It cannot recover a forgotten
passphrase** — there is no recovery path by design. If the passphrase is lost,
the only route is `rm vault.bin && poseidon vault init` and re-entering every
credential.

### Rotate the underlying broker keys

Rekeying changes the vault's lock, not the keys inside it. If the *credentials*
leaked, revoke and reissue them at the broker, then:

```bash
.venv/bin/poseidon vault list           # names only
.venv/bin/poseidon vault set alpaca_paper_keys   # paste the new JSON when prompted
```

---

## 3. The engine will not start

```bash
tail -40 ~/.local/share/poseidon/launcher-engine.log
```

Read the **last** traceback — the log is append-only and unrotated, so it holds
every previous failure too.

| What you see | What to do |
|---|---|
| `audit chain verification FAILED at seq N` | §4 |
| `no market data providers configured` | Add a provider under `data.providers` |
| `credential 'X' not found in vault` | `poseidon vault set X` |
| `wrong passphrase or corrupt vault` | Retype; if it persists, restore `vault.bin` from backup (§1) |
| `AttributeError` / `ImportError` inside `start()` | A bad upgrade. `git -C ~/Poseidon log --oneline -5`, then `git revert <sha>` or check out the last known-good tag |
| Config validation error | `poseidon config validate` prints the offending key |

If an update landed just before the failure, suspect it first:

```bash
git -C ~/Poseidon log --oneline -5
git -C ~/Poseidon revert --no-edit <bad-sha>
```

---

## 4. The audit chain is broken

Startup **refuses** on a broken chain — deliberately. Do not delete the DB as a
first move; scope the damage first.

```bash
.venv/bin/poseidon audit verify        # reports the first bad seq
```

1. **Scope it.** Note the reported `seq`. Rows before it are intact.
2. **Preserve the evidence.** `cp poseidon.db poseidon.db.broken-$(date +%s)` —
   before anything else. If this was tampering rather than corruption, that file
   is the only record.
3. **Decide:**
   - *Corruption* (disk error, killed mid-write, restored from an inconsistent
     copy): restore `poseidon.db` from backup (§1). Trading history since the
     backup is lost; the vault is unaffected.
   - *Tampering, or unknown*: **stop.** Do not start the engine. Treat the host
     as suspect — the audit chain is the only tamper-evidence there is.
4. **Last resort**, accepting the loss of all trading history:
   `mv poseidon.db poseidon.db.old && poseidon doctor` to recreate.

The chain detects modification and reordering of retained rows. It **cannot**
detect truncation of the newest rows or a full table wipe — that is documented,
not a surprise, and it is why an off-host backup matters.

---

## 5. The broker disconnects

Symptoms: `Broker disconnected` notification, or `poseidon doctor` reporting the
broker unhealthy.

1. Is it them or you? Check the broker's status page and your own connectivity.
2. `poseidon doctor` — distinguishes auth failure from unreachability.
3. **Auth failure** (`401`): the key was revoked or rotated at the broker.
   Reissue it and `poseidon vault set <credential>`.
4. **Unreachable**: portfolio sync retries with backoff on its own. After 120s
   of stale state the risk engine refuses **all** orders — including exits —
   which is intended (no data, no trade) but means positions are unmanaged
   while it lasts.
5. If you need out of a position during an outage, use the broker's own web UI.
   That is the honest answer: Poseidon cannot trade through a broker it cannot
   reach.

Note a 403 from Alpaca is usually a *trade permission* rejection (insufficient
buying power, order too small), **not** an auth failure — the message body says
which.

---

## 6. The model backend is down or degraded

The local-model case (`ai.backend: openai_compatible`) has a failure mode worth
knowing: **the server can be up while the model is not loaded.**

```bash
.venv/bin/poseidon doctor      # checks the configured model is actually served
```

| Symptom | Cause | Fix |
|---|---|---|
| `unreachable at http://…` | LM Studio not running | Start it |
| `reachable … but it is not serving 'X'` | Another model holds the VRAM | Load the configured model, or change `ai.model` |
| Cycles run but always `no_action` | Model refusing, emitting prose, or hitting the tool-iteration limit | Check `ai_usage`; try a stronger model |
| `Component error: review_cycle` toasts every 5 min | Cycles failing; the notification is deduped | Read the log for the real error |

**Nothing trades while the model is down.** That is safe — no decisions means no
orders — but existing positions still rely on the guardian's stops, which run
independently of the model.

---

## 7. HALT, and getting back

**HALT** (dashboard header) opens the circuit breaker and **cancels every
resting broker order — including the guardian's protective stops.** With the
default `risk.flatten_on_halt: false` the book is left *unprotected*, not flat.

After halting:

1. Decide whether you want flat or merely stopped. To close everything, either
   set `risk.flatten_on_halt: true` before halting, or exit manually.
2. While halted, **the guardian's stops are gone.** Watch the positions, or
   close them.
3. **Resume** clears the breaker and the halt file. The guardian re-arms from
   the stored exit plans on the next tick.

A halt survives a restart — it is latched in the DB and a `HALT` file in
`data_dir`. If the engine refuses to trade after a restart and you do not know
why, check for that file.

---

## 8. Routine checks

| When | Command | Looking for |
|---|---|---|
| Before starting after any upgrade | `poseidon doctor` | every check green, model actually served |
| Weekly | backup (§1) | vault + DB copied and **verified** |
| Weekly | `poseidon audit verify` | chain intact |
| Monthly | `du -h <data_dir>/poseidon.db` | growth (~1 GB/yr at a 60s cadence; there is no retention policy for `decisions`, `orders`, `audit`, `equity_marks` or `ai_usage`, and no `VACUUM`) |
| After any credential exposure | `poseidon vault rekey` (§2) | new passphrase in use everywhere |

---

## 9. What this platform does not do

Known gaps, so you are not surprised by them at 3am:

- **No HA.** Single host, single process. If it is down, nothing trades and
  nothing watches your stops.
- **No `/healthz`.** Health is inside `GET /api/status`; there is no endpoint an
  external watchdog can poll for a status code.
- **No automatic DB retention or vacuum.** See §8.
- **The desktop notification channel only reaches a logged-in session.** If you
  run unattended, configure a second channel (webhook, telegram, email) — every
  critical alert otherwise terminates at a toast nobody sees.
- **No vault passphrase recovery.** Rotation, yes (§2); recovery, no.
