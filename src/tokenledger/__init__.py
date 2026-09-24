"""tokenledger —— 审计级 AI token 用量凭证账本。

把企业内控的四个动作补到 AI 支出流上：

* **凭证化**：每笔 token 消耗一张凭证，编号连续、只增不改。
* **授权**：预算以授权凭证形式单独成册，用量逐笔比对，超限标记异常但照常入账。
* **对账**：明细账（JSONL）为准，索引可重建；可与供应商账单逐项比对。
* **审计留痕**：凭证之间以哈希链相连，任何事后修改都能被 verify 检出。
"""

from .config import Config, ConfigError, find_repo_root
from .ledger import DuplicateVoucher, Ledger, LedgerError, LockedLedger, build_usage_payload
from .models import KIND_AUTHORIZATION, KIND_USAGE, Usage, verify_hash, date_str, iso_utc, utc_now
from .pricing import price_usage, resolve_rate

__version__ = "0.1.0"

__all__ = [
    "Config",
    "ConfigError",
    "DuplicateVoucher",
    "KIND_AUTHORIZATION",
    "KIND_USAGE",
    "Ledger",
    "LedgerError",
    "LockedLedger",
    "Usage",
    "__version__",
    "build_usage_payload",
    "date_str",
    "find_repo_root",
    "iso_utc",
    "price_usage",
    "resolve_rate",
    "utc_now",
    "verify_hash",
]