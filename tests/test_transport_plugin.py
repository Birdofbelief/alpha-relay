# -*- coding: utf-8 -*-
"""投递后端插件 —— 「换存储不用动这个包」这句话的**证据**。

## 这套测试在证明什么

`docs/ARCHITECTURE.md` 第六节说「换投递方式 = 实现 `Transport` 的三个方法，
策略、闸门、调度不用碰」。这句话有两个各自独立、且**都不报错**的失败方式：

  1. **装不进去。** 文档教人写一个后端，读者写完了，但框架只认三个内置名字 ——
     于是他正确写好的代码永远进不了链。它不报错，因为根本没人试过加载它。
  2. **装错了地方。** 后端确实被构造出来了，但链跑的是另一个后端。
     两端于是写到两处不同的存储，各自日志都显示成功。

所以这里不测文档，测**行为**：在 `tmp_path`（= 仓库外）写一个真的后端文件，
用**两条真实路径**把它装进链 —— `SC_TRANSPORT=<路径>` 和 `build_chain(transport=...)`
—— 然后跑完一轮，检查**它真的收到了字节**。

## 为什么「名字写错要报错」值得单独一条测试

改这次之前，`SC_TRANSPORT=postgres` 的行为是**静默**退回文件后端 + 一行 warning。
这条测试把它钉死：写错名字必须**响亮地失败**，因为静默退化的后果不是「少个功能」，
是上面第 2 条那种「以为在写 A，实际写 B」。同族的纪律见 `build_chain` 里
`executor=` 与 `with_executor=True` 撞车时直接 `raise`。
"""
from __future__ import annotations

from datetime import date

import pytest

from signalchain.calendar import weekday_calendar
from signalchain.config import Config
from signalchain.marketdata import FakeMarketData
from signalchain.models import CH_EXECUTIONS, CH_HEARTBEAT, CH_POSITIONS, CH_SIGNALS
from signalchain.chain import build_chain
from signalchain.transport import FileTransport, Transport, load_transport

from .conftest import CLEAN_CODES, FIXED_NOW


# ------------------------------------------------------------
# 仓库外那个后端文件
# ------------------------------------------------------------
#: 一个完整的、能用的后端：三个方法，一个字典。
#: 刻意做一次 JSON 往返 —— 真实的存储都是「把文档序列化成字节」，
#: 省掉这一步会让测试比真实情况宽松。
_OBJECTSTORE_SRC = '''\
"""仓库外的一个玩具后端：把一个进程内字典当对象存储用。"""
from __future__ import annotations

import json
from typing import Any

from signalchain.transport import Transport


class MyObjectStorage(Transport):
    """对象存储的最小替身。"""

    def __init__(self) -> None:
        self.blobs: dict[str, Any] = {}

    def read(self, channel: str) -> Any | None:
        raw = self.blobs.get(channel)
        return None if raw is None else json.loads(raw)

    def write(self, channel: str, doc: Any) -> bool:
        self.blobs[channel] = json.dumps(doc, ensure_ascii=False)
        return True

    def describe(self) -> str:
        return "objectstore(内存)"
'''


def _write(tmp_path, name: str, body: str):
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def _calendar():
    return weekday_calendar(date(2026, 9, 1), date(2026, 9, 30))


def _market():
    return FakeMarketData(list(CLEAN_CODES), drift=0.002)


# ------------------------------------------------------------
# 一、内置名照旧
# ------------------------------------------------------------
def test_builtin_names_still_load(tmp_path):
    cfg = Config(role="producer", transport="file", runtime_dir=tmp_path / "r")
    assert isinstance(load_transport(cfg), FileTransport)

    cfg = Config(role="producer", transport="null", runtime_dir=tmp_path / "r")
    assert type(load_transport(cfg)).__name__ == "NullTransport"


def test_builtin_names_are_case_insensitive(tmp_path):
    """大小写宽容只给**内置名**。`SC_TRANSPORT=FILE` 是手滑的常见形态，
    而它没有歧义 —— 路径那边就不能这么干（见下面环境变量那条）。"""
    cfg = Config(role="producer", transport="FILE", runtime_dir=tmp_path / "r")
    assert isinstance(load_transport(cfg), FileTransport)


def test_gist_without_credentials_still_degrades_instead_of_failing(tmp_path):
    """★ 降级是**刻意保留**的，别在改「不认识的取值要报错」时顺手把它一起删了。

    两者的成因不同：这里是「凭据没配好」（无人值守时能跑但降级，好过起不来），
    那边是「你把名字写错了」（必须报错，因为跑起来的东西不是你要的）。
    """
    cfg = Config(role="producer", transport="gist", runtime_dir=tmp_path / "r")
    assert isinstance(load_transport(cfg), FileTransport)


# ------------------------------------------------------------
# 二、★ 不认识的取值必须报错（这条是这次改动的防回归）
# ------------------------------------------------------------
def test_unknown_name_raises_and_does_not_silently_fall_back_to_file(tmp_path):
    cfg = Config(role="producer", transport="postgres", runtime_dir=tmp_path / "r")
    with pytest.raises(FileNotFoundError) as e:
        load_transport(cfg)
    msg = str(e.value)
    assert "postgres" in msg, "报错要说清是哪个取值不认得"
    assert "file" in msg, "报错要列出认识的那几个，光说『不认识』没法修"


def test_unknown_name_propagates_out_of_build_chain(tmp_path):
    """装配层也不许把它吞掉 —— 否则错误只活在日志里。"""
    cfg = Config(role="producer", transport="postgres", runtime_dir=tmp_path / "r")
    with pytest.raises(FileNotFoundError):
        build_chain(cfg, strategy="demo", market=_market(), calendar=_calendar(),
                    console=False, schedule=[])


# ------------------------------------------------------------
# 三、按路径装进来（仓库外）
# ------------------------------------------------------------
def test_transport_from_a_path_outside_the_repo(tmp_path):
    path = _write(tmp_path, "my_objectstore.py", _OBJECTSTORE_SRC)
    cfg = Config(role="producer", transport=str(path), runtime_dir=tmp_path / "r")

    store = load_transport(cfg)
    assert type(store).__name__ == "MyObjectStorage"

    # 真的能用：写进去、读回来
    assert store.write(CH_SIGNALS, [{"id": "x"}]) is True
    assert store.read(CH_SIGNALS) == [{"id": "x"}]
    assert store.read(CH_HEARTBEAT) is None


def test_a_path_without_the_py_suffix_also_works(tmp_path):
    path = _write(tmp_path, "my_objectstore.py", _OBJECTSTORE_SRC)
    cfg = Config(role="producer", transport=str(path.with_suffix("")),
                 runtime_dir=tmp_path / "r")
    assert type(load_transport(cfg)).__name__ == "MyObjectStorage"


def test_missing_file_raises_filenotfound(tmp_path):
    cfg = Config(role="producer", transport=str(tmp_path / "nope.py"),
                 runtime_dir=tmp_path / "r")
    with pytest.raises(FileNotFoundError):
        load_transport(cfg)


# ------------------------------------------------------------
# 四、显式指定：`build_chain(transport=...)` 与 `SC_TRANSPORT=<路径>`
# ------------------------------------------------------------
def test_explicit_transport_argument_wins_over_config(tmp_path):
    """`transport=` 优先于配置 —— 和 `strategy=` / `market=` 是同一条优先级。"""
    path = _write(tmp_path, "my_objectstore.py", _OBJECTSTORE_SRC)
    cfg = Config(role="producer", transport="null", runtime_dir=tmp_path / "r")
    mine = load_transport(Config(role="producer", transport=str(path),
                                 runtime_dir=tmp_path / "r"))

    chain = build_chain(cfg, strategy="demo", market=_market(), calendar=_calendar(),
                        transport=mine, with_executor=True, console=False, schedule=[])
    assert chain.raw_transport is mine, "配置里写的是 null，显式传入的那个该赢"
    assert chain.runner.transport.inner is mine, "而且真的被用上了，不是只挂了个字段"


def test_the_chain_reads_and_writes_through_a_custom_backend(tmp_path):
    """★ 端到端：SS_TRANSPORT 指到仓库外那个文件，跑完一轮，检查它收到了字节。

    这是「可拔插」在最外层的样子 —— 使用者改的是**配置文件的一行**，
    没碰这个包的任何一个 .py。
    """
    path = _write(tmp_path, "my_objectstore.py", _OBJECTSTORE_SRC)

    chain = build_chain(
        Config(role="producer", transport=str(path), runtime_dir=tmp_path / "r"),
        strategy="demo", market=_market(), calendar=_calendar(),
        with_executor=True, console=False, schedule=[],
    )
    store = chain.runner.transport.inner
    assert type(store).__name__ == "MyObjectStorage"
    assert "objectstore" in chain.describe(), "describe() 也该走到这个后端上"

    result, _ = chain.step(FIXED_NOW)
    assert result.ok, result.error

    # 四个 channel 全落在这个后端里 —— 一个字节都没往文件后端写
    assert store.blobs[CH_SIGNALS], "信号应当写进这个后端"
    assert store.blobs[CH_HEARTBEAT], "心跳应当写进这个后端"
    assert store.blobs[CH_POSITIONS], "同进程的执行端也走同一个后端"
    assert store.blobs[CH_EXECUTIONS]
    # 只看 channel 目录 —— `runtime/state.json` 是策略状态，本来就落在本地，
    # 不走 Transport（它不属于那四个 channel）。把它算进来会得到一条假红。
    leftovers = list((tmp_path / "r" / "channels").rglob("*.json"))
    assert not leftovers, \
        f"既然选了别的后端，就不该同时往文件后端写一份：{leftovers}"


# ------------------------------------------------------------
# 五、歧义：多个候选必须显式指名
# ------------------------------------------------------------
def test_multiple_candidates_require_an_explicit_marker(tmp_path):
    path = _write(tmp_path, "two.py", _OBJECTSTORE_SRC + '''

class AnotherStore(MyObjectStorage):
    """第二个候选。辅助类很常见，但两个都像主后端时框架不猜。"""

    def describe(self) -> str:
        return "another(内存)"
''')
    cfg = Config(role="producer", transport=str(path), runtime_dir=tmp_path / "r")
    with pytest.raises(ImportError) as e:
        load_transport(cfg)
    assert "TRANSPORT" in str(e.value), "错误信息要告诉人怎么修"

    path.write_text(
        path.read_text(encoding="utf-8") + "\nTRANSPORT = AnotherStore\n",
        encoding="utf-8")
    assert type(load_transport(cfg)).__name__ == "AnotherStore"


def test_an_imported_backend_does_not_count_as_a_candidate(tmp_path):
    """★ 仓库外的文件**必然**会 import 东西，常见写法就是
    `from signalchain.transport import FileTransport, Transport`（拿来当基类或包一层）。

    那个 import 进来的类**不算候选**。算的话，「本文件里只定义了一个后端」
    会变成「找到多个 Transport 子类」，于是照着文档写出来的文件一个也装不进去 ——
    而报错会让人去删自己写得好好的那个类，因为他看不出那两个名字里有一个不是他的。
    """
    path = _write(tmp_path, "wrapped.py", '''\
from signalchain.transport import FileTransport, Transport, TransportError


class Wrapped(Transport):
    """包一层：读写都转给内层那个后端（构造时给）。"""

    def __init__(self, inner: FileTransport | None = None) -> None:
        self._inner = inner

    def _need(self) -> FileTransport:
        if self._inner is None:
            raise TransportError("Wrapped 没接到内层后端")
        return self._inner

    def read(self, channel):
        return self._need().read(channel)

    def write(self, channel, doc):
        return self._need().write(channel, doc)

    def describe(self) -> str:
        return "wrapped(未接内层)"
''')
    cfg = Config(role="producer", transport=str(path), runtime_dir=tmp_path / "r")
    assert type(load_transport(cfg)).__name__ == "Wrapped"


def test_a_module_without_any_transport_subclass_says_so(tmp_path):
    path = _write(tmp_path, "empty.py", "VALUE = 1\n")
    cfg = Config(role="producer", transport=str(path), runtime_dir=tmp_path / "r")
    with pytest.raises(ImportError) as e:
        load_transport(cfg)
    assert "TRANSPORT" in str(e.value)


def test_a_marker_that_is_not_a_transport_is_rejected(tmp_path):
    path = _write(tmp_path, "wrong.py", _OBJECTSTORE_SRC + "\nTRANSPORT = 42\n")
    cfg = Config(role="producer", transport=str(path), runtime_dir=tmp_path / "r")
    with pytest.raises(TypeError):
        load_transport(cfg)


# ------------------------------------------------------------
# 六、要配置的后端：`TRANSPORT` 可以是零参工厂
# ------------------------------------------------------------
def test_a_zero_arg_factory_is_accepted(tmp_path):
    """构造要读凭据/端点的后端（先例：内置的 `GistTransport.from_env()`），
    没法无参构造，所以 `TRANSPORT` 允许指向一个零参工厂。

    ★ 这里放了**两个**候选类：只有一个候选的话，这条测试在「工厂分支被删掉」
    的变异下依然会绿（自动选中那唯一的类），等于什么也没验。
    """
    path = _write(tmp_path, "factory.py", _OBJECTSTORE_SRC + '''

class TunedStore(MyObjectStorage):
    """工厂要造的那个 —— 参数不同的另一种配置。"""

    def describe(self) -> str:
        return "objectstore(调过参)"


def make_store() -> TunedStore:
    return TunedStore()


TRANSPORT = make_store
''')
    cfg = Config(role="producer", transport=str(path), runtime_dir=tmp_path / "r")
    store = load_transport(cfg)
    assert type(store).__name__ == "TunedStore", \
        "TRANSPORT 指的是工厂，就得用工厂返回的那个"
    assert store.describe() == "objectstore(调过参)"


def test_a_class_that_needs_arguments_says_who_to_use_from_env(tmp_path):
    """★ 最容易被劝退的那一步：`TRANSPORT = 我的类`，而那个类构造要参数。

    实测（2026-09-22，外部读者照 README 的「写一个 Transport 子类」写，
    后端构造要 endpoint/key）拿到的是**裸的** Python 报错：

        TypeError: MyObjectStorage.__init__() missing 2 required positional
        arguments: 'endpoint' and 'key'

    它说的是 Python 语法，不是「你该改什么」；全文没有出现 `from_env` ——
    而读者此刻正站在文档里写着「几十行就能接上」的那个扩展点上。
    所以这条测试钉的是：报错必须**指出 ① 零参工厂**这条路。

    ★ 仍然抛 `TypeError`（不换成自定义异常）：调用方可能已经在抓 TypeError，
      换类型是悄悄改接口。这条测试连类型一起钉住。
    """
    path = _write(tmp_path, "needsargs.py", _OBJECTSTORE_SRC + '''


class NeedsArgs(MyObjectStorage):
    def __init__(self, endpoint: str, key: str):
        self.endpoint = endpoint
        self.key = key


TRANSPORT = NeedsArgs
''')
    cfg = Config(role="producer", transport=str(path), runtime_dir=tmp_path / "r")
    with pytest.raises(TypeError) as e:
        load_transport(cfg)
    msg = str(e.value)
    assert "NeedsArgs" in msg, "报错没说是哪个类"
    assert "from_env" in msg, (
        "报错里没有 `from_env` —— 读者拿到的是「Python 语法错了」，"
        "而不是「这里该写零参工厂」")
    assert "build_chain" in msg, "② 那条路（装配时直接传对象）也要说"
    # 原始的构造失败原因不许被吞掉：换掉它就成了「猜原因」。
    assert "required positional argument" in msg, (
        "原始 TypeError 的正文被丢了 —— 那是读者唯一能看到自己缺了哪几个参数的地方")


def test_a_factory_returning_junk_is_rejected(tmp_path):
    """工厂返回的不是后端时**当场**报错，而不是等到链跑到一半。"""
    path = _write(tmp_path, "junk.py", _OBJECTSTORE_SRC + '''

def make_store():
    return "这不是后端"


TRANSPORT = make_store
''')
    cfg = Config(role="producer", transport=str(path), runtime_dir=tmp_path / "r")
    with pytest.raises(TypeError) as e:
        load_transport(cfg)
    assert "Transport" in str(e.value)


# ------------------------------------------------------------
# 七、环境变量那条路（真正使用者走的那条）
# ------------------------------------------------------------
def test_sc_transport_env_var_accepts_a_path(tmp_path, monkeypatch):
    """`SC_TRANSPORT=<仓库外路径>` 经 `Config.from_env()` → `build_chain`。

    前面几条走的是 `Config(...)` 直接构造，这条走**环境变量**：
    使用者实际敲的就是这一行。
    """
    path = _write(tmp_path, "my_objectstore.py", _OBJECTSTORE_SRC)
    monkeypatch.setenv("SC_TRANSPORT", str(path))
    monkeypatch.setenv("SC_RUNTIME_DIR", str(tmp_path / "r"))
    monkeypatch.setenv("SC_ROLE", "producer")

    cfg = Config.from_env()
    # ★ 路径必须**原样**进 Config。大小写敏感的文件系统上，
    #   哪怕只是一次 .lower()，`../MyStore.py` 也会变成「文件不存在」——
    #   而报错会指向一个使用者从没写过的路径，最难查的那一类。
    assert cfg.transport == str(path), "路径要原样进 Config，不许做大小写归一化"

    chain = build_chain(cfg, strategy="demo", market=_market(), calendar=_calendar(),
                        console=False, schedule=[])
    assert type(chain.raw_transport).__name__ == "MyObjectStorage"
    assert isinstance(chain.raw_transport, Transport)
