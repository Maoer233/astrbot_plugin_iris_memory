"""错误友好化：模型侧静默失败必须被识别。

事故背景：
  上游 gemini 因内容策略拦截返回 `choices=[]`，AstrBot 抛出
  EmptyModelOutputError，最终以原始英文报错发到群里：
    "LLM 响应错误: All chat models failed: EmptyModelOutputError: ..."

  旧实现要求「错误关键词 + 框架关键词（platform/api/平台/框架）」同时命中，
  而这段文本里没有任何框架词，所以永远识别不到，用户直接看到技术堆栈。
"""

import pytest

from astrbot_plugin_iris_memory.iris_memory.extras.error_friendly import (
    ErrorFriendlyMessages,
    ErrorFriendlyProcessor,
)


class _Cfg:
    def __init__(self, enabled=True):
        self._enabled = enabled

    def get(self, key, default=None):
        if key == "extras.error_friendly.enable":
            return self._enabled
        return default


@pytest.fixture
def proc():
    return ErrorFriendlyProcessor(_Cfg())


class _Chain(list):
    def clear(self):
        super().clear()


class _Result:
    """最小 MessageEventResult 替身：message() 是追加语义。"""

    def __init__(self, text=""):
        self.chain = _Chain([text] if text else [])

    def get_plain_text(self):
        return "".join(self.chain)

    def message(self, text):
        self.chain.append(text)
        return self


class TestModelFailureRecognized:
    @pytest.mark.parametrize(
        "text",
        [
            # 真实事故日志
            "LLM 响应错误: All chat models failed: EmptyModelOutputError: "
            "OpenAI completion has no choices. response_id=jH3pavuMF",
            "OpenAI completion has no usable output. response_id=x, "
            "finish_reason=content_filter",
            "All chat models failed: RateLimitError",
            "content_policy violation detected",
            "LLM 响应错误: ...",
        ],
    )
    def test_detected(self, proc, text):
        assert proc._is_framework_error(text) is True

    def test_uses_refusal_message(self, proc):
        """只有空 choices、没点名 policy 时，也要说清是「可能被拦」。"""
        text = "EmptyModelOutputError: OpenAI completion has no choices."
        msg = proc._get_friendly_error_message(text)
        assert "拦" in msg
        assert msg == ErrorFriendlyMessages.EMPTY_OUTPUT_MSG


class TestContentBlockedVsEmpty:
    """群友需要知道到底是「被拦截」还是「服务出问题」。"""

    @pytest.mark.parametrize(
        "text",
        [
            "Error: content_policy violation",
            "request blocked by safety filter",
            "content_filter triggered",
            "moderation flagged this request",
            "内容违规，已被拦截",
            "触发内容安全审核",
        ],
    )
    def test_explicit_block_says_blocked(self, proc, text):
        msg = proc._get_friendly_error_message(text)
        assert msg == ErrorFriendlyMessages.CONTENT_BLOCKED_MSG
        assert "安全策略拦" in msg

    @pytest.mark.parametrize(
        "text",
        [
            "OpenAI completion has no choices. response_id=x",
            "no usable output",
            "EmptyModelOutputError",
        ],
    )
    def test_empty_output_mentions_block_possibility(self, proc, text):
        """空 choices 最常见成因就是拦截，文案不能只说「服务波动」。"""
        msg = proc._get_friendly_error_message(text)
        assert msg == ErrorFriendlyMessages.EMPTY_OUTPUT_MSG
        assert "拦" in msg

    def test_infra_errors_not_called_blocked(self, proc):
        for text in ["network timeout", "rate limit exceeded", "bad request 400"]:
            msg = proc._get_friendly_error_message(text)
            assert msg != ErrorFriendlyMessages.CONTENT_BLOCKED_MSG
            assert "拦" not in msg


class TestLegacyPatternsStillWork:
    @pytest.mark.parametrize(
        "text",
        [
            "AstrBot 请求失败\n错误类型: ConnectionError\n错误信息: timeout",
            "HTTP 400 Bad Request: 请求格式错误",
            "platform api error: status code 500",
        ],
    )
    def test_detected(self, proc, text):
        assert proc._is_framework_error(text) is True


class TestNormalTextUntouched:
    @pytest.mark.parametrize(
        "text",
        [
            "嘿嘿，主人真可爱~",
            "这个图片好好笑",
            "我今天学了很多东西呢",
            "嗯...让我想想",
            "刚刚那个游戏真好玩",
        ],
    )
    def test_not_error(self, proc, text):
        assert proc._is_framework_error(text) is False

    def test_process_result_keeps_normal_text(self, proc):
        r = _Result("今天天气真好呀")
        proc.process_result(r)
        assert r.get_plain_text() == "今天天气真好呀"


class TestReplaceSemantics:
    def test_original_error_removed(self, proc):
        r = _Result(
            "LLM 响应错误: All chat models failed: EmptyModelOutputError: "
            "OpenAI completion has no choices. response_id=abc"
        )
        proc.process_result(r)
        out = r.get_plain_text()

        assert "response_id" not in out
        assert "EmptyModelOutputError" not in out
        assert "All chat models failed" not in out
        assert out == ErrorFriendlyMessages.EMPTY_OUTPUT_MSG

    def test_empty_result_safe(self, proc):
        r = _Result("")
        proc.process_result(r)  # 不应抛异常
        assert r.get_plain_text() == ""


class TestDisabledSwitch:
    def test_should_process_respects_config(self):
        assert ErrorFriendlyProcessor(_Cfg(True)).should_process(None) is True
        assert ErrorFriendlyProcessor(_Cfg(False)).should_process(None) is False
