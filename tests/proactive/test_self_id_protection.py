"""自 ID 保护：五条路径语义必须一致。

复核发现的真实缺陷：用户一旦把 bot 自己的 ID 写进忽略名单，
``_has_substantive`` 会把「窗口只剩 bot 自己发言」判成无实质内容，
导致该群主动发起被永久静默且无日志。
"""

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from astrbot_plugin_iris_memory.iris_memory.proactive.config import ConfigManager
from astrbot_plugin_iris_memory.iris_memory.proactive.perception import (
    ContextPackager,
    SlidingWindow,
    WindowMessage,
)
from astrbot_plugin_iris_memory.iris_memory.proactive.proactive import ProactiveEngine
from astrbot_plugin_iris_memory.iris_memory.proactive.sender_gate import (
    SenderIgnoreList,
)
from astrbot_plugin_iris_memory.iris_memory.proactive.state import StateManager
from astrbot_plugin_iris_memory.main import IrisMemoryPlugin

BOT = "999999"
IGNORED = "100000005"
NORMAL = "1000000001"

NEVER_MUTE = {
    "mute_period": {"start_hour": 0, "start_minute": 0, "end_hour": 0, "end_minute": 0}
}


class _GateCfg:
    def __init__(self, ids: str) -> None:
        self.ignore_sender_ids = ids
        self.ignore_sync_external = False
        self.ignore_sync_user_only = False


def _plugin(self_id: str = BOT, ids: str = f"{IGNORED},{BOT}"):
    p = object.__new__(IrisMemoryPlugin)
    p._self_id = self_id
    p._sender_gate = SenderIgnoreList(_GateCfg(ids), config_dir=None)
    p._sliding_window = Mock()
    p._state = Mock()
    p._state.get_whitelist.return_value = {"g1"}
    return p


class TestSnapshotExcludesSelf:
    def test_gate_snapshot_contains_self(self):
        p = _plugin()
        assert BOT in p._sender_gate.snapshot()

    def test_plugin_snapshot_excludes_self(self):
        p = _plugin()
        snap = p._ignore_senders_snapshot()
        assert BOT not in snap
        assert IGNORED in snap

    def test_empty_self_id_keeps_all(self):
        p = _plugin(self_id="")
        assert BOT in p._ignore_senders_snapshot()

    def test_no_gate_returns_empty(self):
        p = object.__new__(IrisMemoryPlugin)
        assert p._ignore_senders_snapshot() == set()


class TestBotOwnMessagesSurvive:
    def test_bot_only_window_is_substantive(self):
        """bot 自己发过言的群必须还能被评估，否则主动发起永久饿死。"""
        p = _plugin()
        eng = object.__new__(ProactiveEngine)
        eng._ignore_senders_get = p._ignore_senders_snapshot

        bot_only = [WindowMessage(BOT, "我", "我发起的话题", 0.0)]
        assert eng._has_substantive(bot_only) is True

    def test_package_does_not_redact_bot(self):
        p = _plugin()
        packager = ContextPackager(
            ConfigManager({}, hidden_get={}.get), self_id_get=lambda: BOT
        )
        msgs = [
            WindowMessage(BOT, "我", "我说的话", 0.0),
            WindowMessage(IGNORED, "小鲸鱼", "被忽略内容", 1.0),
        ]
        out = packager.package(
            "g1", msgs, "initiate", ignore_senders=p._ignore_senders_snapshot()
        )
        assert "我说的话" in out
        assert "被忽略内容" not in out


class TestWatchFilterKeepsSelf:
    def test_self_not_dropped_from_watch(self):
        p = _plugin()
        assert p._filter_watch_users([IGNORED, BOT, NORMAL]) == [BOT, NORMAL]


class TestUnknownSelfIdSkipsCleanup:
    def test_no_purge_when_self_id_unknown(self):
        p = _plugin(self_id="")
        p._on_ignore_list_changed({IGNORED})
        p._sliding_window.purge_senders.assert_not_called()
        p._state.remove_anchor_watch.assert_not_called()

    def test_cleanup_runs_when_self_known(self):
        p = _plugin()
        p._on_ignore_list_changed({IGNORED})
        p._sliding_window.purge_senders.assert_called_once()

    def test_only_self_in_list_is_noop(self):
        p = _plugin()
        p._on_ignore_list_changed({BOT})
        p._sliding_window.purge_senders.assert_not_called()


class TestAnchorKeepsSelf:
    def test_self_not_removed_from_anchor(self):
        cfg = ConfigManager(NEVER_MUTE, hidden_get={}.get)
        st = StateManager(cfg)
        st.add_to_whitelist("g1")
        st.write_anchor(
            "g1", kind="passive", users=[BOT, NORMAL], keywords=["kw"]
        )

        p = object.__new__(IrisMemoryPlugin)
        p._self_id = BOT
        p._sender_gate = SenderIgnoreList(_GateCfg(f"{IGNORED},{BOT}"), config_dir=None)
        p._sliding_window = SlidingWindow(cfg)
        p._state = st

        p._on_ignore_list_changed({IGNORED, BOT})

        anchor = st.get_anchor("g1")
        assert BOT in anchor.participants
        assert NORMAL in anchor.participants


class TestScanDueNoChurn:
    @pytest.mark.asyncio
    async def test_empty_window_not_rescheduled(self):
        """空窗口群不应每轮重排期（白耗写盘）。"""
        eng = object.__new__(ProactiveEngine)
        eng._ignore_senders_get = lambda: {IGNORED}
        eng._state = Mock()
        eng._state.get_whitelist.return_value = {"g1"}
        eng._state.get_next_initiate_check.return_value = 0.0
        eng._window = Mock()
        eng._window.get_messages.return_value = []
        eng._check_pending_timeout = AsyncMock()
        eng._schedule_group = Mock()

        await eng._scan_due()

        eng._schedule_group.assert_not_called()

    @pytest.mark.asyncio
    async def test_future_schedule_not_churned(self):
        """候选时间还在未来时，被忽略群不该每轮重排期并置 dirty。"""
        eng = object.__new__(ProactiveEngine)
        eng._ignore_senders_get = lambda: {IGNORED}
        eng._state = Mock()
        eng._state.get_whitelist.return_value = {"g1"}
        eng._state.get_next_initiate_check.return_value = time.time() + 3600
        eng._window = Mock()
        eng._window.get_messages.return_value = [
            WindowMessage(IGNORED, "u", "内容", time.time())
        ]
        eng._check_pending_timeout = AsyncMock()
        eng._schedule_group = Mock()

        for _ in range(3):
            await eng._scan_due()

        eng._schedule_group.assert_not_called()

    @pytest.mark.asyncio
    async def test_expired_schedule_is_rescheduled(self):
        """候选时间已过期时必须重排期，否则 _loop 会 0.05 秒空转。"""
        eng = object.__new__(ProactiveEngine)
        eng._ignore_senders_get = lambda: {IGNORED}
        eng._state = Mock()
        eng._state.get_whitelist.return_value = {"g1"}
        eng._state.get_next_initiate_check.return_value = time.time() - 100
        eng._window = Mock()
        eng._window.get_messages.return_value = [
            WindowMessage(IGNORED, "u", "内容", time.time())
        ]
        eng._check_pending_timeout = AsyncMock()
        eng._schedule_group = Mock()

        await eng._scan_due()

        eng._schedule_group.assert_called_once()
