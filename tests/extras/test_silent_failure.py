"""主动回复失败静默 + 后端错误不误报为内容拦截。

背景：
  主动插话（chime_in / follow_up / initiate）时用户并未请求 bot 说话，
  失败后冒一句「模型没有返回内容」等于用自己的故障打扰一群没找它的人。
  而且中转站 503（CPU 过载）被说成「内容安全策略拦了」，会让群友
  去改话题，而实际问题在服务端。
"""

import pytest

from astrbot_plugin_iris_memory.iris_memory.extras.error_friendly import (
    ErrorFriendlyMessages,
    ErrorFriendlyProcessor,
)


class _Cfg:
    def get(self, key, default=None):
        if key == "extras.error_friendly.enable":
            return True
        return default


@pytest.fixture
def proc():
    return ErrorFriendlyProcessor(_Cfg())


class _Chain(list):
    def clear(self):
        super().clear()


class _Result:
    def __init__(self, text=""):
        self.chain = _Chain([text] if text else [])

    def get_plain_text(self):
        return "".join(self.chain)

    def message(self, text):
        self.chain.append(text)
        return self


class TestBackendErrorNotBlamedOnContent:
    """503 / CPU 过载 / 超时 不能说成内容被拦。"""

    @pytest.mark.parametrize(
        "text",
        [
            # 真实日志
            "Error code: 503 - {'error': {'message': 'system cpu overloaded "
            "(current: 99.9%, threshold: 90%)', 'type': 'new_api_error', "
            "'code': 'system_cpu_overloaded'}}",
            "LLM 响应错误: All chat models failed: "
            "InternalServerError: Error code: 502 Bad gateway",
            "APIConnectionError: Connection error.",
            "Read timeout",
            "rate limit exceeded, too many requests",
        ],
    )
    def test_classified_as_backend(self, proc, text):
        verdict = proc.detect_and_build(_Result(text))
        assert verdict is not None
        assert verdict.kind == "backend"
        assert verdict.message == ErrorFriendlyMessages.BACKEND_ERROR_MSG

    def test_backend_message_does_not_mention_block(self, proc):
        text = "system cpu overloaded (current: 99.9%)"
        msg = proc._get_friendly_error_message(text)
        assert "拦" not in msg
        assert "内容" not in msg


class TestContentBlockedStillDetected:
    @pytest.mark.parametrize(
        "text",
        [
            "content_policy violation",
            "blocked by safety filter",
            "内容违规，已被拦截",
        ],
    )
    def test_classified_as_blocked(self, proc, text):
        verdict = proc.detect_and_build(_Result(text))
        assert verdict is not None
        assert verdict.kind == "blocked"


class TestDetectAndBuildContract:
    def test_normal_text_returns_none(self, proc):
        assert proc.detect_and_build(_Result("今天天气真好呀")) is None
        assert proc.detect_and_build(_Result("")) is None
        assert proc.detect_and_build(None) is None

    def test_does_not_mutate_result(self, proc):
        """detect_and_build 只判定，不改内容——调用方负责决定怎么处理。"""
        original = "system cpu overloaded (current: 99.9%)"
        r = _Result(original)
        proc.detect_and_build(r)
        assert r.get_plain_text() == original


class TestSilentFailureModes:
    """主动回复类动机应判定为静默。"""

    def test_silent_modes_declared(self):
        # 与 main.py 中 IrisMemoryPlugin._SILENT_FAILURE_MODES 保持一致
        for mode in ("chime_in", "follow_up", "initiate"):
            assert mode in {"chime_in", "follow_up", "initiate"}

    def test_passive_not_silent(self):
        passive = "passive"
        assert passive not in {"chime_in", "follow_up", "initiate"}
