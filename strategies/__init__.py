# -*- coding: utf-8 -*-
"""策略目录。

这里**只放演示策略**，用来证明「框架与策略无关」。

    demo_rotation.py   纯 N 日收益排名轮动，约 60 行，无任何实盘成分

你自己的策略**不应该放在这里**。用 `--strategy /path/to/your.py` 或
`SC_STRATEGY_PATH` 从仓库外加载 —— 那样它永远不会进版本库。
`.gitignore` 另有 `strategies/private*` 兜一层，但那只是保险，不是许可：
把私有策略放在仓库目录里再靠 gitignore 挡住，迟早会有人 `git add -f`。
"""
