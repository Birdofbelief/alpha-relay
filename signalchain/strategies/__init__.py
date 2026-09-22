# -*- coding: utf-8 -*-
"""内置策略。这里**只放演示策略**，用来证明「框架与策略无关」。

    demo_basic.py   纯 N 日收益率排序取前几名，无任何实盘成分

## 为什么它在**包内**，而不是仓库根的一个 `strategies/` 目录

`load_strategy("demo")` 是**默认值** —— `build_chain()` 不显式传策略就加载它，
所以这个文件是**默认值的一部分**，默认值必须在包里。

它原来放在仓库根的 `strategies/` 下，从仓库根跑一直正常，于是问题被藏住了：
`pip install .` 之后，任何一条走到默认策略的路径都会报
`ModuleNotFoundError: No module named 'strategies'` —— 一个只在
「别人装了包、我没装」时才出现的报错。（CI 里也因此补了 `pip install .`。）

## 你自己的策略**不应该放在这里**

放这儿等于放进了包里 —— 那是最彻底的公开。用路径从**仓库外**加载：

    chain = build_file_chain("./runtime", strategy="./my_private.py")
    build_chain(Config(strategy_path="./my_private.py"))   # 或设 SC_STRATEGY_PATH

★ 注意这两个入口读环境变量的方式不同：`SC_STRATEGY_PATH` 由
`Config.from_env()` 读取，所以 **`build_chain()` 认它，`build_file_chain()` 不认**
（后者刻意只认显式传进来的 `strategy=`）。指错了路径不会静默退回内置策略 ——
会报 `FileNotFoundError`，见 `signalchain/strategy.py` 的 `load_strategy`。

`.gitignore` 另有 `strategies/private*` 兜一层，但那只是保险，不是许可：
把私有策略放在仓库目录里再靠 gitignore 挡住，迟早会有人 `git add -f`。
"""
