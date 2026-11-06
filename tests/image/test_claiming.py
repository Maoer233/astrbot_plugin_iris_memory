import asyncio
from datetime import datetime, timedelta

import pytest

from astrbot_plugin_iris_memory.iris_memory.image.models import ImageParseStatus, ImageQueueItem
from astrbot_plugin_iris_memory.iris_memory.l1_buffer.buffer import L1Buffer


@pytest.mark.asyncio
async def test_fifty_concurrent_claims_return_same_hash_once():
    buffer = L1Buffer()
    item = ImageQueueItem(image_hash="same")
    buffer.add_image("session", item)

    claims = await asyncio.gather(
        *(
            buffer.claim_pending_images("session", 1, f"claim-{index}")
            for index in range(50)
        )
    )

    assert sum(len(batch) for batch in claims) == 1
    assert item.status == ImageParseStatus.PROCESSING
    assert item.attempt_count == 1


@pytest.mark.asyncio
async def test_stale_processing_claim_is_safely_recovered():
    buffer = L1Buffer()
    item = ImageQueueItem(
        image_hash="stale",
        status=ImageParseStatus.PROCESSING,
        claim_token="dead-worker",
        claimed_at=datetime.now() - timedelta(seconds=120),
    )
    buffer.add_image("session", item)

    claimed = await buffer.claim_pending_images(
        "session", 1, "new-worker", stale_after_seconds=60
    )

    assert claimed == [item]
    assert item.claim_token == "new-worker"
    assert item.attempt_count == 1


@pytest.mark.asyncio
async def test_intruder_cannot_claim_processing_state():
    """非持有者不得把状态改成 PROCESSING（防止抢走正在解析的图）。"""
    buffer = L1Buffer()
    item = ImageQueueItem(image_hash="busy")
    buffer.add_image("session", item)
    await buffer.claim_pending_images("session", 1, "owner")

    assert not buffer.mark_image_parsed(
        "session", "busy", ImageParseStatus.PROCESSING, "intruder"
    )
    assert item.claim_token == "owner"


@pytest.mark.asyncio
async def test_intruder_terminal_state_is_accepted_to_break_deadlock():
    """非持有者回写终态应被接受。

    这是修复死图循环的关键：claim 超时后会被回收并重新发放 token，
    原持有者解析失败回来时 token 已变。若拒绝写入，该图会永久停在
    PROCESSING，每次新消息都重新解析一次（实测可卡住数天）。
    """
    buffer = L1Buffer()
    item = ImageQueueItem(image_hash="owned")
    buffer.add_image("session", item)
    await buffer.claim_pending_images("session", 1, "owner")

    # 非持有者写 FAILED：接受，用于解除卡死
    assert buffer.mark_image_parsed(
        "session", "owned", ImageParseStatus.FAILED, "intruder"
    )
    assert item.status == ImageParseStatus.FAILED
    assert item.claim_token == ""


@pytest.mark.asyncio
async def test_owner_can_finish_image():
    buffer = L1Buffer()
    item = ImageQueueItem(image_hash="owned")
    buffer.add_image("session", item)
    await buffer.claim_pending_images("session", 1, "owner")

    assert buffer.mark_image_parsed(
        "session", "owned", ImageParseStatus.SUCCESS, "owner"
    )
    assert item.status == ImageParseStatus.SUCCESS
    assert item.claim_token == ""


@pytest.mark.asyncio
async def test_duplicate_hash_all_records_updated():
    """同 hash 的多条记录必须一起回写。

    旧的「命中第一条就 return」会让其余记录永远停在 PROCESSING，
    是死图循环的第二个成因。
    """
    buffer = L1Buffer()
    first = ImageQueueItem(image_hash="dup")
    second = ImageQueueItem(image_hash="dup")
    buffer._image_queues["session"] = [first, second]

    assert buffer.mark_image_parsed(
        "session", "dup", ImageParseStatus.FAILED, ""
    )
    assert first.status == ImageParseStatus.FAILED
    assert second.status == ImageParseStatus.FAILED


@pytest.mark.asyncio
async def test_add_image_deduplicates_same_hash():
    """同 hash 重复入队应合并，避免重复消耗解析配额。"""
    buffer = L1Buffer()
    buffer.add_image("session", ImageQueueItem(image_hash="same"))
    buffer.add_image("session", ImageQueueItem(image_hash="same"))
    buffer.add_image("session", ImageQueueItem(image_hash="other"))

    assert len(buffer._image_queues["session"]) == 2


@pytest.mark.asyncio
async def test_add_image_merges_missing_url():
    """重复入队时，新记录补上的 url 应合并进旧记录。"""
    from astrbot_plugin_iris_memory.iris_memory.image.models import ImageInfo

    buffer = L1Buffer()
    old = ImageQueueItem(image_hash="same", image_info=ImageInfo(url=""))
    new = ImageQueueItem(
        image_hash="same", image_info=ImageInfo(url="https://x/a.jpg")
    )
    buffer.add_image("session", old)
    buffer.add_image("session", new)

    assert len(buffer._image_queues["session"]) == 1
    assert old.image_info.url == "https://x/a.jpg"
