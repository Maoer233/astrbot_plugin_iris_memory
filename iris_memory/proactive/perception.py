from __future__ import annotations

import re
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING
from xml.sax.saxutils import quoteattr

import tiktoken

from astrbot.api import logger

from .config import ConfigManager
from .state import StateManager

if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent

_RE_DIGITS_ONLY = re.compile(r"[\d\s]+")
_RE_NONALNUM_ONLY = re.compile(r"[\W_]+")


@dataclass
class WindowMessage:
    sender_id: str
    sender_name: str
    content: str
    timestamp: float


class Gatekeeper:
    def __init__(self, config: ConfigManager, state: StateManager) -> None:
        self._config = config
        self._state = state

    def should_process(self, event: AstrMessageEvent) -> bool:
        message_str = event.message_str
        if not message_str:
            return False
        if message_str.startswith("/"):
            return False
        if event.is_private_chat():
            return False
        if self._state.is_muted():
            return False
        group_id = event.get_group_id()
        if not self._state.is_whitelisted(group_id):
            return False
        return True

    def quality_score(self, text: str) -> float:
        if not text or not text.strip():
            return 0.0
        stripped = text.strip()
        if _RE_DIGITS_ONLY.fullmatch(stripped):
            return 0.1
        if len(stripped) <= 2:
            return 0.2
        if _RE_NONALNUM_ONLY.fullmatch(stripped):
            return 0.1
        # str.isalnum() 对 CJK 字符同样返回 True，无需额外的区间判断
        alpha_count = sum(1 for c in stripped if c.isalnum())
        ratio = alpha_count / len(stripped) if stripped else 0
        return min(1.0, ratio)


class SlidingWindow:
    def __init__(self, config: ConfigManager) -> None:
        self._config = config
        self._windows: dict[str, deque[WindowMessage]] = {}

    def _ensure_window(self, group_id: str) -> deque[WindowMessage]:
        if group_id not in self._windows:
            self._windows[group_id] = deque(maxlen=self._config.window_size)
        return self._windows[group_id]

    def append(self, group_id: str, msg: WindowMessage) -> None:
        window = self._ensure_window(group_id)
        window.append(msg)

    def get_messages(self, group_id: str) -> list[WindowMessage]:
        window = self._ensure_window(group_id)
        return list(window)

    def remove_group(self, group_id: str) -> None:
        self._windows.pop(group_id, None)

    def discard_through(self, group_id: str, timestamp: float) -> int:
        """丢弃不晚于 ``timestamp`` 的消息，保留决策调用期间新到达的消息。"""
        window = self._windows.get(group_id)
        if not window:
            return 0

        remaining = [msg for msg in window if msg.timestamp > timestamp]
        removed = len(window) - len(remaining)
        if remaining:
            self._windows[group_id] = deque(
                remaining,
                maxlen=self._config.window_size,
            )
        else:
            self._windows.pop(group_id, None)
        return removed

    def cleanup(self, active_group_ids: set[str]) -> None:
        stale = [gid for gid in self._windows if gid not in active_group_ids]
        for gid in stale:
            self._windows.pop(gid, None)

    def purge_senders(
        self,
        group_ids: set[str],
        sender_ids: set[str],
        protected_ids: set[str] | None = None,
    ) -> int:
        """从指定群的窗口里剔除某些发送者，返回移除条数。

        忽略名单变更后调用：已进入窗口的历史消息不会自己消失，
        决策 prompt 仍会带上它们，模型据此评（甚至把该账号写进 watch）。
        主动清扫一次，让忽略立即对齐。

        ``protected_ids``（通常是机器人自身）即使出现在名单里也不删：
        ``is_ignored`` 对自身 ID 恒返回 False，若两者语义不一致，
        用户把 bot 自己的 ID 写进名单就会把 bot 的历史发言从窗口抹掉。
        """
        if not sender_ids:
            return 0
        targets = set(sender_ids) - (protected_ids or set())
        if not targets:
            return 0
        removed = 0
        for gid in list(self._windows):
            if group_ids and gid not in group_ids:
                continue
            window = self._windows.get(gid)
            if not window:
                continue
            remaining = [msg for msg in window if msg.sender_id not in targets]
            if len(remaining) == len(window):
                continue
            removed += len(window) - len(remaining)
            if remaining:
                self._windows[gid] = deque(remaining, maxlen=self._config.window_size)
            else:
                self._windows.pop(gid, None)
        return removed


class ContextPackager:
    def __init__(
        self,
        config: ConfigManager,
        self_id_get: Callable[[], str] | None = None,
    ) -> None:
        self._config = config
        self._self_id_get = self_id_get
        try:
            self._encoding = tiktoken.get_encoding("cl100k_base")
        except Exception as e:
            logger.warning("Iris Reply: tiktoken init failed, falling back to char estimate: %s", e)
            self._encoding = None

    def _count_tokens(self, text: str) -> int:
        if self._encoding:
            return len(self._encoding.encode(text))
        return max(1, len(text) // 2)

    def package(
        self,
        group_id: str,
        messages: list[WindowMessage],
        motive: str,
        ignore_senders: set[str] | None = None,
    ) -> str:
        """把窗口消息打包成决策上下文。

        ``ignore_senders`` 命中的发送者行改写成「已忽略」占位：
        保留时间线位置与对话连贯性（否则上下文会出现突兀跳跃，
        模型可能误判话题走向），但不把具体内容喂给决策模型，
        避免它对着被忽略账号的发言产生反应或写进 watch。
        """
        self_id = self._self_id_get() if self._self_id_get else ""
        skip = ignore_senders or set()
        # 自身 ID 永不打码：用户可能手滑把自己 bot 的 ID 加进忽略名单，
        # 若因此抹掉 bot 自己的历史发言，上下文会断裂、决策质量下降。
        if self_id:
            skip = skip - {str(self_id)}
        lines: list[str] = []
        token_counts: list[int] = []
        for msg in messages:
            # bot 自身发言统一标记为「我」，避免决策模型把自己的历史发言
            # 误认为第三方群友（曾因硬编码插件名产生"Iris 代答"错觉）
            name = "我" if self_id and msg.sender_id == self_id else msg.sender_name
            if msg.sender_id in skip:
                line = f"[{name}({msg.sender_id})] （该账号的发言已按配置忽略）"
            else:
                line = f"[{name}({msg.sender_id})] {msg.content}"
            tc = self._count_tokens(line)
            lines.append(line)
            token_counts.append(tc)

        total_tokens = sum(token_counts)
        max_tokens = self._config.max_token

        start = 0
        while total_tokens > max_tokens and start < len(lines):
            total_tokens -= token_counts[start]
            start += 1

        context_text = "\n".join(lines[start:])

        escaped_motive = quoteattr(motive)
        header = f"<iris:reply-context motive={escaped_motive}>\n"
        footer = "\n</iris:reply-context>"
        return header + context_text + footer
