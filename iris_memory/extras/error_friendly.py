"""错误消息友好化处理器

在消息发送前拦截框架错误消息，替换为友好提示。
（自 v2 processing/message_processor.py 摘出，独立保留）
"""

from typing import Any, Final

from astrbot.api.event import AstrMessageEvent


class ErrorFriendlyMessages:
    """错误消息友好化配置"""

    ERROR_PATTERNS: Final[tuple] = (
        "AstrBot 请求失败",
        "错误类型:",
        "错误信息:",
        "请在平台日志查看",
    )

    # 模型侧「静默失败」的标志。
    # 这类错误不经过框架的错误模板，而是 AstrBot 自己的兜底文案，
    # 例如上游返回空 choices（内容策略拦截）时：
    #   "LLM 响应错误: All chat models failed: EmptyModelOutputError: ..."
    # 旧判定要求同时命中「错误词 + 框架词」，而这里没有 platform/api/框架
    # 之类的字样，导致永远识别不到、用户直接看到原始英文报错。
    MODEL_FAILURE_PATTERNS: Final[tuple] = (
        "llm 响应错误",
        "all chat models failed",
        "emptymodeloutputerror",
        "openai completion has no choices",
        "no usable output",
        "content_policy",
        "content policy",
        "safety",
        "blocked",
        "model output",
    )

    # 明确的内容拦截/审核拒绝：与「模型故障」区分开，
    # 让群友知道是内容触发了限制，而不是服务坏了。
    CONTENT_BLOCKED_PATTERNS: Final[tuple] = (
        "content_policy",
        "content policy",
        "content_filter",
        "content filter",
        "content management policy",
        "safety system",
        "safety filter",
        "safety_settings",
        "blocked",
        "flagged",
        "moderation",
        "敏感",
        "违规",
        "拦截",
        "审核",
        "内容安全",
    )

    # 空输出：模型没返回任何内容。
    # 注意：Gemini/OpenAI 兼容网关在**内容被安全策略拦截时**也返回
    # 空 choices（而非 4xx），因此空输出不宜说成「服务波动」，
    # 应当按「可能被拦截」如实告知。
    EMPTY_OUTPUT_PATTERNS: Final[tuple] = (
        "has no choices",
        "no usable output",
        "empty model output",
        "emptymodeloutputerror",
    )

    DEFAULT_FRIENDLY_MSG: Final[str] = "呜...遇到了一点问题，请稍后再试试吧~"
    NETWORK_ERROR_MSG: Final[str] = "网络好像不太稳定呢，稍后再试试？"
    RATE_LIMIT_MSG: Final[str] = "我需要休息一下，请稍后再来找我~"
    BAD_REQUEST_MSG: Final[str] = "请求出了点问题，稍后再试试吧~"
    # 内容被拦截：明确告知是「内容限制」导致，不甩锅给用户也不假装没事。
    CONTENT_BLOCKED_MSG: Final[str] = (
        "刚才那段内容被模型的安全策略拦下来了，没法继续聊这个呢～换个话题吧"
    )
    # 空输出：最常见成因就是上游内容拦截（返回空 choices 而不报错）。
    # 如实说明「被拦」，同时保留「确实没返回」这一事实，便于用户判断。
    EMPTY_OUTPUT_MSG: Final[str] = (
        "模型没有返回内容，这条多半是被内容安全策略拦了～换个说法或话题试试吧"
    )
    # 兼容旧名称：无细分信息时的拒答兜底
    MODEL_REFUSED_MSG: Final[str] = CONTENT_BLOCKED_MSG


class ErrorFriendlyProcessor:
    """错误消息友好化处理器

    在消息发送前拦截框架错误消息，替换为友好提示。

    Args:
        config: 插件配置对象（iris_memory.config.Config）
    """

    def __init__(self, config: Any) -> None:
        self._config = config

    def should_process(self, event: AstrMessageEvent) -> bool:
        """检查是否需要处理该事件"""
        return self._is_enabled()

    def process_result(self, result: Any) -> None:
        """处理消息结果，替换错误消息"""
        if not result:
            return

        text = self._get_result_plain_text(result)
        if not text:
            return

        if self._is_framework_error(text):
            friendly_msg = self._get_friendly_error_message(text)
            result.chain.clear()
            result.message(friendly_msg)

    def _is_enabled(self) -> bool:
        """检查错误消息友好化功能是否启用"""
        try:
            if hasattr(self._config, "get"):
                return bool(self._config.get("extras.error_friendly.enable", True))
            return True
        except Exception:
            return True

    def _get_result_plain_text(self, result: Any) -> str:
        """获取消息结果的纯文本内容"""
        if hasattr(result, "get_plain_text"):
            return result.get_plain_text() or ""
        return ""

    def _is_framework_error(self, text: str) -> bool:
        """检测是否为需要友好化的错误消息"""
        text_lower = text.lower()

        # 模型侧失败（空输出 / 内容策略拦截）：优先判定。
        # 这类文本往往不含 platform/api 等框架词，必须单独识别，
        # 否则会漏过、把英文堆栈直接甩给群友。
        if any(pat in text_lower for pat in ErrorFriendlyMessages.MODEL_FAILURE_PATTERNS):
            return True

        match_count = sum(
            1 for pattern in ErrorFriendlyMessages.ERROR_PATTERNS
            if pattern.lower() in text_lower
        )
        if match_count >= 2:
            return True

        if "400" in text or "bad request" in text_lower:
            if any(keyword in text_lower for keyword in ["请求", "request", "error", "failed"]):
                return True

        error_keywords = [
            "error", "failed", "exception", "traceback",
            "请求失败", "错误", "异常"
        ]
        framework_indicators = [
            "platform", "api", "http", "status code",
            "平台", "框架"
        ]

        has_error_keyword = any(kw in text_lower for kw in error_keywords)
        has_framework_indicator = any(ind in text_lower for ind in framework_indicators)

        return has_error_keyword and has_framework_indicator

    def _get_friendly_error_message(self, text: str) -> str:
        """根据错误内容返回合适的友好消息。

        判定顺序按「信息量从高到低」：
        1. 明确提到内容审核/拦截 → 直接说明被拦截
        2. 提到空输出/无 choices → 说明模型没返回内容（可能拦截或波动）
        3. 网络、限流、400 等基础设施问题
        4. 其余兜底
        """
        text_lower = text.lower()

        # 1. 内容拦截：消息里已经点明了 policy/safety/filter/审核/违规
        if any(pat in text_lower for pat in ErrorFriendlyMessages.CONTENT_BLOCKED_PATTERNS):
            return ErrorFriendlyMessages.CONTENT_BLOCKED_MSG

        # 2. 空输出但没说明原因
        if any(pat in text_lower for pat in ErrorFriendlyMessages.EMPTY_OUTPUT_PATTERNS):
            return ErrorFriendlyMessages.EMPTY_OUTPUT_MSG

        # 3. 基础设施类
        if "400" in text or "bad request" in text_lower:
            return ErrorFriendlyMessages.BAD_REQUEST_MSG

        if any(kw in text_lower for kw in ["network", "timeout", "连接", "网络"]):
            return ErrorFriendlyMessages.NETWORK_ERROR_MSG

        if any(kw in text_lower for kw in ["rate", "limit", "限流", "频率", "频繁"]):
            return ErrorFriendlyMessages.RATE_LIMIT_MSG

        # 4. 模型侧其它失败
        if any(pat in text_lower for pat in ErrorFriendlyMessages.MODEL_FAILURE_PATTERNS):
            return ErrorFriendlyMessages.EMPTY_OUTPUT_MSG

        return ErrorFriendlyMessages.DEFAULT_FRIENDLY_MSG
