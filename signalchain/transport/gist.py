# -*- coding: utf-8 -*-
"""GitHub Gist 后端 —— 把 Gist 当作一个「四个文件的免运维共享存储」。

## 为什么是 Gist（先说清楚它不好的地方）

它不是为这个用途设计的，缺陷有三条，用之前必须知道：

1. **没有原子性**。更新文件内容是 read-modify-write（细节见 `base.py` 顶部）。
   这正是本仓库强制单一写入者的根本原因。
2. **读写配额低**。匿名 60 次/小时，带 token 5000 次/小时。所以本后端**每次只读写
   需要的那一个文件**，绝不整份拉取；轮询频率也必须克制。
3. **secret gist 不是 private**。它是「不被检索到」，不是「访问不了」——
   **知道 ID 的人就能读**。所以 Gist ID 本身要当作凭据对待，不要贴进聊天记录、
   不要进截图、不要写进公开仓库。

那为什么还用它：**零运维**。不需要服务器、不需要数据库、不需要暴露任何入站端口，
在有网的地方就能跑。对个人量化场景，这三条缺点换来的东西是划算的。

## 凭据

**本模块不含任何凭据字面量，也不该有。** 从构造参数传，或走 `from_env()`。
"""
from __future__ import annotations

import json
import logging
import os
import random
import time
from typing import Any

from ..models import CHANNEL_FILENAMES
from .base import Transport, TransportError

log = logging.getLogger(__name__)

API_BASE = "https://api.github.com/gists/"

#: 值得重试的 HTTP 状态码。
#: 429=限流，5xx=服务端瞬时故障。**401/403/404 不在其中** —— 那是配置错了
#: （token 失效、gist 被删、ID 写错），重试只会把配额耗光并推迟你发现问题的时间。
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})

#: 默认重试次数（不含首次尝试，即默认共 3 次请求）。
DEFAULT_RETRIES = 2


class GistTransport(Transport):
    """把每个 channel 映射成同一个 gist 里的一个文件。

    参数：
        gist_id: gist 的 ID（当作凭据对待）
        token:   GitHub token
        timeout: 单次请求超时秒数。**必须有值** —— 一个卡住的请求比一个失败的请求
                 更难排查，而且会直接吃掉交易窗口的时间预算。
        retries: 额外重试次数
    """

    supports_cas = False

    def __init__(
        self,
        gist_id: str,
        token: str,
        timeout: float = 15.0,
        retries: int = DEFAULT_RETRIES,
        session: Any | None = None,
    ) -> None:
        if not gist_id or not token:
            raise ValueError("gist_id 与 token 均为必填；本模块不接受空凭据")
        self._gist_id = gist_id
        self._token = token
        self._timeout = timeout
        self._retries = retries
        # 允许注入 requests.Session（测试用；也便于复用连接池）
        self._session = session

    # ---------------- 凭据来源 ----------------

    @classmethod
    def from_env(cls, **kwargs: Any) -> "GistTransport":
        """从环境变量读凭据。**这是推荐的用法**，源码里不留字面量。

        变量名：`SC_GIST_ID` / `SC_GITHUB_TOKEN`
        """
        gist_id = os.environ.get("SC_GIST_ID", "").strip()
        token = os.environ.get("SC_GITHUB_TOKEN", "").strip()
        if not gist_id or not token:
            raise TransportError(
                "缺少 Gist 凭据：请设置环境变量 SC_GIST_ID 与 SC_GITHUB_TOKEN。"
                "（模板见仓库根目录 .env.example）"
            )
        return cls(gist_id, token, **kwargs)

    # ---------------- HTTP ----------------

    def _request(self, method: str, url: str, **kwargs: Any) -> Any:
        """带指数退避重试的请求。

        语义（刻意与既有线上实现逐条对齐）：

        - 成功或**不可重试**的状态码 → 立即返回，由调用方判断 status_code
        - 重试耗尽仍是可重试状态码 → 返回**最后一次**响应（让调用方走它自己的降级分支）
        - 重试耗尽仍抛异常 → 把最后一个异常抛出去

        重试时**原样重发同一请求与同一份 body**，循环内绝不重新 GET。
        这一点很重要：本后端的写是「整体覆盖」语义，重放同一份 body 天然幂等，
        所以重试不会造成重复信号。若写语义是「追加」，这条重试就必须重做。
        """
        import requests

        kwargs.setdefault("timeout", self._timeout)
        headers = dict(kwargs.pop("headers", {}) or {})
        headers.setdefault("Authorization", f"token {self._token}")
        kwargs["headers"] = headers

        sender = self._session.request if self._session is not None else requests.request

        resp = None
        last_exc: Exception | None = None

        for attempt in range(self._retries + 1):
            retry_after: str | None = None
            last_err = ""
            try:
                resp = sender(method, url, **kwargs)
                if resp.status_code not in RETRY_STATUS:
                    return resp
                last_err = f"HTTP {resp.status_code}"
                if resp.status_code == 429:
                    retry_after = resp.headers.get("Retry-After")
            except (requests.ConnectionError, requests.Timeout) as e:
                # 连接重置（含 RemoteDisconnected）/超时：连接级瞬时故障，值得重试
                last_exc = e
                last_err = type(e).__name__

            if attempt >= self._retries:
                break

            if retry_after:
                try:
                    wait = max(0.0, float(retry_after))
                except (TypeError, ValueError):
                    wait = 1.5 * (2 ** attempt) + random.uniform(0, 0.5)
            else:
                # 退避序列约 1.5~2.0s、3.0~3.5s；加随机抖动错峰，避免多进程同时重试
                wait = 1.5 * (2 ** attempt) + random.uniform(0, 0.5)
            log.warning("Gist 请求失败（%s），第 %d/%d 次重试，等待 %.1fs: %s %s",
                        last_err, attempt + 1, self._retries, wait, method, url[:70])
            time.sleep(wait)

        if resp is None and last_exc is not None:
            raise last_exc
        return resp

    # ---------------- Transport 接口 ----------------

    def _filename(self, channel: str) -> str:
        name = CHANNEL_FILENAMES.get(channel)
        if not name:
            raise TransportError(f"未知 channel: {channel!r}")
        return name

    def read(self, channel: str) -> Any | None:
        """读取单个文件。

        注意这里**没有**做整份 gist 的缓存或复用：每次读都是一次真实的 HTTP 请求。
        这是刻意的 —— 缓存会让「读到的」和「实际存的」出现时间差，而在交易链路上
        这种时间差表现为「用旧持仓做了新决策」，属于最贵的一类错误。
        """
        name = self._filename(channel)
        try:
            resp = self._request("GET", API_BASE + self._gist_id)
        except Exception as e:  # noqa: BLE001 - 网络层异常统一转成 TransportError
            raise TransportError(f"读取 gist 失败（{channel}）: {type(e).__name__}: {e}") from e

        if resp.status_code != 200:
            raise TransportError(f"读取 gist 失败（{channel}）: HTTP {resp.status_code}")

        try:
            files = resp.json().get("files", {})
        except (ValueError, AttributeError) as e:
            raise TransportError(f"gist 响应不是合法 JSON（{channel}）: {e}") from e

        if name not in files:
            # 文件不存在是**正常状态**（还没写过），返回 None。
            # 与「读失败」区分开 —— 后者已经在上面抛异常了。
            return None

        content = files[name].get("content", "")
        if not content.strip():
            return None
        try:
            return json.loads(content)
        except json.JSONDecodeError as e:
            raise TransportError(f"gist 文件 {name} 内容不是合法 JSON: {e}") from e

    def write(self, channel: str, doc: Any) -> bool:
        """覆盖写单个文件。

        PATCH body 里**只放这一个文件** —— gist API 只替换你显式给出的文件，
        其余文件原样保留。所以这里不需要 read-modify-write 整份 gist。
        （但文件**内容**的整体覆盖仍是 RMW 的，竞态讨论见 base.py。）
        """
        name = self._filename(channel)
        payload = {
            "files": {
                name: {"content": json.dumps(doc, ensure_ascii=False, indent=2)}
            }
        }
        try:
            resp = self._request("PATCH", API_BASE + self._gist_id, json=payload)
        except Exception as e:  # noqa: BLE001
            raise TransportError(f"写入 gist 失败（{channel}）: {type(e).__name__}: {e}") from e

        if resp.status_code != 200:
            log.warning("写入 gist 失败（%s）: HTTP %s", channel, resp.status_code)
            return False
        return True

    def describe(self) -> str:
        # **绝不能把 gist_id 或 token 打进日志**。给一个可区分身份、但不可用于访问的指纹。
        import hashlib

        fp = hashlib.sha256(self._gist_id.encode()).hexdigest()[:8]
        return f"gist:{fp}"
