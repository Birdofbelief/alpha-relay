# -*- coding: utf-8 -*-
"""测试包。

有 `__init__.py` 是刻意的：这样 pytest 会用「包内相对导入」，
测试之间可以共用 `tests.conftest` 里的常量（标代码白名单、固定时钟），
而不必靠 `sys.path` 的偶然顺序。
"""
