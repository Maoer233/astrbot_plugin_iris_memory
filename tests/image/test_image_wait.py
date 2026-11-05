"""图片解析等待机制测试。

背景：用户「发图 + 紧跟说话」时，图片解析在后台异步进行，
LLM 请求若抢在前面发出，模型只看得到 [IMG:hash] 占位符，
表现为「模型没识别到图片」。本机制在注入上下文前等待近期图片就绪。
"""

import asyncio
from datetime import datetime, timedelta

import pytest

from astrbot_plugin_iris_memory.iris_memory.image.models import (
    ImageParseStatus,
    ImageQueueItem,
)


class _FakeBuffer:
    """承载被测逻辑的最小缓冲：与 L1Buffer 的同名方法保持一致。"""

    def __init__(self):
        self._image_queues = {}
        self._image_events = {}

    def _get_queue_key(self, gid):
        return gid

    def _get_image_event(self, key):
        ev = self._image_events.get(key)
        if ev is None:
            ev = asyncio.Event()
            self._image_events[key] = ev
        return ev

    def _notify_image_state_changed(self, key):
        ev = self._image_events.get(key)
        if ev is not None:
            ev.set()

    def count_unresolved_recent_images(self, group_id, window_seconds):
        images = self._image_queues.get(group_id) or []
        if not images:
            return 0
        cutoff = datetime.now() - timedelta(seconds=max(0.0, window_seconds))
        return sum(
            1
            for img in images
            if img.timestamp is not None
            and img.timestamp >= cutoff
            and img.status
            in (ImageParseStatus.PENDING, ImageParseStatus.PROCESSING)
        )

    async def wait_for_recent_images(
        self, group_id, *, window_seconds=10.0, timeout_seconds=8.0
    ):
        if timeout_seconds <= 0:
            return self.count_unresolved_recent_images(group_id, window_seconds)
        remaining = self.count_unresolved_recent_images(group_id, window_seconds)
        if remaining <= 0:
            return 0
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        while remaining > 0:
            left = deadline - loop.time()
            if left <= 0:
                break
            event = self._get_image_event(group_id)
            event.clear()
            try:
                await asyncio.wait_for(event.wait(), timeout=left)
            except asyncio.TimeoutError:
                break
            remaining = self.count_unresolved_recent_images(
                group_id, window_seconds
            )
        return remaining


def _img(status=ImageParseStatus.PENDING, age_s=0.0):
    return ImageQueueItem(
        image_hash="h",
        group_id="g1",
        timestamp=datetime.now() - timedelta(seconds=age_s),
        status=status,
    )


class TestCountUnresolved:
    def test_no_images(self):
        assert _FakeBuffer().count_unresolved_recent_images("g1", 10) == 0

    def test_pending_counts(self):
        b = _FakeBuffer()
        b._image_queues["g1"] = [_img()]
        assert b.count_unresolved_recent_images("g1", 10) == 1

    def test_processing_counts(self):
        b = _FakeBuffer()
        b._image_queues["g1"] = [_img(ImageParseStatus.PROCESSING)]
        assert b.count_unresolved_recent_images("g1", 10) == 1

    @pytest.mark.parametrize(
        "status",
        [
            ImageParseStatus.SUCCESS,
            ImageParseStatus.FAILED,
            ImageParseStatus.DEFERRED,
        ],
    )
    def test_settled_statuses_do_not_count(self, status):
        """已完成/失败/延后都不应阻塞请求，否则配额耗尽时会等满超时。"""
        b = _FakeBuffer()
        b._image_queues["g1"] = [_img(status)]
        assert b.count_unresolved_recent_images("g1", 10) == 0

    def test_old_image_outside_window(self):
        b = _FakeBuffer()
        b._image_queues["g1"] = [_img(age_s=60)]
        assert b.count_unresolved_recent_images("g1", 10) == 0

    def test_recent_image_inside_window(self):
        b = _FakeBuffer()
        b._image_queues["g1"] = [_img(age_s=5)]
        assert b.count_unresolved_recent_images("g1", 10) == 1


class TestWaitForRecentImages:
    @pytest.mark.asyncio
    async def test_no_images_returns_immediately(self):
        b = _FakeBuffer()
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        assert await b.wait_for_recent_images("g1", timeout_seconds=8) == 0
        assert loop.time() - t0 < 0.05

    @pytest.mark.asyncio
    async def test_wakes_up_on_completion(self):
        """解析完成的通知应立即唤醒，而不是等满超时。"""
        b = _FakeBuffer()
        b._image_queues["g1"] = [_img()]

        async def finish():
            await asyncio.sleep(0.1)
            b._image_queues["g1"][0].status = ImageParseStatus.SUCCESS
            b._notify_image_state_changed("g1")

        task = asyncio.create_task(finish())
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        remaining = await b.wait_for_recent_images("g1", timeout_seconds=8)
        elapsed = loop.time() - t0
        await task

        assert remaining == 0
        assert elapsed < 1.0, f"未及时唤醒，耗了 {elapsed:.2f}s"

    @pytest.mark.asyncio
    async def test_timeout_returns_remaining(self):
        b = _FakeBuffer()
        b._image_queues["g1"] = [_img(), _img()]
        remaining = await b.wait_for_recent_images("g1", timeout_seconds=0.2)
        assert remaining == 2

    @pytest.mark.asyncio
    async def test_zero_timeout_does_not_block(self):
        b = _FakeBuffer()
        b._image_queues["g1"] = [_img()]
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        remaining = await b.wait_for_recent_images("g1", timeout_seconds=0)
        assert remaining == 1
        assert loop.time() - t0 < 0.05

    @pytest.mark.asyncio
    async def test_multiple_images_all_must_settle(self):
        b = _FakeBuffer()
        b._image_queues["g1"] = [_img(), _img(), _img()]

        async def finish_all():
            for i in range(3):
                await asyncio.sleep(0.05)
                b._image_queues["g1"][i].status = ImageParseStatus.SUCCESS
                b._notify_image_state_changed("g1")

        task = asyncio.create_task(finish_all())
        remaining = await b.wait_for_recent_images("g1", timeout_seconds=5)
        await task
        assert remaining == 0

    @pytest.mark.asyncio
    async def test_deferred_does_not_hold_request(self):
        """配额耗尽转 DEFERRED 后，请求应立即继续而不是等超时。"""
        b = _FakeBuffer()
        b._image_queues["g1"] = [_img()]

        async def defer():
            await asyncio.sleep(0.05)
            b._image_queues["g1"][0].status = ImageParseStatus.DEFERRED
            b._notify_image_state_changed("g1")

        task = asyncio.create_task(defer())
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        remaining = await b.wait_for_recent_images("g1", timeout_seconds=5)
        elapsed = loop.time() - t0
        await task

        assert remaining == 0
        assert elapsed < 1.0
