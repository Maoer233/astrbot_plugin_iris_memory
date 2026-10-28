"""主动回复侧的「发送者忽略」门控。

背景（与 AstrBot 管线的事实对应关系）：

- 「完全禁用」类插件（如 astrbot_plugin_completely_disable）用
  ``@filter.event_message_type(ALL, priority=sys.maxsize)`` + ``event.stop_event()``
  拦截消息。StarRequestSubStage 按 priority 倒序执行，命中后 break，
  因此这条链确实能拦住任意插件的 AdapterMessageEvent handler。
- 但这类插件普遍在入口处判断 ``event.is_at_or_wake_command``，
  只在「这条消息本身就是唤醒」时才拦截。普通群消息（既没 @ 也没前缀）
  的该标记为 False，于是黑名单直接 return，什么都不拦。
- Iris 的插话/跟进采样读的正是这类普通群消息，并且唤醒标记是 Iris
  自己在 handler 链尾（on_message 内）补上的，此时黑名单早已执行完毕。

结果：别的 bot 在群里随便说话 → Iris 采样 → 自己把
``is_at_or_wake_command`` 置 True → 进入统一决策 → 插话。
黑名单插件对这条通路天然无效。

本模块在 Iris 自己的门控里补一层发送者级过滤，并且：

- 只作用于**主动回复**（插话 / 跟进 / 发起 / 跟进评估），
  @ 唤醒的被动回复仍然正常工作；
- 可以自动同步 astrbot_plugin_completely_disable 的禁用名单，
  避免维护两份名单。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Callable

from astrbot.api import logger

# 外部黑名单插件的配置文件（data/config/<name>_config.json）
_EXTERNAL_CONFIG_NAME = "astrbot_plugin_completely_disable_config.json"
# 只有黑名单模式下的 disable_list 才能安全地当作「忽略名单」使用；
# 白名单模式下名单语义相反，同步会变成「忽略除名单外所有人」。
_EXTERNAL_BLACKLIST_MODE = "blacklist"
# 名单每次刷新后复用的时间（秒），避免每条群消息都读盘
_REFRESH_INTERVAL_SECONDS = 10.0

_TOKEN_SPLIT = re.compile(r"[,，、;；\s|/+&]+")
# re.ASCII：Python 的 \d 默认匹配 Unicode Nd（全角 ２、阿拉伯数字等），
# 而平台 sender_id 恒为半角 ASCII 数字，放行全角只会制造永不命中的幽灵条目。
# 括号包裹的 ID：``@昵称(100000005)`` / ``昵称（100000005）``
_PAREN_ID = re.compile(r"[（(]\s*(\d{5,})\s*[)）]", re.ASCII)
# 冒号分隔的 ID：``qq:FriendMessage:100000000`` / ``qq：100000000``
_COLON_ID = re.compile(r"[:：]\s*(\d{5,})\s*$", re.ASCII)
# 明确标注「这是用户」的写法（用于严格同步模式）
_USER_SHAPED = re.compile(r"FriendMessage|PrivateMessage|@", re.IGNORECASE)
# 条目里出现 @ 标记：说明带昵称，需整条解析（昵称与数字之间可能有空格）
_AT_MARK = re.compile(r"@")


def _parse_token(token: str) -> str | None:
    """从单个条目里解析出真实 ID，无法确定时返回 None。

    只采信**结构明确**的写法，按可信度排序：

    1. 条目本身就是纯 ID（允许前缀 ``@``）—— ``100000005`` / ``@100000005``
    2. 括号内的数字 —— ``@昵称(100000005)`` / ``@昵称（100000005）``
    3. 冒号结尾的数字 —— ``qq:FriendMessage:100000000``

    刻意**不做**「取最后一段数字」的兜底：``@玩家20245`` 这类昵称自带
    数字的写法会解析出错误 ID，把无辜用户加入忽略名单；``@某人(123)456789``
    这类则可能丢掉真正的 ID。解析失败时返回 None，由调用方提示用户粘贴
    明确 ID —— 漏配比误伤安全。
    """
    core = token.lstrip("@").strip()
    if not core:
        return None

    # 1. 纯数字（含 @ 前缀形式）
    if core.isascii() and core.isdigit() and len(core) >= 5:
        return core

    # 2/3. 括号或冒号包裹的 ID
    for pattern in (_PAREN_ID, _COLON_ID):
        match = pattern.search(token)
        if match:
            return match.group(1)
    return None


def extract_ids(raw: Any) -> set[str]:
    """把任意写法的 ID 清单收敛为纯数字 ID 集合。

    兼容 AstrBot 生态里常见的混合写法：

    - 纯 ID：``100000005``
    - 平台限定：``qq:FriendMessage:100000000``
    - 带昵称：``@茧墨（肥鱼版）(100000005)``
    - 列表、逗号、中文顿号、斜杠、加号、空白分隔的字符串

    没有数字片段的条目（如 ``None``）会被丢弃，不会污染匹配集合。
    """
    if raw is None:
        return set()

    if isinstance(raw, (list, tuple, set)):
        entries = [str(item) for item in raw]
    else:
        entries = [str(raw)]

    ids: set[str] = set()
    for entry in entries:
        entry = entry.strip()
        if not entry:
            continue

        tokens = [t.strip() for t in _TOKEN_SPLIT.split(entry) if t.strip()]
        has_at = bool(_AT_MARK.search(entry))

        for index, token in enumerate(tokens):
            parsed = _parse_token(token)
            if not parsed:
                continue
            # 「@昵称 空格 数字」的变体：分隔符把一条带昵称的写法拆成
            # "@玩家" + "20245"，后半段这个裸数字不可采信。
            #
            # 但必须同时满足「上一个 token 带 @」**且「它自己没产出 ID」**：
            #   "@玩家 20245"        → 上一段 "@玩家" 无 ID，20245 应丢弃
            #   "@某人(999999),888888" → 上一段已产出 999999，888888 是
            #                            独立条目，绝不能丢（顺序不对称 bug）
            if (
                index > 0
                and token.isascii()
                and token.isdigit()
                and _AT_MARK.search(tokens[index - 1])
                and _parse_token(tokens[index - 1]) is None
            ):
                continue
            ids.add(parsed)

        # 整条交给结构化解析，捞回 "@某人(999999)" 这类跨分隔符的写法
        if has_at or "(" in entry or "（" in entry or ":" in entry or "：" in entry:
            parsed = _parse_token(entry)
            if parsed:
                ids.add(parsed)
    return ids


class SenderIgnoreList:
    """主动回复的发送者忽略表。

    数据来源：

    1. 隐藏配置 ``reply_ignore_sender_ids``（手写名单，逗号分隔）
    2. 可选：``astrbot_plugin_completely_disable`` 的 ``disable_list``
       （由 ``reply_ignore_sync_external`` 控制，默认开启）

    刷新采用「10 秒内复用 + mtime 判定」的混合策略：隐藏配置改了能立刻
    生效（内存读取），外部 JSON 改了也不必每条消息都重新解析。
    """

    def __init__(self, config: Any, *, config_dir: Path | None = None) -> None:
        self._config = config
        self._config_dir = config_dir
        self._ids: set[str] = set()
        self._next_refresh = 0.0
        self._external_signature: tuple[float, int] | None = None
        self._external_ids: set[str] = set()
        # 名单变化回调：参数为新的忽略集合。用于清扫滑窗与锚点里的历史残留。
        self.on_change: Callable[[set[str]], None] | None = None

    # ------------------------------------------------------------------
    # 刷新
    # ------------------------------------------------------------------
    def refresh(self, *, force: bool = False) -> None:
        """按需重建忽略集合。

        自有名单（内存读）每次都重新解析——成本可忽略，且必须即时：
        否则 WebUI 改完要等 10 秒才生效，用户会以为没配上。
        只有需要读磁盘的外部名单才走复用窗口。
        """
        now = time.monotonic()
        ids = extract_ids(self._read_own_raw())
        if self._read_sync_external():
            if force or now >= self._next_refresh:
                self._next_refresh = now + _REFRESH_INTERVAL_SECONDS
            ids |= self._load_external()

        if ids == self._ids:
            return
        self._ids = ids
        callback = self.on_change
        if callback is not None:
            try:
                callback(set(ids))
            except Exception as exc:
                logger.debug(f"Iris Reply: 忽略名单变更回调失败（已忽略）：{exc}")

    def _read_own_raw(self) -> str:
        try:
            return str(getattr(self._config, "ignore_sender_ids", "") or "")
        except Exception as exc:  # 配置对象异常不应影响消息处理
            logger.debug(f"Iris Reply: 读取主动回复忽略名单失败（已忽略）：{exc}")
            return ""

    def _read_sync_external(self) -> bool:
        try:
            return bool(getattr(self._config, "ignore_sync_external", False))
        except Exception:
            return False

    def _read_sync_user_only(self) -> bool:
        """只同步「明确是用户」的条目（跳过裸数字）。

        默认 True：该外部黑名单的实际内容是「群号 + 好友号」混合体
        （源插件在群聊里只比对 group_id）。若把裸数字统统当发送者导入，
        会把同号的真人误加入忽略名单 —— 实测本机配置就会误伤 5 个真人。
        """
        try:
            return bool(getattr(self._config, "ignore_sync_user_only", True))
        except Exception:
            return True

    def _external_path(self) -> Path | None:
        if self._config_dir is None:
            return None
        return self._config_dir / _EXTERNAL_CONFIG_NAME

    def _load_external(self) -> set[str]:
        path = self._external_path()
        if path is None:
            return set()
        try:
            stat = path.stat()
            signature = (stat.st_mtime, stat.st_size)
        except OSError:
            # 外部插件未安装/被删除：清空缓存，避免残留旧名单
            self._external_signature = None
            self._external_ids = set()
            return set()

        if self._external_signature == signature:
            return self._external_ids

        try:
            # utf-8-sig：Windows 上手工编辑/记事本保存的配置常带 BOM，
            # 用 utf-8 严格解码会直接抛 JSONDecodeError，导致整张名单同步失败。
            text = path.read_text(encoding="utf-8-sig")
            data = json.loads(text)
        except Exception as exc:
            logger.debug(f"Iris Reply: 解析外部黑名单失败（已忽略）：{exc}")
            self._external_signature = None
            self._external_ids = set()
            return set()

        self._external_signature = signature
        if not isinstance(data, dict):
            self._external_ids = set()
            return set()

        # 注意：data.get("mode", default) 只在键缺失时兜底。老版本配置文件
        # 可能写成 "mode": ""，空串会让同步静默失效，因此必须显式收敛。
        raw_mode = data.get("mode")
        mode = str(raw_mode).strip().lower() if raw_mode is not None else ""
        if not mode:
            mode = _EXTERNAL_BLACKLIST_MODE
        if mode != _EXTERNAL_BLACKLIST_MODE:
            logger.debug(
                "Iris Reply: 外部禁用插件处于 %s 模式，跳过主动回复忽略名单同步", mode
            )
            self._external_ids = set()
            return set()

        raw = data.get("disable_list")
        if not isinstance(raw, list):
            self._external_ids = set()
            return set()

        # 关键事实：源插件 astrbot_plugin_completely_disable 的判定是
        # ``target_id = group_id if group_id else sender_id``，群聊里只比对
        # 群号、从不比对发送者。所以它的 disable_list 实际是「群号 + 好友号」
        # 的混合体。整表当 sender_id 导入，一旦某个真人的 QQ 号与某群号相同，
        # 就会误忽略那个真人。
        # 严格模式只吸收「明确是用户」的写法（@昵称(ID) / qq:FriendMessage:ID）。
        user_only = self._read_sync_user_only()
        kept: set[str] = set()
        ambiguous = 0
        for item in raw:
            text = str(item)
            ids = extract_ids([text])
            if not ids:
                continue
            if _USER_SHAPED.search(text):
                kept |= ids
            elif user_only:
                ambiguous += 1
            else:
                kept |= ids
                ambiguous += 1
        if ambiguous:
            logger.debug(
                "Iris Reply: 外部黑名单中有 %d 条纯数字条目可能是群号"
                "（已按%s模式处理）",
                ambiguous,
                "严格" if user_only else "兼容",
            )
        self._external_ids = kept
        return self._external_ids

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def is_ignored(self, sender_id: Any, *, self_id: str = "") -> bool:
        """该发送者的消息是否应被主动回复侧忽略。"""
        sid = str(sender_id or "").strip()
        if not sid:
            return False
        if self_id and sid == str(self_id):
            # 自身 ID 永不忽略，否则机器人自己的发言会把窗口和采样全掐掉
            return False
        self.refresh()
        return sid in self._ids

    def snapshot(self) -> set[str]:
        """当前生效的忽略集合（调试/日志用）。"""
        self.refresh()
        return set(self._ids)

    def external_snapshot(self) -> set[str]:
        """从外部黑名单同步来的 ID 集合（不含手写名单）。

        Web 页面需要把这类条目也列出来，否则用户只看到手写名单为空，
        却被告知「生效 2 个」，无从判断到底拦了谁。
        """
        self.refresh()
        if not self._read_sync_external():
            return set()
        return set(self._external_ids)
