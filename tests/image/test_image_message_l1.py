"""纯图片消息入 L1 与按 message_id 拼回的回归测试。

事故背景：
  群主发了一张香蕉拳击图（无文字），紧接着问「图片内容是什么」，
  机器人回复「主人发了什么呀，咱这边完全看不到图喵！」

  两个 bug 叠加：
  1. 纯图片消息 message_str 为空，_add_to_l1_buffer 直接 return，
     这条消息从未进入 L1 —— 图片描述没有容器可承接。
  2. prepend_to_last_message 只认「最后一条」，图片解析要几秒，
     完成时最后一条往往已是别人的发言，same_source 校验必然失败。
"""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_iris_memory.iris_memory.config.config import init_config
from astrbot_plugin_iris_memory.iris_memory.l1_buffer.buffer import L1Buffer


class _AstrCfg(dict):
    def save_config(self):
        pass


@pytest.fixture
def buffer(tmp_path):
    data_dir = tmp_path / "plugin_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    init_config(_AstrCfg(), data_dir)
    buf = L1Buffer()
    buf._is_available = True
    return buf


class TestPrependByMessageId:
    def test_prepend_lands_on_own_message_despite_interruption(self, buffer):
        """他人插话后，图片描述仍应拼回图片自己那条消息。"""
        asyncio.run(
            buffer.add_message(
                group_id="g1",
                role="user",
                content="[图片]",
                source="owner",
                metadata={"message_id": "m_img"},
            )
        )
        asyncio.run(
            buffer.add_message(
                group_id="g1",
                role="user",
                content="欸？主人发了什么呀",
                source="other",
            )
        )

        ok = buffer.prepend_to_last_message(
            "g1", "[图:香蕉打拳击]", same_source="owner", message_id="m_img"
        )

        assert ok is True
        msgs = buffer.get_context("g1")
        assert "[图:香蕉打拳击]" in msgs[0].content
        assert "欸？主人发了什么呀" in msgs[1].content

    def test_fallback_to_last_when_message_id_unknown(self, buffer):
        asyncio.run(
            buffer.add_message(
                group_id="g1", role="user", content="旧消息", source="u1"
            )
        )
        ok = buffer.prepend_to_last_message(
            "g1", "[X]", same_source="u1", message_id="not_exist"
        )
        assert ok is True
        assert buffer.get_context("g1")[0].content.startswith("[X]")

    def test_same_source_still_guards_cross_user(self, buffer):
        """按 message_id 找不到又跨用户时，仍不应写错地方。"""
        asyncio.run(
            buffer.add_message(
                group_id="g1", role="user", content="别人说的", source="other"
            )
        )
        ok = buffer.prepend_to_last_message(
            "g1", "[图:X]", same_source="owner", message_id="missing"
        )
        assert ok is False


class TestPlaceholderCountedAsUnresolved:
    def test_residual_placeholder_blocks_request(self, buffer):
        """队列已 SUCCESS 但 L1 仍有 [IMG:] 时，必须仍算未完成。

        否则等待机制会误判「图都解析完了」而放行，
        模型拿到的是 [IMG:hash] 乱码。
        """
        asyncio.run(
            buffer.add_message(
                group_id="g1", role="user", content="[IMG:abc123]", source="u1"
            )
        )
        n = buffer.count_unresolved_recent_images("g1", 10)
        assert n >= 1

    def test_placeholder_cleared_after_replace(self, buffer):
        asyncio.run(
            buffer.add_message(
                group_id="g1", role="user", content="[IMG:abc123]", source="u1"
            )
        )
        buffer.replace_image_placeholder("g1", "[IMG:abc123]", "[图:香蕉]")
        assert buffer.count_unresolved_recent_images("g1", 10) == 0
        assert "香蕉" in buffer.get_context("g1")[0].content

    def test_failed_placeholder_is_readable(self, buffer):
        """解析失败的占位符要可读，不能是空串（空串等于没消息）。"""
        asyncio.run(
            buffer.add_message(
                group_id="g1", role="user", content="[IMG:bad]", source="u1"
            )
        )
        buffer.replace_image_placeholder("g1", "[IMG:bad]", "[图:无法识别]")
        content = buffer.get_context("g1")[0].content
        assert "无法识别" in content
        assert buffer.count_unresolved_recent_images("g1", 10) == 0


class TestWaitWakesOnReplace:
    def test_replace_notifies_waiter(self, buffer):
        """占位符替换完成应立刻唤醒等待方，而不是等满超时。"""
        asyncio.run(
            buffer.add_message(
                group_id="g1", role="user", content="[IMG:zzz]", source="u1"
            )
        )

        async def scenario():
            async def finish():
                await asyncio.sleep(0.05)
                buffer.replace_image_placeholder("g1", "[IMG:zzz]", "[图:香蕉]")

            task = asyncio.create_task(finish())
            loop = asyncio.get_running_loop()
            t0 = loop.time()
            remaining = await buffer.wait_for_recent_images(
                "g1", timeout_seconds=5
            )
            elapsed = loop.time() - t0
            await task
            return remaining, elapsed

        remaining, elapsed = asyncio.run(scenario())
        assert remaining == 0
        assert elapsed < 1.0, f"未及时唤醒，耗时 {elapsed:.2f}s"
