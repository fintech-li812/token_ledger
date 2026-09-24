# 快速上手

从零到一本可审计、可查询的 AI token 账本，大约五分钟。

English version: [QUICKSTART.md](QUICKSTART.md)。

> 本文里的每一条命令，在写下来之前都用 Python 3.12 在 Windows/PowerShell 上真跑过一遍。
> macOS 和 Linux 换成对应路径即可，命令本身一样。

---

## 0. 环境要求

- **Python 3.11+**（依赖标准库的 `tomllib`）。
- **git** 在 `PATH` 里。账本自己维护一个本地仓库，作为第二重独立留痕。
- **零第三方依赖。** 运行时依赖列表是空的。

## 1. 安装

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

**不想装也行**（它就是个普通 Python 包）：

```bash
# Windows
$env:PYTHONPATH = "C:\path\to\token_ledger\src"
# macOS / Linux
export PYTHONPATH=/path/to/token_ledger/src

python -m tokenledger --help
```

## 2. 建账本

```bash
tledger init ~/ai-ledger
cd ~/ai-ledger
```

`init` 会生成：

```
tokenledger.toml           # 配置（价格、预算、来源证明）
ledger/usage.jsonl         # 只增不改的用量凭证  <- 唯一真相
ledger/authorization.jsonl # 只增不改的授权凭证
ledger/attestation.jsonl   # 来源日志证明
ledger/index.sqlite        # 派生索引，可重建，绝不是权威
.git/                      # 本地仓库，每次入账一次提交
```

所有命令都会从当前目录往上找 `tokenledger.toml` 来定位账本。在别处执行时用 `--repo <目录>`，
或设置 `$TOKENLEDGER_REPO`。

## 3. 先授权，后花钱

内控铁律：未经批准，不得支出。

```bash
# 金额预算（需要价格表，见第 6 节）
tledger budget set --agent "codex*" --from 2026-09-01 --to 2026-09-30 \
    --limit-usd 50 --approved-by alice

# token 预算（完全不需要价格表）
tledger budget set --agent "claude*" --limit-tokens 1000000 --approved-by alice

tledger budget list      # 已签发的授权凭证
tledger budget status    # 授权执行情况：已用 vs 额度
```

超限的用量**照常入账**并标记 `exceeded`，账本绝不静默丢弃一笔支出。

## 4. 给来源日志做证明（建议做）

哈希链证明的是"凭证在入账**之后**没被改"，它对"日志在入账**之前**就被改过"一无所知。
`attest` 补的就是这个缺口。

```bash
# 1. 生成密钥。放在账本仓库之外。
tledger attest keygen --out ~/.secrets/tokenledger.key

# 2. 让账本找到它，这样 ingest 才能在入账时复核。
#    在 tokenledger.toml 里：
#        [attestation]
#        key_file = "~/.secrets/tokenledger.key"

# 3. 给日志签名
tledger attest sign ~/.codex/sessions --signer ops --note "september batch"

# 4. 随时核对
tledger attest list
tledger attest verify
```

> **坑：** `ingest` 没有 `--key` 参数。它只从 `tokenledger.toml` 的 `key_file` 读密钥
> （默认在用户配置目录）。如果你用 `--key` 签了名却没配 `key_file`，所有凭证都会被记成
> `no_key`，账本报告 `有证明但缺密钥` 而不是 `verified`。先配好 `key_file`。

日志签名之后，凭证会带上来源证明状态：

| 状态 | 含义 |
| --- | --- |
| `verified` | 文件哈希与 HMAC 双双通过，日志与签名时一致。 |
| `changed` | 日志在签名**之后**被改动过。按失败处理。 |
| `no_key` | 证明存在，但找不到密钥。 |
| `invalid` | HMAC 与登记的签名不符。 |
| `unsigned` | 没有任何证明覆盖这个文件。 |

在 `tokenledger.toml` 的 `[attestation]` 下把 `require` 设为 `true`，没通过证明的日志会被直接
拒绝入账（退出码 `6`）。也可以单次加 `--require-attestation`。

## 5. 从日志入账

### Codex CLI 日志（内置）

```bash
tledger ingest codex --since 2026-09-01
```

自动探测 `$CODEX_HOME/sessions`，其次 `~/.codex/sessions`。可用 `--path` 覆盖。

### 其他 agent（通用 JSONL）

每行一个 JSON 对象，用 `--map 逻辑字段=日志键名` 建立映射：

```bash
tledger ingest generic --path thirdparty.jsonl \
    --map occurred_at=when --map agent_id=who --map model=m \
    --map session_id=sess --map input_tokens=in --map output_tokens=out
```

逻辑字段名，以及默认就能识别的日志键名（不区分大小写）：

| 逻辑字段 | 默认识别的键名 |
| --- | --- |
| `occurred_at` | `occurred_at`、`timestamp`、`time`、`ts`、`date` |
| `agent_id` | `agent_id`、`agent`、`agent_name` |
| `provider` | `provider`、`model_provider` |
| `agent_version` | `agent_version`、`version` |
| `model` | `model`、`model_name` |
| `session_id` | `session_id`、`session`、`conversation_id` |
| `turn_id` | `turn_id`、`turn`、`request_id` |
| `project` | `project`、`workspace`、`cwd` |
| `input_tokens` | `input_tokens`、`prompt_tokens`、`input` |
| `output_tokens` | `output_tokens`、`completion_tokens`、`output` |
| `cached_input_tokens` | `cached_input_tokens`、`cached_tokens`、`cache_read_input_tokens` |
| `cache_write_input_tokens` | `cache_write_input_tokens`、`cache_creation_input_tokens` |
| `reasoning_output_tokens` | `reasoning_output_tokens`、`reasoning_tokens` |
| `total_tokens` | `total_tokens`、`total` |
| `dedup_key` | `dedup_key`、`event_id`、`id`、`request_id` |

**`--map` 只匹配顶层键，且不区分大小写，不支持 `usage.prompt_tokens` 这类嵌套路径**
—— 请先把日志拍平。

重复执行是安全的：每条事件都有去重键，同一份日志再采一次只会显示
`跳过 N 条（已存在）`，不会重复入账。

常用参数：`--dry-run`（以 JSON 预览将要入账的凭证，不写账）、`--since` / `--until`（UTC 日）、
`--limit N`、`--no-commit`、`--strict`、`--require-attestation`。

## 6. 让金额真正算得出来

价格表**默认是空的，这是故意的** —— tokenledger 不会替你猜厂商单价。在你填入价格之前，
成本标记为 `unpriced`、授权状态为 `indeterminate`，这**不等于**"未超限"。

在 `tokenledger.toml` 里按每百万 token 填价格：

```toml
[pricing]
currency = "USD"
cached_input_included_in_input = true   # Codex / OpenAI 的记账口径为 true

[pricing.models."claude-sonnet-4"]
input = 3.0
output = 15.0
cached_input = 0.30
cache_write = 3.75
```

在配了金额预算、但用量未定价的情况下，`--strict` **不会**返回 `5` —— 没有可比对象。
想要一个不依赖价格表的硬性刹车，就用 `--limit-tokens`。

## 7. 看结果

```bash
tledger report --group-by agent      # agent | model | project | day | session |
                                     # provider | parser | voucher |
                                     # cost_status | budget_status | attestation
tledger report --exceptions          # 超预算 / 无授权 / 未定价 /
                                     # 来源无证明 / 被篡改，一屏看完
tledger query --agent codex --since 2026-09-01
tledger query --budget-status exceeded
tledger query --cost-status unpriced
tledger query --attestation changed
tledger export --format csv  --out usage.csv
tledger export --format jsonl --kind authorization --out auth.jsonl
tledger history                      # 账本自身的 git 提交史
```

`report` 和 `query` 也支持 `--format json` 与 `--format csv`，方便接别的工具。

## 8. 审计

```bash
tledger verify              # 哈希链、凭证缺号、索引一致性、
                            # 会话账实核对、来源证明覆盖率
tledger reindex             # 从 usage.jsonl 重建 SQLite 索引
tledger reconcile --bill provider-bill.csv
```

`verify` 只读那份只增不改的 JSONL，所以索引被删了或坏了它照样能跑。凭证一旦被改动，
它会报出不一致并以退出码 `3` 结束。

## 9. 挂进自动化与退出码

```bash
tledger ingest codex --since 2026-09-01 --strict
```

| 退出码 | 含义 |
| --- | --- |
| `0` | 正常 |
| `1` | 用法或 I/O 错误 |
| `3` | 审计不通过（哈希链 / 索引 / 账实核对） |
| `4` | 与供应商账单对账不符 |
| `5` | 超预算（仅 `--strict`，而且账已经记了） |
| `6` | 来源证明不满足（`--require-attestation` 或 `require = true`） |

## 10. 已知的坑

- **价格表空着。** 金额预算会一直是 `indeterminate`，`--strict` 永远不触发。
  要么配价格（第 6 节），要么用 `--limit-tokens`。
- **没配 `key_file`。** 签名能成功，但入账时无法复核，凭证全被记成 `no_key`。见第 4 节。
- **`--map` 不支持嵌套。** 只认顶层键。
- **签名密钥不要留在账本仓库里。** 一旦泄漏进提交，任何能写日志的人都能伪造出匹配的证明。
- **来源证明不是不可否认性。** 它是运维者持有的 HMAC。它能可靠地抓住日志的意外损坏和
  第三方改动，但挡不住"跑 agent 的那台机器从一开始就编造用量"。后者需要 agent 侧独立
  私钥（在路线图上）。
- **改动静默不了。** 改动已签名的日志会让凭证翻成 `changed`、`tledger verify` 不通过、
  哈希链断裂 —— 两个独立控制点会同时报警。

## 接下来看什么

- [README.zh-CN.md](README.zh-CN.md) — 完整命令参考与设计说明
- [docs/internal-control.md](docs/internal-control.md) — 每个机制对应哪条内控原则，
  以及我们刻意**不**主张什么