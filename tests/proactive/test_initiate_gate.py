"""initiate 准入：窗口里只剩被忽略账号发言时不得发起。

对应审核 P2：打码后的上下文是一行空占位，模型没有依据却仍可能 speak。
"""

import time
from unittest.mock import AsyncMock, Mock

import pytest

from astrbot_plugin_iris_memory.iris_memory.proactive.perception import WindowMessage
from astrbot_plugin_iris_memory.iris_memory.proactive.proactive import ProactiveEngine

IGNORED = "100000005"
NORMAL = "1000000001"


def _engine(ignore: set[str]):
    engine = object.__new__(ProactiveEngine)
    engine._ignore_senders_get = lambda: set(ignore)
    return engine


def _msgs(*sender_ids: str):
    now = time.time()
    return [
        WindowMessage(sid, f"u{sid}", f"内容{i}", now + i)
        for i, sid in enumerate(sender_ids)
    ]


class TestHasSubstantive:
    def test_only_ignored_is_not_substantive(self):
        assert _engine({IGNORED})._has_substantive(_msgs(IGNORED)) is False
        assert (
            _engine({IGNORED})._has_substantive(_msgs(IGNORED, IGNORED)) is False
        )

    def test_mixed_is_substantive(self):
        assert _engine({IGNORED})._has_substantive(_msgs(IGNORED, NORMAL)) is True

    def test_no_ignore_list_always_substantive(self):
        """未配置名单时行为完全不变。"""
        assert _engine(set())._has_substantive(_msgs(IGNORED)) is True

    def test_without_callback_degrades_gracefully(self):
        engine = object.__new__(ProactiveEngine)
        engine._ignore_senders_get = None
        assert engine._has_substantive(_msgs(IGNORED)) is True

    def test_callback_raising_does_not_break(self):
        engine = object.__new__(ProactiveEngine)

        def boom():
            raise RuntimeError("boom")

        engine._ignore_senders_get = boom
        assert engine._has_substantive(_msgs(NORMAL)) is True


class TestScanDueSkipsIgnoredOnly:
    @pytest.mark.asyncio
    async def test_only_ignored_group_not_due(self):
        engine = _engine({IGNORED})
        engine._state = Mock()
        engine._state.get_whitelist.return_value = {"g1"}
        engine._state.get_next_initiate_check.return_value = time.time() - 1
        engine._state.is_muted.return_value = False
        engine._window = Mock()
        engine._window.get_messages.return_value = _msgs(IGNORED)
        engine._check_pending_timeout = AsyncMock()
        engine._schedule_group = Mock()
        engine.attempt_initiate = Mock()

        await engine._scan_due()

        # 未进入决策流程
        engine.attempt_initiate.assert_not_called()

    def test_next_deadline_skips_ignored_only_groups(self):
        """关键：_next_deadline 与 _scan_due 必须同判据。

        否则被跳过的群 deadline 停在过去，_loop 的 timeout 退化成
        0.05 秒并无限空转。
        """
        engine = _engine({IGNORED})
        engine._state = Mock()
        engine._state.get_whitelist.return_value = {"g1"}
        engine._state.get_next_initiate_check.return_value = time.time() - 100
        engine._window = Mock()
        engine._window.get_messages.return_value = _msgs(IGNORED)

        assert engine._next_deadline() == 0.0

    def test_next_deadline_picks_earliest_substantive_group(self):
        engine = _engine({IGNORED})
        now = time.time()
        engine._state = Mock()
        engine._state.get_whitelist.return_value = {"g1", "g2"}
        deadlines = {"g1": now - 200, "g2": now - 10}
        engine._state.get_next_initiate_check.side_effect = lambda gid: deadlines[gid]
        engine._window = Mock()
        windows = {"g1": _msgs(IGNORED), "g2": _msgs(NORMAL)}
        engine._window.get_messages.side_effect = lambda gid: windows[gid]

        assert abs(engine._next_deadline() - (now - 10)) < 1

    @pytest.mark.asyncio
    async def test_ignored_only_group_is_rescheduled_not_starved(self):
        """被跳过的群必须重排期，避免时间永远停在过去。"""
        engine = _engine({IGNORED})
        engine._state = Mock()
        engine._state.get_whitelist.return_value = {"g1"}
        engine._state.get_next_initiate_check.return_value = time.time() - 100
        engine._window = Mock()
        engine._window.get_messages.return_value = _msgs(IGNORED)
        engine._check_pending_timeout = AsyncMock()
        engine._schedule_group = Mock()

        await engine._scan_due()

        engine._schedule_group.assert_called_once()

    @pytest.mark.asyncio
    async def test_substantive_group_reaches_schedule_loop(self):
        engine = _engine({IGNORED})
        engine._state = Mock()
        engine._state.get_whitelist.return_value = {"g1"}
        engine._state.get_next_initiate_check.return_value = time.time() - 1
        engine._window = Mock()
        engine._window.get_messages.return_value = _msgs(NORMAL)
        engine._check_pending_timeout = AsyncMock()
        engine._is_busy = Mock(return_value=True)  # 让流程在排期处返回
        engine._schedule_group = Mock()
        engine._initiating = set()
        engine._skip_retry_after = {}

        await engine._scan_due()

        engine._check_pending_timeout.assert_called_once_with("g1")
