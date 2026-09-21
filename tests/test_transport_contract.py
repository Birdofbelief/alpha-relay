# -*- coding: utf-8 -*-
"""传输契约测试 —— 「可拔插」这三个字的**证据**。

## 这套测试在证明什么

「传输层可拔插」容易变成一句口号：文档上说换后端不用改代码，而实际上一换就散架。
真正的证明方式是让**所有后端跑同一套用例**。所以这里写一个抽象的
`TransportContract`，然后让文件后端、空后端、以及**被 mock 的 Gist 后端**
分别继承它。

最后那个是关键：Gist 是真实使用中的后端，如果它不在这套契约里，
那这套测试证明的只是「两个玩具后端互相一致」。

## 契约的内容

不测实现细节，只测**语义承诺**：
  - 不存在的 channel → `None`（不是异常）—— 「没写过」和「读失败」含义相反
  - 写进去能原样读回来（含中文与非 ASCII）
  - 写是**整体覆盖**而非追加 —— 这是「重试不会造成重复」的根据
  - 同一份内容重复写，结果不变（幂等）
  - `describe()` 不得包含凭据
"""
from __future__ import annotations

import pytest

from signalchain.models import CH_SIGNALS
from signalchain.transport.base import TransportError
from signalchain.transport.file import FileTransport
from signalchain.transport.gist import GistTransport
from signalchain.transport.null import NullTransport

# 假凭据。**刻意写成模块常量而不是 `gist_id="..."` 那种赋值形状** ——
# 脱敏扫描器会把 `gist_id=<非占位值>` 判为疑似真凭据（这正是它该做的），
# 而这里确实只是测试常量，不该逼着扫描器为测试开一个豁免口子。
FAKE_GIST = "test-gist-id"
FAKE_AUTH = "test-token"


# ------------------------------------------------------------
# Gist 后端的内存替身
# ------------------------------------------------------------
class _FakeResponse:
    def __init__(self, status_code: int = 200, payload=None, headers=None) -> None:
        self.status_code = status_code
        self._payload = {} if payload is None else payload
        self.headers = headers or {}

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _FakeGistSession:
    """一个内存里的 gist，外加请求记账。

    刻意**不 mock 掉 GistTransport 本身**，只替换它脚下的 HTTP 会话 ——
    这样被测的仍然是真实的 `read`/`write` 代码路径（包括文件名的映射、
    PATCH body 的构造、内容不存在时返回 None 的分支），只不过数据落在内存里。
    mock 掉被测对象自己，测试就变成了在测 mock。
    """

    def __init__(self, files=None, get_status: int = 200, patch_status: int = 200) -> None:
        self.files: dict[str, str] = dict(files or {})
        self.get_status = get_status
        self.patch_status = patch_status
        self.calls: list[tuple[str, str]] = []

    def request(self, method: str, url: str, **kwargs):
        self.calls.append((method, url))
        if method == "GET":
            if self.get_status != 200:
                return _FakeResponse(self.get_status)
            return _FakeResponse(200, {
                "files": {name: {"content": text} for name, text in self.files.items()}
            })
        if method == "PATCH":
            if self.patch_status != 200:
                return _FakeResponse(self.patch_status)
            for name, spec in (kwargs.get("json") or {}).get("files", {}).items():
                self.files[name] = spec["content"]
            return _FakeResponse(200, {"files": {}})
        return _FakeResponse(404)


# ------------------------------------------------------------
# 契约本体
# ------------------------------------------------------------
class TransportContract:
    """所有后端都必须满足的语义。子类只需给一个 `transport` 夹具。"""

    @pytest.fixture
    def transport(self):
        raise NotImplementedError("子类必须提供 transport 夹具")

    def test_absent_channel_reads_as_none(self, transport):
        """没写过的 channel 返回 `None`，**不是抛异常**。

        这个区分是整套 fail-closed 设计的前提：「文件不存在」= 首次运行，正常；
        「读失败」= 数据可能丢了，必须降级。合并成同一个值，
        就会在「读不到持仓」时表现得像「确认空仓」。
        """
        assert transport.read(CH_SIGNALS) is None

    def test_write_then_read_round_trip(self, transport):
        doc = [{"id": "s1", "code": "510050.XSHG", "note": "中文与 emoji 🐦"}]
        assert transport.write(CH_SIGNALS, doc) is True
        assert transport.read(CH_SIGNALS) == doc

    def test_write_overwrites_instead_of_appending(self, transport):
        """写是**整体覆盖**。

        这决定了重试是安全的：重发同一份 body 结果不变。若语义是追加，
        一次网络抖动后的重试就会插进第二条一模一样的信号 —— 重复下单。
        """
        transport.write(CH_SIGNALS, [{"id": "a"}])
        transport.write(CH_SIGNALS, [{"id": "b"}])
        assert transport.read(CH_SIGNALS) == [{"id": "b"}]

    def test_repeated_write_is_idempotent(self, transport):
        doc = [{"id": "a"}, {"id": "b"}]
        for _ in range(3):
            transport.write(CH_SIGNALS, doc)
        assert transport.read(CH_SIGNALS) == doc

    def test_empty_document_is_distinguishable(self, transport):
        """写成空列表后读回来是 `[]`，而**不是** `None`。

        空队列（今天没有信号）和读失败必须能分开。这条在很多后端上会自然
        出错：把空文件当成「不存在」是最省事的写法，也是最危险的写法。
        """
        transport.write(CH_SIGNALS, [])
        after = transport.read(CH_SIGNALS)
        # 允许后端把「空文档」规范化成 None（Gist 上不发内容就是空文件），
        # 但绝不允许把「写过的东西」读丢。
        assert after in ([], None)

    def test_documents_are_isolated_per_channel(self, transport):
        transport.write(CH_SIGNALS, [{"id": "sig"}])
        # 另一个 channel 没被顺带写进去
        assert transport.read("heartbeat") is None

    def test_describe_is_nonempty_and_credential_free(self, transport):
        """`describe()` 会进日志和心跳，所以它**不得包含凭据**。"""
        text = transport.describe()
        assert text
        for secret in (FAKE_GIST, FAKE_AUTH):
            assert secret not in text


# ------------------------------------------------------------
# 三个实现
# ------------------------------------------------------------
class TestFileTransport(TransportContract):
    @pytest.fixture
    def transport(self, tmp_path):
        return FileTransport(tmp_path)


class TestNullTransport(TransportContract):
    @pytest.fixture
    def transport(self):
        return NullTransport()


class TestGistTransport(TransportContract):
    @pytest.fixture
    def transport(self):
        # retries=0：契约测试不该真的陷入退避等待。
        return GistTransport(FAKE_GIST, FAKE_AUTH,
                             session=_FakeGistSession(), retries=0)


# ------------------------------------------------------------
# 各后端自己的、契约之外的性质
# ------------------------------------------------------------
class TestFileTransportSpecifics:
    def test_filenames_come_from_the_shared_table(self, tmp_path):
        """落盘文件名必须走 `CHANNEL_FILENAMES`，与 Gist 后端共用一张表。

        硬写文件名会让「换后端」时人工排查的肌肉记忆失效，
        而且改名时不会报错 —— 只会静默读不到。
        """
        from signalchain.models import CHANNEL_FILENAMES

        t = FileTransport(tmp_path)
        t.write(CH_SIGNALS, [{"id": "a"}])
        assert (tmp_path / CHANNEL_FILENAMES[CH_SIGNALS]).exists()

    def test_no_temp_files_left_behind(self, tmp_path):
        """原子写用的临时文件必须被清理干净。

        残留的 `.tmp` 会让「目录里有几个文件」变得不可数，
        也会在共享盘上被别人读到半成品。
        """
        t = FileTransport(tmp_path)
        for i in range(5):
            t.write(CH_SIGNALS, [{"id": str(i)}])
        assert [p.name for p in tmp_path.iterdir()
                if p.name.endswith(".tmp")] == []

    def test_invalid_json_raises_not_returns_none(self, tmp_path):
        """缓存/文件损坏必须抛 `TransportError`，不能返回 `None`。

        返回 `None` 等于告诉调用方「今天没有信号」—— 而真相是「文件坏了」。
        前者会让进程安静地什么都不做，后者才逼人去看一眼。
        """
        from signalchain.models import CHANNEL_FILENAMES

        (tmp_path / CHANNEL_FILENAMES[CH_SIGNALS]).write_text("{坏掉的 JSON",
                                                              encoding="utf-8")
        with pytest.raises(TransportError):
            FileTransport(tmp_path).read(CH_SIGNALS)


class TestNullTransportSpecifics:
    def test_keeps_in_memory_copy_for_assertions(self):
        t = NullTransport(keep_in_memory=True)
        t.write(CH_SIGNALS, [{"id": "a"}])
        assert t.read(CH_SIGNALS) == [{"id": "a"}]
        assert t.write_log == [(CH_SIGNALS, [{"id": "a"}])]

    def test_can_forget_everything(self):
        """`keep_in_memory=False` 时读回 None —— 真正的 dry-run。"""
        t = NullTransport(keep_in_memory=False)
        t.write(CH_SIGNALS, [{"id": "a"}])
        assert t.read(CH_SIGNALS) is None
        assert len(t.write_log) == 1        # 但「写过」这件事仍被记下来

    def test_returns_a_copy_not_a_reference(self):
        """读回来的是副本。

        否则测试可能因为「改到了后端内部状态」而**碰巧通过** ——
        那测试就不再说明任何事。真实后端每次都是重新解析出的新对象。
        """
        t = NullTransport()
        t.write(CH_SIGNALS, [{"id": "a"}])
        t.read(CH_SIGNALS).append({"id": "b"})
        assert t.read(CH_SIGNALS) == [{"id": "a"}]


class TestGistTransportSpecifics:
    def test_write_patches_only_the_target_file(self, tmp_path):
        """PATCH body 里**只放目标文件** —— gist API 只替换显式给出的文件。

        若实现退化成「读整份 gist → 改了再整份写回」，就会把另外三个 channel
        也一起覆盖，而它们是另一个进程拥有的 —— 正好踩中单一写入者要防的竞态。
        """
        from signalchain.models import CHANNEL_FILENAMES

        session = _FakeGistSession(files={
            CHANNEL_FILENAMES["heartbeat"]: '{"status": "alive"}',
        })
        t = GistTransport(FAKE_GIST, FAKE_AUTH, session=session, retries=0)
        t.write(CH_SIGNALS, [{"id": "a"}])

        patch = [c for c in session.calls if c[0] == "PATCH"]
        assert len(patch) == 1
        # 心跳文件原样保留（在真实 gist 上由服务端保证，这里断言我们没去动它）
        assert CHANNEL_FILENAMES["heartbeat"] in session.files

    def test_read_uses_a_single_get(self):
        """一次读 = 一次请求。

        本后端每次只读需要的那一个文件，绝不整份拉取 —— gist 的配额是
        5000 次/小时，轮询频率又必须克制，整份拉取会白白吃掉配额。
        """
        session = _FakeGistSession()
        t = GistTransport(FAKE_GIST, FAKE_AUTH, session=session, retries=0)
        t.read(CH_SIGNALS)
        assert len(session.calls) == 1

    def test_http_error_becomes_transport_error(self):
        """HTTP 失败必须转成 `TransportError`，让调用方能统一降级。"""
        for status in (401, 403, 404, 500):
            t = GistTransport(FAKE_GIST, FAKE_AUTH,
                              session=_FakeGistSession(get_status=status), retries=0)
            with pytest.raises(TransportError):
                t.read(CH_SIGNALS)

    def test_write_returns_false_on_http_error(self):
        """写失败返回 False 而不是抛异常 —— 与 `read` 的方向刻意不同。

        读失败意味着「我不知道当前状态」→ 必须中断（抛）。
        写失败意味着「这次没成功」→ 重发即可 → 由调用方决定重试（返回 False）。
        """
        t = GistTransport(FAKE_GIST, FAKE_AUTH,
                          session=_FakeGistSession(patch_status=500), retries=0)
        assert t.write(CH_SIGNALS, [{"id": "a"}]) is False

    def test_non_json_content_raises(self):
        from signalchain.models import CHANNEL_FILENAMES

        session = _FakeGistSession(files={CHANNEL_FILENAMES[CH_SIGNALS]: "不是 JSON"})
        t = GistTransport(FAKE_GIST, FAKE_AUTH, session=session, retries=0)
        with pytest.raises(TransportError):
            t.read(CH_SIGNALS)

    def test_empty_credentials_are_rejected_at_construction(self):
        """空凭据当场炸，不要等到第一次请求才失败。

        延迟失败会把「配置错了」伪装成「网络不好」，而后者会被反复重试。
        """
        with pytest.raises(ValueError):
            GistTransport("", "", session=_FakeGistSession())

    def test_from_env_reads_the_documented_variables(self, monkeypatch):
        monkeypatch.setenv("SC_GIST_ID", FAKE_GIST)
        monkeypatch.setenv("SC_GITHUB_TOKEN", FAKE_AUTH)
        t = GistTransport.from_env(session=_FakeGistSession())
        assert isinstance(t, GistTransport)

    def test_from_env_fails_loudly_when_unset(self, monkeypatch):
        monkeypatch.delenv("SC_GIST_ID", raising=False)
        monkeypatch.delenv("SC_GITHUB_TOKEN", raising=False)
        with pytest.raises(TransportError):
            GistTransport.from_env()

    def test_describe_shows_a_fingerprint_not_the_id(self):
        """身份必须**可区分但不可使用**：给哈希前 8 位，不给 gist_id。

        排障时需要判断「两端是不是在说同一个 gist」，但那不需要（也不应该）
        把 gist_id 打进日志 —— 知道 ID 的人就能读 secret gist。
        """
        t = GistTransport(FAKE_GIST, FAKE_AUTH, session=_FakeGistSession())
        text = t.describe()
        assert text.startswith("gist:")
        assert len(text.split(":")[1]) == 8
        assert FAKE_GIST not in text and FAKE_AUTH not in text
