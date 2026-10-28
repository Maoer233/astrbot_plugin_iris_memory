"""on_message 发送者忽略门控的集成测试

验证三件事：
1. 被忽略账号的普通群消息：不入滑窗、不触发采样，但会刷新主动发起的冷场计时
2. 被忽略账号 @ 机器人：仍然走被动回复，不被忽略
3. 决策模型给出的 watch 里混入被忽略账号时会被剔除
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from astrbot_plugin_iris_memory.iris_memory.proactive.sender_gate import (
    SenderIgnoreList,
)
from astrbot_plugin_iris_memory.main import IrisMemoryPlugin


def _real_gate(ignored: set[str]) -> SenderIgnoreList:
    """构造真实的 SenderIgnoreList（不再用 mock 复刻被测逻辑）。

    关闭外部同步，只用手写名单，行为完全由生产代码决定。
    """

    class _Cfg:
        ignore_sender_ids = ",".join(sorted(ignored))
        ignore_sync_external = False

    return SenderIgnoreList(_Cfg(), config_dir=None)


def _make_plugin(*, ignored: set[str]):
    plugin = object.__new__(IrisMemoryPlugin)
    plugin._reply_config = SimpleNamespace(
        enabled=True, quality_threshold=0.2, provider_id="p1"
    )
    plugin._gatekeeper = Mock()
    plugin._gatekeeper.should_process.return_value = True
    plugin._gatekeeper.quality_score.return_value = 1.0
    plugin._group_umo = {}
    plugin._umo_dirty = False
    plugin._self_id = "bot-1"
    plugin._sender_gate = _real_gate(ignored)
    plugin._sliding_window = Mock()
    plugin._proactive = Mock()
    plugin._signals = Mock()
    plugin._signals.evaluate_message.return_value = "chime_in"
    plugin._proactive.is_initiating.return_value = False
    plugin._state = Mock()
    plugin._state.consume_initiate_pending.return_value = False
    plugin._state.match_anchor_user.return_value = False
    plugin._state.can_detect.return_value = True
    plugin._state.get_lock.return_value = _async_lock()
    plugin._state.save_dirty = AsyncMock()
    plugin._decision_tickets = Mock()
    plugin._decision_tickets.claim.return_value = SimpleNamespace(ticket_id="t1")
    plugin._triggering = {}
    plugin._reply_in_progress = {}
    plugin._passive_active = {}
    plugin._follow_pending = set()
    plugin._tool_ctx = Mock()
    plugin._prepare_pure_at_request = AsyncMock(return_value=False)
    return plugin


def _async_lock():
    lock = MagicMock()
    lock.__aenter__ = AsyncMock(return_value=None)
    lock.__aexit__ = AsyncMock(return_value=None)
    return lock


def _make_event(sender_id: str, *, at_bot: bool = False):
    event = Mock()
    event.message_str = "随便说点什么"
    event.get_group_id.return_value = "g1"
    event.get_sender_id.return_value = sender_id
    event.get_sender_name.return_value = "昵称"
    event.get_self_id.return_value = "bot-1"
    event.is_at_or_wake_command = at_bot
    event.unified_msg_origin = "umo-1"
    event.get_extra.return_value = None
    return event


@pytest.mark.asyncio
async def test_ignored_sender_skips_window_and_sampling():
    plugin = _make_plugin(ignored={"100000005"})
    event = _make_event("100000005")

    await plugin.on_message(event)

    plugin._sliding_window.append.assert_not_called()
    plugin._signals.evaluate_message.assert_not_called()
    # 冷场计时仍要刷新：否则别人刷屏时会被判成冷场而主动发起
    plugin._proactive.notify_human_message.assert_called_once()


@pytest.mark.asyncio
async def test_ignored_sender_consumes_initiate_pending():
    """被忽略账号的接话也要消费 pending。

    否则 ProactiveEngine 会把它记成「发起无人接话」，
    streak++ → fatigue = 0.4**n，系统性压低该群的主动发起频率。
    """
    plugin = _make_plugin(ignored={"100000005"})
    plugin._state.consume_initiate_pending.return_value = True
    event = _make_event("100000005")

    await plugin.on_message(event)

    plugin._state.consume_initiate_pending.assert_called_once_with("g1")
    plugin._state.save_dirty.assert_awaited()


@pytest.mark.asyncio
async def test_no_save_when_no_pending():
    """没有 pending 时不该产生额外写盘。"""
    plugin = _make_plugin(ignored={"100000005"})
    plugin._state.consume_initiate_pending.return_value = False
    event = _make_event("100000005")

    await plugin.on_message(event)

    plugin._state.save_dirty.assert_not_awaited()


@pytest.mark.asyncio
async def test_ignored_sender_still_answers_when_at_bot():
    plugin = _make_plugin(ignored={"100000005"})
    event = _make_event("100000005", at_bot=True)

    await plugin.on_message(event)

    # 入窗 → 保留上下文；设置 passive 模式 → 正常应答
    plugin._sliding_window.append.assert_called_once()
    event.set_extra.assert_any_call("iris_mode", "passive")
    plugin._signals.evaluate_message.assert_not_called()


@pytest.mark.asyncio
async def test_normal_sender_enters_sampling_as_before():
    plugin = _make_plugin(ignored={"100000005"})
    event = _make_event("1000000001")

    await plugin.on_message(event)

    plugin._sliding_window.append.assert_called_once()
    plugin._signals.evaluate_message.assert_called_once()
    assert "g1" in plugin._triggering


@pytest.mark.asyncio
async def test_self_id_is_never_suppressed():
    plugin = _make_plugin(ignored={"bot-1"})
    event = _make_event("bot-1")

    await plugin.on_message(event)

    plugin._sliding_window.append.assert_called_once()


def test_watch_filter_drops_ignored_accounts():
    plugin = _make_plugin(ignored={"100000005"})
    assert plugin._filter_watch_users(["100000005", "1000000001", "None", ""]) == [
        "1000000001"
    ]


def test_watch_filter_noop_without_gate():
    plugin = object.__new__(IrisMemoryPlugin)
    assert plugin._filter_watch_users(["123", "456"]) == ["123", "456"]


def test_watch_filter_handles_none():
    plugin = _make_plugin(ignored=set())
    assert plugin._filter_watch_users(None) == []
    assert plugin._filter_watch_users([]) == []


def test_ignore_list_change_purges_window_and_anchor():
    plugin = _make_plugin(ignored=set())
    plugin._state.get_whitelist.return_value = ["g1", "g2"]

    plugin._on_ignore_list_changed({"100000005"})

    plugin._sliding_window.purge_senders.assert_called_once_with(
        set(), {"100000005"}, protected_ids={"bot-1"}
    )
    assert plugin._state.remove_anchor_watch.call_count == 2


def test_ignore_list_change_is_safe_on_partial_instance():
    plugin = object.__new__(IrisMemoryPlugin)
    plugin._on_ignore_list_changed({"100000005"})  # 不应抛异常


def test_empty_ignore_list_must_not_touch_window_or_anchor():
    """名单被清空时不得清理任何东西。

    ``purge_senders(set(), set())`` 语义是「删掉全部发送者的消息」，
    ``remove_anchor_watch(user_ids=[])`` 会走 ``anchor.clear()`` 分支，
    把每个群的跟进锚点整体清空。两者都不是「名单变空」的预期行为。
    """
    plugin = _make_plugin(ignored=set())
    plugin._state.get_whitelist.return_value = ["g1", "g2"]

    plugin._on_ignore_list_changed(set())

    plugin._sliding_window.purge_senders.assert_not_called()
    plugin._state.remove_anchor_watch.assert_not_called()


def test_ignore_list_change_never_clears_whole_anchor():
    """即使名单非空，也必须走「精确移除某人」而不是「整体 clear」。"""
    from astrbot_plugin_iris_memory.iris_memory.proactive.config import ConfigManager
    from astrbot_plugin_iris_memory.iris_memory.proactive.state import StateManager

    cfg = ConfigManager({}, hidden_get={}.get)
    state = StateManager(cfg)
    state.add_to_whitelist("g1")
    state.write_anchor(
        "g1",
        kind="passive",
        topic="t",
        bot_message="b",
        users=["1000000001", "100000005"],
        keywords=["kw"],
    )

    plugin = object.__new__(IrisMemoryPlugin)
    plugin._state = state
    plugin._sliding_window = Mock()
    # _self_id 必须已知，否则实现会保守跳过清理（见 test_self_id_protection）
    plugin._self_id = "bot-1"
    plugin._on_ignore_list_changed({"100000005"})

    anchor = state.get_anchor("g1")
    assert anchor.active, "锚点不能被整体清空"
    assert "100000005" not in anchor.participants
    assert "1000000001" in anchor.participants
    assert "kw" in anchor.keywords
