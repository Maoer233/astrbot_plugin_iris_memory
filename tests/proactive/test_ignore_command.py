"""忽略名单管理命令的回归测试（不连真实平台）"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from astrbot_plugin_iris_memory.main import IrisMemoryPlugin, _split_id_list


def _plugin(current: str = "", effective: set[str] | None = None):
    plugin = object.__new__(IrisMemoryPlugin)
    plugin._reply_config = SimpleNamespace(
        ignore_sender_ids=current, ignore_sync_external=True
    )
    plugin._sender_gate = Mock()
    plugin._sender_gate.snapshot.return_value = effective or set()
    plugin.config = Mock()
    plugin._state = Mock()
    plugin._state.save_dirty = AsyncMock()
    plugin._kv_save = AsyncMock()
    return plugin


def _event():
    event = Mock()
    event.set_result = Mock()
    return event


def test_split_id_list():
    assert _split_id_list("") == []
    assert _split_id_list("1,2") == ["1", "2"]
    assert _split_id_list("1，2、3；4 5|6") == ["1", "2", "3", "4", "5", "6"]
    assert _split_id_list("  ") == []


@pytest.mark.asyncio
async def test_list_shows_effective_and_manual():
    plugin = _plugin(current="888888", effective={"888888", "100000005"})
    event = _event()

    await plugin.cmd_ignore(event, "list")

    text = event.set_result.call_args[0][0]
    assert "100000005" in text and "888888" in text and "2 个" in text


@pytest.mark.asyncio
async def test_add_writes_hidden_config():
    plugin = _plugin(current="888888")
    event = _event()

    await plugin.cmd_ignore(event, "add", "100000005")

    plugin.config.set_hidden.assert_called_once_with(
        "reply_ignore_sender_ids", "888888,100000005"
    )
    plugin._sender_gate.refresh.assert_called_once_with(force=True)


@pytest.mark.asyncio
async def test_add_rejects_duplicate_across_formats():
    # 手写名单里是带昵称写法，但同一个人不应被重复加入
    plugin = _plugin(current="@茧墨（肥鱼版）(100000005)")
    event = _event()

    await plugin.cmd_ignore(event, "add", "100000005")

    plugin.config.set_hidden.assert_not_called()
    assert "已在忽略名单" in event.set_result.call_args[0][0]


@pytest.mark.asyncio
async def test_remove_matches_normalized_form():
    plugin = _plugin(current="@茧墨（肥鱼版）(100000005),888888")
    event = _event()

    await plugin.cmd_ignore(event, "remove", "100000005")

    plugin.config.set_hidden.assert_called_once_with(
        "reply_ignore_sender_ids", "888888"
    )


@pytest.mark.asyncio
async def test_remove_missing_is_reported():
    plugin = _plugin(current="888888")
    event = _event()

    await plugin.cmd_ignore(event, "remove", "100000005")

    plugin.config.set_hidden.assert_not_called()
    assert "不在手写忽略名单" in event.set_result.call_args[0][0]


@pytest.mark.asyncio
async def test_remove_multi_id_entry_keeps_siblings():
    """一个条目里含多个 ID 时，只移除目标 ID，不能整条删掉。

    ``next(iter(extract_ids(...)))`` 的取值依赖 set 的哈希顺序，
    曾导致 "100000000/100000005" 整条被删、无辜 ID 一起消失。
    """
    plugin = _plugin(current="100000000/100000005")
    event = _event()

    await plugin.cmd_ignore(event, "remove", "100000005")

    plugin.config.set_hidden.assert_called_once()
    saved = plugin.config.set_hidden.call_args[0][1]
    assert "100000000" in saved, "同条目里的其他 ID 必须保留"
    assert "100000005" not in saved


@pytest.mark.asyncio
async def test_add_detects_id_inside_multi_entry():
    plugin = _plugin(current="100000000/100000005")
    event = _event()

    await plugin.cmd_ignore(event, "add", "100000005")

    plugin.config.set_hidden.assert_not_called()
    assert "已在忽略名单" in event.set_result.call_args[0][0]


@pytest.mark.asyncio
async def test_multiple_ids_in_one_command_rejected():
    plugin = _plugin()
    event = _event()

    await plugin.cmd_ignore(event, "add", "100000005/100000000")

    plugin.config.set_hidden.assert_not_called()
    assert "一次只提供一个" in event.set_result.call_args[0][0]


@pytest.mark.asyncio
async def test_non_numeric_id_gets_specific_hint():
    """Lark/WebChat 的 ID 非数字，要明确告知而不是笼统报错。"""
    plugin = _plugin()
    event = _event()

    await plugin.cmd_ignore(event, "add", "ou_7d8a6e6df7621556ce0d21922b676706")

    plugin.config.set_hidden.assert_not_called()
    msg = event.set_result.call_args[0][0]
    assert "只支持数字 ID" in msg


@pytest.mark.asyncio
async def test_invalid_id_rejected():
    """纯数字但不足 5 位：不是有效 ID，但不是「平台不支持」的情况。"""
    plugin = _plugin()
    event = _event()

    await plugin.cmd_ignore(event, "add", "1234")

    plugin.config.set_hidden.assert_not_called()
    assert "有效的用户 ID" in event.set_result.call_args[0][0]


@pytest.mark.asyncio
async def test_alpha_id_reports_platform_limitation():
    plugin = _plugin()
    event = _event()

    await plugin.cmd_ignore(event, "add", "webchat-user")

    plugin.config.set_hidden.assert_not_called()
    assert "只支持数字 ID" in event.set_result.call_args[0][0]


@pytest.mark.asyncio
async def test_sync_forces_refresh():
    plugin = _plugin(effective={"100000005"})
    event = _event()

    await plugin.cmd_ignore(event, "sync")

    plugin._sender_gate.refresh.assert_called_once_with(force=True)
    assert "100000005" in event.set_result.call_args[0][0]


@pytest.mark.asyncio
async def test_unknown_action_prints_usage():
    plugin = _plugin()
    event = _event()

    await plugin.cmd_ignore(event, "wat")

    assert "用法" in event.set_result.call_args[0][0]
