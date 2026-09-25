# tokenledger

![ci](https://github.com/fintech-li812/token_ledger/actions/workflows/ci.yml/badge.svg)

An auditable token-usage ledger for AI agents, built on enterprise internal-control principles:
**vouchers, authorization, reconciliation, and a tamper-evident audit trail**.

> Every token spent should be able to answer: how much, by whom, authorized by whom, was it over budget, and has anyone changed the record since?

中文文档见 [README.zh-CN.md](README.zh-CN.md)。

## What it does

- **Where the money went** — report by agent, model, project, day, session, provider, or cost/attestation status.
- **Who spent it** — each usage voucher carries the agent identity (originator, provider, version), session ID, and working directory.
- **Where the data comes from** — log parsing, fully local. A Codex CLI rollout parser is built in; a generic JSONL parser accepts a field mapping for any other agent.
- **Who approved it** — authorization (budget) vouchers live in their own append-only stream. Usage is compared against them at booking time. Over-limit vouchers are **still booked, but flagged**.
- **Whether the log was tampered with** — `attest` signs source logs with HMAC-SHA256 and the ledger re-verifies them at ingestion. A log edited after signing is marked `changed` and fails the audit.
- **Whether the ledger itself was altered** — vouchers are linked by a SHA-256 hash chain. `tledger verify` detects any post-booking edit, gap in numbering, or index drift.

## Install

```bash
git clone https://github.com/fintech-li812/token_ledger.git && cd token_ledger
python -m pip install -e .
tledger --version
```

Requires Python 3.11+ (uses the standard-library `tomllib`). **Zero runtime dependencies.**

## Quickstart

> A step-by-step walkthrough, with every command individually verified and a list of
> known pitfalls, lives in [QUICKSTART.md](QUICKSTART.md).

```bash
tledger init ~/ai-ledger          # creates the ledger, a git repo, and an initial commit
cd ~/ai-ledger

# 1) Authorize first: give codex 50 USD for September
tledger budget set --agent "codex*" --from 2026-09-01 --to 2026-09-30 --limit-usd 50 --approved-by me

# 2) (Recommended) Attest the source logs before booking anything
tledger attest keygen --out ~/.secrets/tokenledger.key
tledger attest sign ~/.codex/sessions --signer ops

# 3) Ingest usage from the logs (defaults to $CODEX_HOME/sessions or ~/.codex/sessions)
tledger ingest codex --since 2026-09-01

# 4) Report
tledger report --group-by agent

# 5) Audit
tledger verify
```

## Commands

| Command | Purpose |
| --- | --- |
| `tledger init [DIR]` | Create the ledger, git repo, and initial commit |
| `tledger budget set / list / status` | Issue authorization vouchers; review limit execution |
| `tledger attest keygen / sign / list / verify` | Cryptographic attestation of source logs |
| `tledger record` | Book a single usage voucher by hand (fallback for agents that self-report) |
| `tledger ingest codex --path P` | Parse Codex rollout logs and book them |
| `tledger ingest generic --path P --map k=v` | Parse a generic JSONL log |
| `tledger query` | Voucher-level detail query |
| `tledger report --group-by agent\|model\|project\|day\|session\|attestation` | Aggregated report |
| `tledger report --exceptions` | Exception report: over budget, unbudgeted, unpriced, unattested, tampered |
| `tledger reconcile --bill FILE` | Reconcile against a provider bill (CSV) |
| `tledger verify` | Hash chain, numbering gaps, index parity, session reconciliation, attestation coverage |
| `tledger reindex` | Rebuild the SQLite index from the ledger |
| `tledger export --format csv\|jsonl` | Export the ledger |
| `tledger history` | Git history of the ledger |

Exit codes: `0` ok, `1` usage/IO error, `3` audit failed, `4` reconciliation mismatch, `5` budget exceeded, `6` attestation not satisfied.

## Source attestation

The hash chain proves a voucher was not altered *after booking*. It says nothing about whether the
source log was altered *before ingestion*. `tledger attest` covers that gap:

```bash
tledger attest keygen --out ~/.secrets/tokenledger.key   # keep the key OUTSIDE the ledger repo
tledger attest sign ~/.codex/sessions --signer ops       # register HMAC-SHA256 attestations
tledger ingest codex                                     # verify each file while booking
tledger report --group-by attestation                    # report by attestation status
```

Every usage voucher records a source status:

| Status | Meaning |
| --- | --- |
| `verified` | Attestation found, file hash matches, HMAC recomputes |
| `changed` | Attestation found, file hash differs — **the log was edited after signing** |
| `no_key` | Attestation found and hash matches, but this machine has no key to recompute the HMAC |
| `invalid` | Attestation found and hash matches, but the HMAC does not — the record is inconsistent |
| `unsigned` | No attestation covers this file |

`changed` and `invalid` make `tledger verify` fail (exit code 3). For a hard policy, use
`ingest --require-attestation`: logs that do not satisfy attestation are **refused** (exit code 6).

Reproduce the effect yourself (always on a copy, never on the live logs):

```bash
tledger attest sign ./logs && tledger ingest codex --path ./logs   # everything verified
echo '{"...a forged usage event..."}' >> ./logs/rollout.jsonl      # tamper after signing
tledger ingest codex --path ./logs                                 # the new voucher is changed
tledger verify                                                     # exit code 3
```

In testing, the forged event tripped two independent controls at once: the attestation status became
`changed`, and the "sum of per-turn deltas vs the log's cumulative total" reconciliation went out of
balance. Two controls, one fraud, caught twice.

**Boundary (stated plainly):** this is not non-repudiation. The key is held by the operator, so it
proves "this log has not changed since it was signed" and "which key signed it". Stopping the machine
that runs the agent from lying requires the agent to sign with its own private key — that is on the
roadmap.

## Pricing

The price table is **empty by default**: this project does not guess anyone's prices. Fill in what
you have verified yourself:

```toml
[pricing]
currency = "USD"

[pricing.models."your-model"]     # wildcards work, e.g. "gpt-4*"
input = 0.0          # per million tokens
output = 0.0
cached_input = 0.0
cache_write = 0.0
```

Unpriced usage is recorded as `cost.status = "unpriced"` and listed in `report --exceptions`. It is
**never silently treated as zero cost**, and it cannot make a budget look satisfied — the budget
status becomes `indeterminate` instead.

## Ledger layout

```
<ledger>/
├─ tokenledger.toml         # pricing, ledger location, auto-commit, attestation key
├─ ledger/
│  ├─ usage.jsonl           # usage vouchers        (source of truth)
│  ├─ authorization.jsonl   # budget vouchers       (source of truth)
│  ├─ attestation.jsonl     # source attestations   (source of truth)
│  └─ index.sqlite          # derived index, rebuildable, git-ignored
└─ .git/                    # ledger history (one commit per ingest run)
```

A usage voucher:

```json
{"schema":1,"voucher_no":"TL-2026-000001","kind":"usage","recorded_at":"2026-09-24T14:20:00Z",
 "prev_hash":"0000...","payload":{"occurred_at":"2026-09-24T14:19:22Z","occurred_day":"2026-09-24",
 "agent":{"id":"codex-tui","provider":"deepseek","version":"0.156.1"},"model":"deepseek-v4-flash",
 "project":"token_ledger","session":{"id":"01a0...","turn_id":"01a0...","ordinal":13},
 "usage":{"input_tokens":11919,"cached_input_tokens":5888,"output_tokens":337,"total_tokens":12256},
 "usage_cumulative":{"total_tokens":12256},
 "cost":{"status":"priced","currency":"USD","amount":0.0031},
 "budget":{"status":"within","budget_ids":["TA-2026-000001"]},
 "source":{"parser":"codex","file":"rollout-....jsonl","sha256":"...",
           "attestation":{"status":"verified","key_id":"2ed91d912a4b6c23"}},
 "dedup_key":"codex:01a0...:13","recorded_by":"tledger/0.1.0 ingest:codex","approved_by":null},
 "hash":"...."}
```

## Design stances

- **The ledger, not the index, is the truth.** SQLite is a derived cache; `reindex` rebuilds it. That is what makes reconciliation meaningful.
- **Exceptions are surfaced, never hidden.** Over-budget, unpriced, unattested, and tampered vouchers are all booked and flagged.
- **Append-only.** There is no update or delete. Corrections are new reversing vouchers (a red-ink reversal, in accounting terms).
- **The seal is not kept with the ledger.** The signing key lives outside the repo, or anyone who can edit the ledger could also re-sign the evidence.
- **Local-first.** Nothing leaves the machine; there is no cloud component.

## Limits and roadmap

- Only the Codex CLI log format is parsed out of the box; other agents need a small parser or a field mapping.
- No multi-user access control: it assumes a single trusted operator.
- Attestation is operator-keyed HMAC, not non-repudiation (see above). Agent-side signing keys are the roadmap item.
- The ledger records bookkeeping order, not strictly event order; backfilling historical logs is normal and does not break the chain.

## Documentation

- `docs/internal-control.md` — internal-control mapping, control objectives, and walkthrough tests (Chinese).
- `README.zh-CN.md` — Chinese README.
- `QUICKSTART.md` — step-by-step walkthrough (English).
- `QUICKSTART.zh-CN.md` — step-by-step walkthrough (Chinese).

## License

MIT