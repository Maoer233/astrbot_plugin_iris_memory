"""默认 Provider 字段读取：兼容 AstrBot 新版位置。

AstrBot 4.24+ 把「默认模型」从 ``provider_settings.default_provider_id``
迁移到 ``agent_runner.config.model.provider_id``，并把旧键从配置里 pop 掉。
只读旧键会导致用户在 WebUI 选了默认模型、插件侧仍拿到 None。
"""

import pytest

from astrbot_plugin_iris_memory.iris_memory.llm.manager import LLMManager


class _FakeContext:
    def __init__(self, cfg):
        self._cfg = cfg

    def get_config(self):
        return self._cfg


def _manager(cfg):
    m = LLMManager.__new__(LLMManager)
    m._context = _FakeContext(cfg)
    return m


class TestFieldLocation:
    def test_reads_new_location(self):
        """新版位置：agent_runner.config.model.provider_id"""
        cfg = {
            "agent_runner": {
                "config": {"model": {"provider_id": "V3API/gemini-3.8-flash-high"}}
            }
        }
        assert _manager(cfg)._get_default_provider() == "V3API/gemini-3.8-flash-high"

    def test_reads_legacy_location(self):
        """旧版位置仍需兼容。"""
        cfg = {"provider_settings": {"default_provider_id": "old/legacy"}}
        assert _manager(cfg)._get_default_provider() == "old/legacy"

    def test_new_takes_priority(self):
        cfg = {
            "agent_runner": {"config": {"model": {"provider_id": "new/model"}}},
            "provider_settings": {"default_provider_id": "old/model"},
        }
        assert _manager(cfg)._get_default_provider() == "new/model"

    def test_empty_new_falls_back_to_legacy(self):
        cfg = {
            "agent_runner": {"config": {"model": {"provider_id": ""}}},
            "provider_settings": {"default_provider_id": "fallback/model"},
        }
        assert _manager(cfg)._get_default_provider() == "fallback/model"


class TestRobustness:
    def test_empty_config(self):
        assert _manager({})._get_default_provider() is None

    @pytest.mark.parametrize(
        "cfg",
        [
            {"agent_runner": "not-a-dict"},
            {"agent_runner": {"config": "not-a-dict"}},
            {"agent_runner": {"config": {"model": "not-a-dict"}}},
            {"agent_runner": {"config": {}}},
            {"provider_settings": [1, 2, 3]},
        ],
    )
    def test_malformed_does_not_raise(self, cfg):
        """结构异常时静默返回 None，不能打断消息流程。"""
        assert _manager(cfg)._get_default_provider() is None
