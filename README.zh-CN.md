# tokenledger

审计级 AI token 用量账本 —— 把企业内控的四根柱子（**凭证、授权、对账、审计留痕**）搬到 AI 成本治理上。

> 每一笔 token 消耗，都要能回答：花了多少、谁花的、谁批的、有没有超标、有没有被改过。

英文版见 [README.md](README.md)。

## 它解决什么

- **钱去哪了**：按 agent / 模型 / 项目 / 日期 / 会话任意维度出账。
- **谁花的**：每条用量凭证带 agent 身份（originator、provider、版本）、会话 ID、工作目录。
- **数据从哪来**：解析本机 agent 日志（内置 Codex rollout 解析器，通用 JSONL 可映射字段），纯本地、不走网络。
- **谁批的**：授权（预算）凭证独立成册，用量自动比对，超限凭证**照常入账但标记异常**。
- **有没有被动过**：凭证之间用哈希链相连，`tledger verify` 一条命令检出任何事后修改、缺号、索引错配。

## 安装

```bash
git clone <this-repo> && cd token_ledger
python -m pip install -e .
tledger --version
```

要求 Python >= 3.11（用到标准库 `tomllib`），**运行时零第三方依赖**。

## 30 秒上手

> 逐条命令都实测过的完整上手流程，附已知的坑：[QUICKSTART.zh-CN.md](QUICKSTART.zh-CN.md)。

```bash
tledger init ~/ai-ledger          # 建账本目录（含本地 git 仓库 + 初始提交）
cd ~/ai-ledger

# 1) 先授权：给 codex 一个 30 天 50 美元的额度
tledger budget set --agent codex-tui --from 2026-09-01 --to 2026-09-30 --limit-usd 50 --approved-by me

# 2) 再从日志采集用量（默认读 $CODEX_HOME/sessions 或 ~/.codex/sessions）
tledger ingest codex --since 2026-09-01

# 3) 出账
tledger report --group-by agent

# 4) 审计
tledger verify
```

## 命令一览

| 命令 | 作用 |
| --- | --- |
| `tledger init [DIR]` | 建账本 + git 仓库 + 初始提交 |
| `tledger budget set / list / status` | 签发授权凭证、查看额度执行情况 |
| `tledger record` | 手工补录一笔用量凭证（agent 主动上报的兜底通道） |
| `tledger ingest codex --path P` | 解析 Codex rollout 日志批量入账 |
| `tledger ingest generic --path P --map k=v` | 解析通用 JSONL 日志入账 |
| `tledger query` | 凭证级明细查询 |
| `tledger report --group-by agent\|model\|project\|day\|session` | 汇总报表 |
| `tledger report --exceptions` | 例外报告：超预算 / 未定价 / 无授权 |
| `tledger reconcile --bill FILE` | 与供应商账单对账 |
| `tledger attest keygen/sign/list/verify` | 来源日志的密码学证明（入账前的可信度） |
| `tledger verify` | 哈希链 + 缺号 + 索引一致性审计 + 来源证明覆盖 |
| `tledger reindex` | 从明细账重建 SQLite 索引 |
| `tledger export --format csv\|jsonl` | 导出 |

退出码：`0` 正常；`1` 参数或 IO 错误；`3` 审计不通过；`4` 对账差异超容差；`5` 预算超限；`6` 来源证明未通过。

## 定价

价格表**默认是空的**：本项目不猜测任何厂商的单价。请在账本目录的 `tokenledger.toml` 里填自己核对过的价格：

```toml
[pricing]
currency = "USD"

[pricing.models."deepseek-chat"]   # 支持通配符，如 "gpt-4*"
input = 0.0          # 单位：每百万 token
output = 0.0
cached_input = 0.0
cache_write = 0.0
```

未定价的凭证会记为 `cost.status = "unpriced"`，并在 `report --exceptions` 中列出，**不会被悄悄当成 0 元**。

## 账本结构

```
<账本目录>/
├─ tokenledger.toml        # 配置：定价、账本位置、是否自动 git 提交
├─ ledger/
│  ├─ usage.jsonl          # 用量凭证（明细账，唯一真相来源）
│  ├─ authorization.jsonl  # 授权 / 预算凭证
│  └─ index.sqlite         # 派生索引，可重建，不纳入版本控制
└─ .git/                   # 账本历史（默认每次入账自动提交）
```

单条用量凭证长这样：

```json
{"schema":1,"voucher_no":"TL-2026-000001","kind":"usage","recorded_at":"2026-09-24T14:20:00Z",
 "prev_hash":"0000...","payload":{"occurred_at":"2026-09-24T14:19:22Z","occurred_day":"2026-09-24",
 "agent":{"id":"codex-tui","provider":"deepseek","version":"0.156.1"},
 "model":"deepseek-chat","project":"token_ledger",
 "session":{"id":"01a0...","turn_id":"01a0...","ordinal":13,"cwd":"C:\\workspace"},
 "usage":{"input_tokens":11919,"cached_input_tokens":5888,"output_tokens":337,
          "reasoning_output_tokens":276,"total_tokens":12256},
 "usage_cumulative":{"total_tokens":12256},
 "cost":{"status":"priced","currency":"USD","amount":0.0031},
 "budget":{"status":"within","budget_ids":["TA-2026-000001"]},
 "source":{"parser":"codex","file":"rollout-....jsonl","sha256":"..."},
 "dedup_key":"codex:01a0...:13","recorded_by":"tledger/0.1.0 ingest:codex","approved_by":null},
 "hash":"...."}
```

## 内控映射

| 内控要素 | 账本里的实现 |
| --- | --- |
| 凭证与记录 | 每笔用量一张凭证，编号 `TL-YYYY-NNNNNN`，只增不改、连续无缺号 |
| 授权审批 | `budget set` 签发授权凭证 `TA-YYYY-NNNNNN`，限定 agent / 项目 / 期间 / 额度 |
| 不相容职务分离 | 凭证分别记录 `recorded_by`（采集端）与 `approved_by`（授权人） |
| 账实核对 | 明细账（JSONL）为准，SQLite 索引可由 `reindex` 重建 |
| 外部对账 | `reconcile` 与供应商账单逐项比对，支持金额与比例容差 |
| 审计留痕 | 哈希链 + git 提交历史，双份时间戳 |
| 入账前证据 | `attest`：对来源日志做 HMAC 签名，日志被改动则状态为 `changed`，直接判审计不通过 |
| 例外管理 | 超预算 / 未定价 / 无授权凭证统一在 `report --exceptions` 暴露 |

详细的控制目标与穿行测试方法见 `docs/internal-control.md`。

## 来源证明（入账前的可信度）

账本里的哈希链只能证明**入账之后**没被改过。`tledger attest` 补上另一段：
证明**入账之前**源日志有没有被动过。

```bash
tledger attest keygen --out ~/.secrets/tokenledger.key   # 密钥放在账本之外
tledger attest sign ~/.codex/sessions --signer ops       # 给日志登记来源证明
tledger ingest codex                                     # 入账时逐文件核对
tledger report --group-by attestation                    # 按证明状态出账
```

每张用量凭证都会带上来源状态：

| 状态 | 含义 |
| --- | --- |
| `verified` | 有证明、文件哈希一致、HMAC 重算通过 |
| `changed` | 有证明但文件哈希变了 —— **日志在签名之后被改过** |
| `no_key` | 有证明、哈希一致，但本机没有密钥，无法重算 HMAC |
| `invalid` | 有证明、哈希一致，但 HMAC 对不上 —— 证明记录不自洽 |
| `unsigned` | 没有任何证明覆盖这个文件 |

审计口径：`changed` / `invalid` 会让 `tledger verify` 直接不通过（退出码 3）。
想更强硬就用 `ingest --require-attestation`：来源没通过证明的日志**拒绝入账**（退出码 6）。

真实效果可以自己复现（在日志的副本上操作，别动原日志）：

```bash
tledger attest sign ./logs && tledger ingest codex --path ./logs   # 全部 verified
echo '{"...一封伪造的用量事件..."}' >> ./logs/rollout.jsonl      # 签名之后动手
tledger ingest codex --path ./logs                                 # 新凭证标 changed
tledger verify                                                     # 退出码 3
```

实测中伪造的那笔用量同时触发了两个独立控制点：来源状态变成 `changed`，
而且"明细逐笔累加 vs 日志累计值"的账实核对也失衡 —— 两个控制点各抓一次。

**边界（必须说清）**：这仍然不是不可否认性证明。密钥在运维者手里，它能证明的是
"这份日志自某时刻签名之后没变过"，以及"是哪个密钥签的"。
要防"跑 agent 的那台机器自己造假"，需要 agent 侧持有独立私钥并自行签名 —— 属于 Roadmap。

## 许可

MIT