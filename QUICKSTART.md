# Quickstart

From zero to an audited, queryable ledger of AI token spend in about five minutes.

Chinese version: [QUICKSTART.zh-CN.md](QUICKSTART.zh-CN.md).

> Every command in this file was executed end-to-end on Windows/PowerShell with Python 3.12 before
> being written down. macOS and Linux need the same commands with POSIX paths.

---

## 0. Requirements

- **Python 3.11+** (the standard-library `tomllib` is required).
- **git** on your `PATH`. The ledger keeps its own local repo as a second, independent audit trail.
- **No third-party packages.** The runtime dependency list is empty.

## 1. Install

```bash
cd token_ledger
python -m venv .venv

# Windows
.venv\Scripts\Activate.ps1
# macOS / Linux
source .venv/bin/activate

pip install -e .
tledger --version        # -> tokenledger 0.1.0
```

**Without installing** (also fine — it is a plain Python package):

```bash
# Windows
$env:PYTHONPATH = "C:\path\to\token_ledger\src"
# macOS / Linux
export PYTHONPATH=/path/to/token_ledger/src

python -m tokenledger --help
```

## 2. Create the ledger

```bash
tledger init ~/ai-ledger
cd ~/ai-ledger
```

`init` creates:

```
tokenledger.toml          # configuration (prices, budgets, attestation)
ledger/usage.jsonl        # append-only usage vouchers  <- the source of truth
ledger/authorization.jsonl# append-only budget vouchers
ledger/attestation.jsonl  # source-log attestations
ledger/index.sqlite       # derived query index; rebuildable, never authoritative
.git/                     # local repo, one commit per booking
```

Every command finds the ledger by walking up from the current directory looking for
`tokenledger.toml`. From elsewhere, pass `--repo <dir>` or set `$TOKENLEDGER_REPO`.

## 3. Authorize before the spend

Internal control rule: nothing is spent before it is approved.

```bash
# Dollar budget (needs a price table, see section 6)
tledger budget set --agent "codex*" --from 2026-09-01 --to 2026-09-30 \
    --limit-usd 50 --approved-by alice

# Token budget (works with no price table at all)
tledger budget set --agent "claude*" --limit-tokens 1000000 --approved-by alice

tledger budget list      # issued authorization vouchers
tledger budget status    # limit execution: used vs. authorized
```

Over-limit usage is **still booked** and flagged `exceeded`. The ledger never silently
drops a spend.

## 4. Attest your source logs (recommended)

The hash chain proves a voucher was not altered *after booking*. It says nothing about whether
the log was altered *before ingestion*. `attest` closes that gap.

```bash
# 1. Generate a key. Keep it OUTSIDE the ledger repo.
tledger attest keygen --out ~/.secrets/tokenledger.key

# 2. Point the ledger at it, so `ingest` can re-verify at booking time.
#    In tokenledger.toml:
#        [attestation]
#        key_file = "~/.secrets/tokenledger.key"

# 3. Sign the logs
tledger attest sign ~/.codex/sessions --signer ops --note "september batch"

# 4. Check at any time
tledger attest list
tledger attest verify
```

> **Trap:** `ingest` has no `--key` flag. It resolves the key from `key_file` in
> `tokenledger.toml` (default: the user config dir). If you sign with `--key` but never set
> `key_file`, every voucher is booked as `no_key` — the ledger reports
> `有证明但缺密钥` / "attested but key missing" instead of `verified`.
> Set `key_file` first.

Once the logs are signed, the vouchers carry an attestation status:

| Status | Meaning |
| --- | --- |
| `verified` | File hash and HMAC both match; the log is as it was when signed. |
| `changed` | The log was edited **after** signing. Treated as a failure. |
| `no_key` | An attestation exists but the key cannot be found. |
| `invalid` | The HMAC does not match the recorded signature. |
| `unsigned` | No attestation covers this file. |

Set `require = true` under `[attestation]` in `tokenledger.toml` to refuse any log that
does not pass (exit code `6`). Or pass `--require-attestation` per run.

## 5. Book usage from logs

### Codex CLI rollout logs (built in)

```bash
tledger ingest codex --since 2026-09-01
```

Auto-detects `$CODEX_HOME/sessions`, then `~/.codex/sessions`. Override with `--path`.

### Any other agent (generic JSONL)

One JSON object per line, field names mapped with `--map logical_field=json_key`:

```bash
tledger ingest generic --path thirdparty.jsonl \
    --map occurred_at=when --map agent_id=who --map model=m \
    --map session_id=sess --map input_tokens=in --map output_tokens=out
```

Logical field names and the JSON keys recognised by default (case-insensitive):

| Logical field | Default JSON keys |
| --- | --- |
| `occurred_at` | `occurred_at`, `timestamp`, `time`, `ts`, `date` |
| `agent_id` | `agent_id`, `agent`, `agent_name` |
| `provider` | `provider`, `model_provider` |
| `agent_version` | `agent_version`, `version` |
| `model` | `model`, `model_name` |
| `session_id` | `session_id`, `session`, `conversation_id` |
| `turn_id` | `turn_id`, `turn`, `request_id` |
| `project` | `project`, `workspace`, `cwd` |
| `input_tokens` | `input_tokens`, `prompt_tokens`, `input` |
| `output_tokens` | `output_tokens`, `completion_tokens`, `output` |
| `cached_input_tokens` | `cached_input_tokens`, `cached_tokens`, `cache_read_input_tokens` |
| `cache_write_input_tokens` | `cache_write_input_tokens`, `cache_creation_input_tokens` |
| `reasoning_output_tokens` | `reasoning_output_tokens`, `reasoning_tokens` |
| `total_tokens` | `total_tokens`, `total` |
| `dedup_key` | `dedup_key`, `event_id`, `id`, `request_id` |

**`--map` only matches top-level keys, case-insensitively. Nested paths such as
`usage.prompt_tokens` are not supported** — flatten the log first.

Safe to re-run: each event gets a dedup key, so re-ingesting the same file reports
`跳过 N 条（已存在）` and books nothing twice.

Useful flags: `--dry-run` (preview the exact vouchers as JSON, write nothing),
`--since` / `--until` (UTC days), `--limit N`, `--no-commit`, `--strict`,
`--require-attestation`.

## 6. Make dollars meaningful

The price table starts **empty on purpose** — tokenledger will not guess vendor prices.
Until you add prices, cost is `unpriced` and budget status is `indeterminate`, which is
*not* the same as "within budget".

Add prices in `tokenledger.toml` (per million tokens):

```toml
[pricing]
currency = "USD"
cached_input_included_in_input = true   # true for Codex / OpenAI accounting

[pricing.models."claude-sonnet-4"]
input = 3.0
output = 15.0
cached_input = 0.30
cache_write = 3.75
```

With a dollar budget and unpriced usage, `--strict` will **not** return `5` — there is
nothing to compare against. Use `--limit-tokens` if you want a hard stop that needs no
price table.

## 7. Read the results

```bash
tledger report --group-by agent      # agent | model | project | day | session |
                                     # provider | parser | voucher |
                                     # cost_status | budget_status | attestation
tledger report --exceptions          # over budget / unbudgeted / unpriced /
                                     # unattested / tampered, in one place
tledger query --agent codex --since 2026-09-01
tledger query --budget-status exceeded
tledger query --cost-status unpriced
tledger query --attestation changed
tledger export --format csv  --out usage.csv
tledger export --format jsonl --kind authorization --out auth.jsonl
tledger history                      # the ledger's own git log
```

`report` and `query` also accept `--format json` and `--format csv` for piping into
other tools.

## 8. Audit

```bash
tledger verify              # hash chain, numbering gaps, index parity,
                            # session reconciliation, attestation coverage
tledger reindex             # rebuild the SQLite index from usage.jsonl
tledger reconcile --bill provider-bill.csv
```

`verify` reads only the append-only JSONL, so it still works if the index is deleted or
corrupt. If a voucher has been edited, it reports the mismatch and exits `3`.

## 9. Automation and exit codes

```bash
tledger ingest codex --since 2026-09-01 --strict
```

| Exit code | Meaning |
| --- | --- |
| `0` | OK |
| `1` | Usage or I/O error |
| `3` | Audit failed (hash chain / index / reconciliation) |
| `4` | Reconciliation mismatch against a provider bill |
| `5` | Budget exceeded (`--strict` only; the booking still happened) |
| `6` | Attestation not satisfied (`--require-attestation` or `require = true`) |

## 10. Pitfalls

- **Empty price table.** Dollar budgets stay `indeterminate` and `--strict` never fires.
  Configure prices (§6) or use `--limit-tokens`.
- **`key_file` not set.** Signing works, but ingestion cannot verify and marks vouchers
  `no_key`. See §4.
- **`--map` does not do nesting.** Top-level keys only.
- **Keep the attestation key out of the ledger repo.** If it leaks into a commit, anyone
  who can write a log can forge a matching attestation.
- **Attestation is not non-repudiation.** It is an HMAC held by whoever operates the
  ledger. It reliably catches accidental corruption and third-party edits of a log, but it
  cannot stop the machine that *runs* the agent from fabricating usage in the first place.
  That requires an agent-side private key (on the roadmap).
- **Editing a signed log is not silent.** It flips the voucher to `changed`, fails
  `tledger verify`, and changes the hash chain — two independent controls trip at once.

## Where to go next

- [README.md](README.md) — full command reference and design notes
- [docs/internal-control.md](docs/internal-control.md) — which internal-control principle
  each mechanism implements, and what is deliberately *not* claimed