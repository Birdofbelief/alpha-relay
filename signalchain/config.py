# -*- coding: utf-8 -*-
"""配置：一切可调项的唯一入口，且**源码里没有凭据**。

## 为什么凭据只从环境变量来

一个凭据只要在源码里出现过一次，它就已经泄露了 —— 因为它同时存在于：
本地工作区、每一份 `.bak`、每一个历史版本、以及**所有克隆过这个仓库的人**的磁盘上。
「我先写死，回头再改」在实践中等于「写死了，而且没人再改」。

更要紧的是**改不动**：硬编码的凭据轮换一次，就要翻遍所有副本核对一遍。
环境变量把这个动作变成「改一处、重启一次」。

所以本模块的原则是：
  - 凭据**只能**来自环境变量或 `.env`（`.env` 在 `.gitignore` 里）
  - 日志、报错、`describe()` **任何地方都不打印凭据本身**（见 `fingerprint`）
  - 缺凭据时**降级到不需要凭据的后端**，而不是带着空字符串发请求

## `.env` 不是标准库

本模块自带一个 30 行的 `.env` 解析器，不引入 `python-dotenv`。
理由：这个仓库的卖点之一是「依赖极少」，而 `.env` 的格式简单到不值得为它加一个包。
解析器只支持 `KEY=VALUE`、`#` 注释、以及可选的引号 —— 够用，且行为完全可预期。
"""
from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

ENV_PREFIX = "SC_"

#: 所有能被环境变量覆盖的键（同时也是 `.env.example` 的内容清单）
ENV_KEYS = (
    "SC_ROLE",              # producer | executor
    "SC_TRANSPORT",         # file | gist | null
    "SC_RUNTIME_DIR",       # 运行态目录（缓存、状态、日志）
    "SC_STRATEGY_PATH",     # 从**仓库外**加载私有策略的路径
    "SC_GIST_ID",           # 仅 gist 后端需要
    "SC_GITHUB_TOKEN",      # 仅 gist 后端需要
    "SC_SERVERCHAN_KEY",    # 仅 ServerChan 通知需要
    "SC_HTTP_TIMEOUT",
    "SC_SNAPSHOT_TTL",
    "SC_MAX_STALE_DAYS",
    "SC_CATCHUP_MINUTES",
    "SC_TASK_FAIL_LIMIT",
)


def fingerprint(secret: str | None) -> str:
    """给凭据算一个**不可逆的短指纹**，用于日志中区分「是哪一把」。

    排障时经常只需要知道「换没换」「两边是不是同一把」，而不需要知道值本身。
    指纹正好回答这类问题，且泄露它没有意义（无法反推）。

    ★ 本仓库任何地方都**不打印凭据原文** —— 包括异常信息与 debug 日志。
    明文一旦进了日志文件，它就从「一个受控的配置项」变成了
    「一份躺得到处都是的副本」，性质完全不同。
    """
    if not secret:
        return "(未设置)"
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:8]


def load_dotenv(path: str | Path) -> dict[str, str]:
    """极简 `.env` 解析器。

    刻意**不覆盖**已存在的环境变量：真实环境 > 文件。这样在 CI 或临时调试时
    用 `SC_TRANSPORT=null python ...` 就能压过 `.env`，不需要改文件。
    """
    path = Path(path)
    if not path.exists():
        return {}
    loaded: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value
        loaded[key] = value
    return loaded


@dataclass
class Config:
    """运行配置。所有取值都有**能跑起来**的默认值。

    默认值的取向是「不配置就能离线跑通」：默认后端是文件、默认通知是空实现。
    这样克隆下来直接 `python -m signalchain.demo` 就能看到完整闭环，
    而不必先注册一堆账号 —— 那是最容易让人放弃的一步。
    """

    role: str = "producer"
    transport: str = "file"
    runtime_dir: Path = field(default_factory=lambda: Path("./runtime"))
    strategy_path: str | None = None

    gist_id: str = ""
    github_token: str = ""
    serverchan_key: str = ""

    http_timeout: float = 15.0
    snapshot_ttl: float = 300.0
    max_stale_days: int = 1
    catchup_minutes: int = 30
    task_fail_limit: int = 3

    # ---------------- 构造 ----------------

    @classmethod
    def from_env(cls, dotenv: str | Path | None = ".env") -> "Config":
        if dotenv:
            load_dotenv(dotenv)
        return cls(
            role=os.environ.get("SC_ROLE", "producer").strip().lower(),
            # ★ 只 strip，**不 lower** —— 这个值除了内置名还可以是一个文件路径，
            # 而大小写敏感的文件系统上，一个 .lower() 就足以把 `../MyStore.py`
            # 变成「文件不存在」。内置名的大小写宽容交给 load_transport 去做。
            transport=os.environ.get("SC_TRANSPORT", "file").strip(),
            runtime_dir=Path(os.environ.get("SC_RUNTIME_DIR", "./runtime")),
            strategy_path=os.environ.get("SC_STRATEGY_PATH") or None,
            gist_id=os.environ.get("SC_GIST_ID", "").strip(),
            github_token=os.environ.get("SC_GITHUB_TOKEN", "").strip(),
            serverchan_key=os.environ.get("SC_SERVERCHAN_KEY", "").strip(),
            http_timeout=_num("SC_HTTP_TIMEOUT", 15.0),
            snapshot_ttl=_num("SC_SNAPSHOT_TTL", 300.0),
            max_stale_days=int(_num("SC_MAX_STALE_DAYS", 1)),
            catchup_minutes=int(_num("SC_CATCHUP_MINUTES", 30)),
            task_fail_limit=int(_num("SC_TASK_FAIL_LIMIT", 3)),
        )

    # ---------------- 派生路径 ----------------

    @property
    def cache_dir(self) -> Path:
        return self.runtime_dir / "hist_cache"

    @property
    def state_path(self) -> Path:
        return self.runtime_dir / "state.json"

    @property
    def channel_dir(self) -> Path:
        return self.runtime_dir / "channels"

    def ensure_dirs(self) -> "Config":
        for d in (self.runtime_dir, self.cache_dir, self.channel_dir):
            d.mkdir(parents=True, exist_ok=True)
        return self

    # ---------------- 安全的自述 ----------------

    def describe(self) -> str:
        """供日志/心跳显示。**只出指纹，不出凭据。**

        这个方法被刻意写得比一般的 `__repr__` 啰嗦：因为它是唯一一个
        「方便」的打印配置的入口，一旦它顺手把 token 带出去，
        所有人都会在日志里看到它。堵住这一个口子，比指望每个调用点都记得脱敏可靠。
        """
        return (
            f"Config(role={self.role}, transport={self.transport}, "
            f"runtime={self.runtime_dir}, "
            f"gist_id={fingerprint(self.gist_id)}, "
            f"token={fingerprint(self.github_token)}, "
            f"serverchan={fingerprint(self.serverchan_key)}, "
            f"strategy={self.strategy_path or '(内置示例)'})"
        )

    def validate(self) -> list[str]:
        """返回**问题列表**（空 = 通过）。不抛异常，由调用方决定怎么处理。

        返回列表而不是抛异常，是因为「配置有问题」通常不致命 ——
        比如规定了 gist 后端却没给 token，正确的反应是**降级到文件后端并告警**，
        而不是让整个进程起不来。在无人值守的场景里，能跑但降级，
        永远好过起不来。
        """
        problems: list[str] = []
        if self.role not in ("producer", "executor"):
            problems.append(f"SC_ROLE 必须是 producer 或 executor，当前 {self.role!r}")
        # SC_TRANSPORT 的取值**不在这里判**：它除了内置名还可以是一个仓库外
        # 文件路径，只有 transport 那一层知道完整规则（`load_transport`），
        # 而它判错的方式是**直接抛异常**，比这里出一行 warning 更响亮。
        # 在 config 里再抄一份名字清单，等于制造第二个真相源。
        if self.transport == "gist":
            if not self.gist_id:
                problems.append("gist 后端需要 SC_GIST_ID")
            if not self.github_token:
                problems.append("gist 后端需要 SC_GITHUB_TOKEN")
        if self.strategy_path and not Path(self.strategy_path).exists():
            problems.append(f"SC_STRATEGY_PATH 指向的文件不存在: {self.strategy_path}")
        return problems


def _num(key: str, default: float) -> float:
    raw = os.environ.get(key)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        log.warning("环境变量 %s=%r 不是数字，改用默认值 %s", key, raw, default)
        return default
