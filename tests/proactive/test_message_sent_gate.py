"""on_message_sent 通路的忽略门控测试。

审核发现此前完全没有覆盖这条通路（原疑点：sender_id 到底是用户还是 bot）。
框架事实：RespondStage 触发 after_message_sent 时传的是当次用户事件本体，
``get_sender_id()`` 返回触发者（用户），``get_self_id()`` 返回 bot。
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from astrbot_plugin_iris_memory.iris_memory.proactive.sender_gate import (
    SenderIgnoreList,
)
from astrbot_plugin_iris_memory.main import IrisMemoryPlugin

BOT = "999999"
IGNORED = "100000005"


def _real_gate(ignored: set[str]) -> SenderIgnoreList:
    class _Cfg:
        ignore_sender_ids = ",".join(sorted(ignored))
        ignore_sync_external = False

    return SenderIgnoreList(_Cfg(), config_dir=None)


def _async_lock():
    lock = MagicMock()
    lock.__aenter__ = AsyncMock(return_value=None)
    lock.__aexit__ = AsyncMock(return_value=None)
    return lock


def _make_plugin(*, ignored: set[str]):
    plugin = object.__new__(IrisMemoryPlugin)
    plugin._sender_gate = _real_gate(ignored)
    plugin._self_id = BOT
    plugin._sliding_window = Mock()
    plugin._kv_save = AsyncMock()
    plugin._state = Mock()
    plugin._state.get_lock.return_value = _async_lock()
    plugin._state.save_dirty = AsyncMock()
    plugin._state.is_whitelisted.return_value = True
    return plugin


def _event(sender_id: str, *, bot_text: str = "机器人的回复", mode: str | None = None):
    event = Mock()
    event.get_group_id.return_value = "g1"
    event.get_sender_id.return_value = sender_id
    event.get_self_id.return_value = BOT
    result = Mock()
    result.get_plain_text.return_value = bot_text
    event.get_result.return_value = result
    event.get_extra.side_effect = lambda key, default=None: (
        mode if key == "iris_mode" else default
    )
    return event


@pytest.mark.asyncio
async def test_bot_message_enters_window_even_from_ignored_trigger():
    """被忽略账号触发的那次回复，bot 自己的话仍要入窗（上下文完整性）。"""
    plugin = _make_plugin(ignored={IGNORED})
    event = _event(IGNORED, mode="chime_in")

    await plugin.on_message_sent(event)

    plugin._sliding_window.append.assert_called_once()
    appended = plugin._sliding_window.append.call_args[0][1]
    assert appended.sender_id == BOT
    assert appended.sender_name == "我"


@pytest.mark.asyncio
async def test_no_anchor_written_for_ignored_sender():
    """不给被忽略账号写关注锚点，否则下条消息会命中 follow_up 绕开名单。"""
    plugin = _make_plugin(ignored={IGNORED})
    event = _event(IGNORED, mode="chime_in")

    await plugin.on_message_sent(event)

    plugin._state.write_anchor.assert_not_called()


@pytest.mark.asyncio
async def test_anchor_written_for_normal_sender():
    plugin = _make_plugin(ignored={IGNORED})
    event = _event("1000000001", mode="chime_in")
    event.get_extra.side_effect = lambda key, default=None: (
        ("", "", "") if key == "iris_pending_watch"
        else ("chime_in" if key == "iris_mode" else default)
    )

    await plugin.on_message_sent(event)

    plugin._state.write_anchor.assert_called_once()
    kwargs = plugin._state.write_anchor.call_args[1]
    assert "1000000001" in kwargs["users"]


@pytest.mark.asyncio
async def test_normal_sender_gets_anchor_without_ignore_list():
    """未配置忽略名单时行为完全不变。"""
    plugin = _make_plugin(ignored=set())
    event = _event("1000000001", mode="follow_up")
    event.get_extra.side_effect = lambda key, default=None: (
        ("", "", "") if key == "iris_pending_watch"
        else ("follow_up" if key == "iris_mode" else default)
    )

    await plugin.on_message_sent(event)

    plugin._state.write_anchor.assert_called_once()


@pytest.mark.asyncio
async def test_pending_watch_drops_ignored_users():
    """even in pending watch, 被忽略账号也不能被写进锚点。"""
    plugin = _make_plugin(ignored={IGNORED})
    event = _event("1000000001", mode="chime_in")
    event.get_extra.side_effect = lambda key, default=None: (
        ([IGNORED, "1000000001"], [], "") if key == "iris_pending_watch"
        else ("chime_in" if key == "iris_mode" else default)
    )

    await plugin.on_message_sent(event)

    kwargs = plugin._state.write_anchor.call_args[1]
    assert IGNORED not in kwargs["users"]
    assert "1000000001" in kwargs["users"]
