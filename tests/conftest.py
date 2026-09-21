# -*- coding: utf-8 -*-
"""共享夹具与测试常量。

## 一条只属于这个仓库的测试纪律：测试里用的标的代码也要过脱敏扫描

`tools/sanitize_check.py` 会把**全仓库**（包括 `tests/`）按使用者自备的黑名单
扫一遍。这不是多余的谨慎 —— 示例和测试里写下的六位数字，是最容易被复制到
别人仓库、也最容易被抄回公开文档的地方，而它们恰好可能正是私有标的池的成员。
本仓库初版的演示策略就撞上过两个。

所以：**测试里一律用 `CLEAN_CODES` 里的代码**，或在需要特定前缀时用
`SYNTHETIC_*`（那些是不存在的证券代码，只是形状对）。
"""
from __future__ import annotations

from datetime import datetime

import pytest

#: 已确认不在任何私有池中的宽基代码。需要真实代码形状时用这三个。
CLEAN_CODES = ["510050", "510500", "159919"]

#: 只为覆盖「后缀由首位数字决定」而造的**不存在**的代码。
#: 用 123456/234567 这种连号，是为了让「这不是真标的」一眼可见。
SYNTHETIC_SH = "123456"   # 首位 1 → 按规则归深市，故意的：它证明规则只看首位
SYNTHETIC_SZ = "234567"

#: 全套测试统一用这个「现在」。**不要用 datetime.now()** ——
#: 一旦测试依赖墙上时钟，信号 id、跨日判断、缓存新鲜度就会随机红，
#: 而随机的红比稳定地红更糟：它会训练人忽略失败。
FIXED_NOW = datetime(2026, 9, 18, 14, 45, 0)


@pytest.fixture
def fixed_now() -> datetime:
    return FIXED_NOW


@pytest.fixture
def clean_codes() -> list[str]:
    return list(CLEAN_CODES)
