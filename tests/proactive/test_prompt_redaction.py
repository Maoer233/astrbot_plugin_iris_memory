"""决策 prompt 打码：被忽略账号的内容不得进入上下文。

背景：被忽略账号 @ 机器人时仍要正常应答，所以那条消息会进滑动窗口。
之后主动发起/插话组装 prompt 时若原样带上，模型会对着它的内容产生反应，
甚至把它写进 watch。
"""

import time

import pytest

from astrbot_plugin_iris_memory.iris_memory.proactive.config import ConfigManager
from astrbot_plugin_iris_memory.iris_memory.proactive.perception import (
    ContextPackager,
    WindowMessage,
)

IGNORED = "100000005"
NORMAL = "1000000001"


@pytest.fixture
def packager():
    cfg = ConfigManager({}, hidden_get={}.get)
    return ContextPackager(cfg, self_id_get=lambda: "bot-1")


@pytest.fixture
def messages():
    now = time.time()
    return [
        WindowMessage(IGNORED, "小鲸鱼", "今日签到成功，获得100金币", now),
        WindowMessage(NORMAL, "群友甲", "这游戏真好玩", now + 1),
        WindowMessage("bot-1", "我", "我也在玩", now + 2),
    ]


def test_ignored_content_is_redacted(packager, messages):
    out = packager.package("g1", messages, "chime_in", ignore_senders={IGNORED})

    assert "今日签到成功" not in out, "被忽略账号的正文不得进入 prompt"
    assert "已按配置忽略" in out


def test_other_participants_survive(packager, messages):
    out = packager.package("g1", messages, "chime_in", ignore_senders={IGNORED})

    assert "这游戏真好玩" in out, "正常群友内容必须保留"
    assert "我也在玩" in out, "bot 自己的发言必须保留"
    assert "[我(bot-1)]" in out


def test_timeline_position_preserved(packager, messages):
    """打码而非删除：行数一致，模型仍能理解对话轮次与时间线。"""
    plain = packager.package("g1", messages, "chime_in")
    redacted = packager.package("g1", messages, "chime_in", ignore_senders={IGNORED})

    assert plain.count("\n") == redacted.count("\n"), "行数必须一致"
    assert IGNORED in redacted, "保留 ID 便于模型理解是谁"


def test_no_ignore_set_keeps_original_behavior(packager, messages):
    out = packager.package("g1", messages, "chime_in")
    assert "今日签到成功" in out

    out2 = packager.package("g1", messages, "chime_in", ignore_senders=set())
    assert out == out2


def test_empty_ignore_set_matches_none(packager, messages):
    assert packager.package(
        "g1", messages, "chime_in", ignore_senders=None
    ) == packager.package("g1", messages, "chime_in", ignore_senders=set())


def test_self_id_never_redacted(packager, messages):
    """即使 bot 自己的 ID 误入忽略名单，它自己的发言也不该被打码。

    否则上下文里 bot 的历史发言会凭空消失，决策会失去连贯性。
    """
    out = packager.package("g1", messages, "chime_in", ignore_senders={"bot-1"})
    assert "我也在玩" in out, "bot 自身发言必须保留"
    assert "[我(bot-1)]" in out
