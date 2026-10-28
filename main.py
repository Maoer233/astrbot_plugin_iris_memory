"""
Iris Memory - AstrBot 轻量化三合一插件

v3.0 架构：
- 记忆侧（源自 Iris Chat Memory 轻量方案）：
  L1 消息上下文缓冲 / L2 记忆库（FAISS + SQLite）/ L3 知识图谱（SQLite）
  + 用户/群聊画像 + 梦境离线加工 + 图片解析
- 主动回复侧（源自 Iris Reply 统一决策模型）：
  chime_in 跟话 / follow_up 跟进 / initiate 发起 / watch 被动评估，
  SignalGate 本地零成本门控 + 单次 LLM 统一决策 + ThreadAnchor 记账
- 人格自学习迭代：
  脱敏采样 + 风格分析 + 候选生成 + 独立审查 + Revision 审批/回滚

钩子编排（等价于原两插件并存时的兼容性契约）：
  群消息 → on_message（主动回复门控，设 iris_mode extra）
        → on_all_message（入 L1、图片入队）
  门控命中 → 统一决策（llm_generate 直调，不触发钩子）
        → 决策发言：劫持主管线，注入 SPEAK_HINTS（mark_as_temp）
  on_llm_request → 记忆侧：清空 contexts，注入 L1/L2/L3/画像（mark_as_temp）
  on_llm_response → 记忆侧：bot 回复入 L1 → 主动回复侧：按 iris_mode 记账
  after_message_sent → 主动回复侧：入滑动窗口 + 写 ThreadAnchor
  initiate 直发（context.send_message）→ 手动记账 + 回填 L1
"""

import asyncio
import hashlib
import re
import time
from pathlib import Path
from typing import Any, Optional

from .iris_memory.config import init_config, Config

from astrbot.api import AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.message_components import At
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.agent.message import TextPart
from astrbot.core.provider.entities import LLMResponse, ProviderRequest

from .iris_memory.core import (
    ComponentManager,
    get_logger,
    create_components,
    initialize_components,
    shutdown_components,
    handle_user_message,
    preprocess_llm_request,
    handle_llm_response,
    handle_agent_done,
    handle_pre_request_cleanup,
    handle_initiate_backfill,
    set_component_manager,
    get_run_log_manager,
)
from .iris_memory.tools import register_llm_tools
from .iris_memory.utils.token_counter import warm_up_encoders_async
from .iris_memory.web import register_all_routes
from .iris_memory.llm import LLMManager
from .iris_memory.llm_modules import FRAMEWORK_REPLY, proactive_reply_module
from .iris_memory.commands import (
    get_registry,
    execute_command,
    L1CommandHandler,
    L2CommandHandler,
    L3CommandHandler,
    ProfileCommandHandler,
    AllCommandHandler,
    LearningCommandHandler,
    EvolutionCommandHandler,
)
from .iris_memory.proactive.admin import AdminCommands
from .iris_memory.proactive.api import (
    register_web_apis as register_reply_web_apis,
    sync_stats_group_state,
)
from .iris_memory.proactive.config import ConfigManager as ReplyConfigManager
from .iris_memory.proactive.decision import (
    INPUT_SAFETY_COOLDOWN_MINUTES,
    DecisionCore,
    DecisionRequest,
    SafetyCleanupResult,
)
from .iris_memory.proactive.perception import (
    ContextPackager,
    Gatekeeper,
    SlidingWindow,
    WindowMessage,
)
from .iris_memory.proactive.prompts import SPEAK_HINTS
from .iris_memory.proactive.proactive import ProactiveEngine
from .iris_memory.proactive.sender_gate import SenderIgnoreList, extract_ids
from .iris_memory.proactive.signals import SignalGate
from .iris_memory.proactive.state import StateManager
from .iris_memory.proactive.stats import StatsCollector
from .iris_memory.proactive.tickets import DecisionTicketRegistry
from .iris_memory.proactive.time_hint import resolve_datetime_reminder
from .iris_memory.proactive.tools import ToolContext
from .iris_memory.extras import ErrorFriendlyProcessor, MarkdownStripper

logger = get_logger("main")

PLUGIN_NAME = "astrbot_plugin_iris_memory"

# 旧版（v2.x）数据自动迁移开关；v4 删除本常量与 iris_memory/legacy_migration/ 即彻底移除
LEGACY_MIGRATION_ENABLED = True

_IRIS_ACTIVE_TIMEOUT = 120
# @ 唤醒后的活跃窗口（秒）。窗口内群消息逐条评估是否接话，到期回归常规采样。
WAKE_WINDOW_SECONDS = 60
_UMO_KV_KEY = "iris_reply:group_umo"

# 决策模型偶尔会把空值写成这些字面量塞进 watch 字段。
# 刻意不含 "na" / "n/a"：Lark 的 sender_id 形如 ou_xxx、WebChat 直接拿
# username 当 user_id，短字母串完全可能是真实 ID，判成垃圾会误删。
_WATCH_JUNK_VALUES = {"none", "null", "undefined", "无", "空"}

_ID_LIST_SPLIT = re.compile(r"[,，、;；\s|]+")


def _split_id_list(raw: str) -> list[str]:
    """把手写忽略名单原文拆成条目列表（保留原始写法，便于回显给人看）。"""
    if not raw:
        return []
    return [item.strip() for item in _ID_LIST_SPLIT.split(str(raw)) if item.strip()]

_PASSIVE_WATCH_SIGNAL = re.compile(
    r"[?？]|"
    r"我(?:会|将|之后|稍后|晚点|等会|回头)|"
    r"(?:下次|之后|稍后|晚点|有结果|有消息)(?:再|会)?(?:告诉|提醒|联系|确认|回复)|"
    r"(?:等|等待).{0,16}(?:回复|消息|结果)|"
    r"\b(?:i(?:'ll| will)|follow up|let you know|remind|check back|waiting for)\b",
    re.IGNORECASE,
)


def _detect_passive_trigger(event: AstrMessageEvent, req, context: Context) -> None:
    """检测 LLM 请求是否为被动触发（sampling/主动回复）

    当用户消息不以唤醒前缀开头且未 @机器人 时，LLM 请求可能是由 AstrBot 的
    active_reply/sampling 机制触发的。此时标记事件，供后续钩子
    判断是否跳过图片解析等高 token 消耗操作。

    注：本插件主动回复触发的请求会将 is_at_or_wake_command 置 True，
    天然不会被误判为被动触发，无需额外处理 iris_mode。
    """
    try:
        is_at_or_wake = getattr(event, "is_at_or_wake_command", False)
        if not is_at_or_wake:
            event.set_extra("iris_passive_trigger", True)
            logger.debug(
                "检测到被动触发（sampling/主动回复），is_at_or_wake_command 为 False"
            )
    except Exception as e:
        logger.debug(f"被动触发检测异常（不影响正常流程）：{e}")


def _resolve_astrbot_config_dir(data_dir: Path | None = None) -> Path | None:
    """定位 AstrBot 的 data/config 目录，用于读取其他插件的配置文件。

    优先用插件自身数据目录反推：``data/plugin_data/<插件名>`` 往上两级就是
    ``data``，其下的 ``config`` 即目标目录。这条路径与进程 cwd 无关，
    在桌面打包、``ASTRBOT_ROOT`` 覆盖、源码运行三种部署下都成立。
    框架路径工具作为兜底，再失败则返回 None，由调用方跳过同步。
    """
    if data_dir is not None:
        try:
            candidate = Path(data_dir).parent.parent / "config"
            if candidate.is_dir():
                return candidate
        except Exception:
            pass
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_config_path

        candidate = Path(get_astrbot_config_path())
        if candidate.is_dir():
            return candidate
    except Exception:
        pass
    logger.debug("Iris Reply: 无法定位 AstrBot 配置目录，跳过外部忽略名单同步")
    return None


def _is_pure_at_self(event: AstrMessageEvent) -> bool:
    """Return whether the message consists only of an @ mention to the bot."""
    try:
        messages = event.get_messages()
        return (
            len(messages) == 1
            and isinstance(messages[0], At)
            and str(messages[0].qq) == str(event.get_self_id())
        )
    except Exception:
        return False


class IrisMemoryPlugin(Star):
    """AstrBot 轻量化三合一插件主类。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.context: Context = context

        try:
            # ── 记忆侧初始化 ──
            data_dir = StarTools.get_data_dir()
            self.config: Config = init_config(config, data_dir)
            logger.info(f"插件数据目录：{data_dir}")

            components = create_components(context, self)
            self.component_manager: Optional[ComponentManager] = ComponentManager(
                components
            )
            self._llm_manager = self.component_manager.get_component(
                "llm_manager", LLMManager
            )

            set_component_manager(self.component_manager)

            from .iris_memory.image.recorder_bridge import init_recorder_bridge

            init_recorder_bridge(context)

            # ── extras（自 v2 保留的低成本功能） ──
            self._error_processor = ErrorFriendlyProcessor(self.config)
            self._markdown_stripper = MarkdownStripper(
                context=self.context,
                config=self.config,
            )

            # ── 主动回复侧初始化 ──
            self._reply_config = ReplyConfigManager(
                config if config else context.get_config(),
                hidden_get=self.config.get,
            )
            self._state = StateManager(self._reply_config)
            self._tool_ctx = ToolContext()
            self._gatekeeper = Gatekeeper(self._reply_config, self._state)
            self._sender_gate = SenderIgnoreList(
                self._reply_config,
                config_dir=_resolve_astrbot_config_dir(data_dir),
            )
            # Web API 拿不到插件实例，把「当前生效集合」的取值函数注入到
            # 全局 Config 单例，让忽略名单页面读到的就是运行时真实生效的数据。
            self.config.set_ignore_snapshot_provider(self._ignore_senders_snapshot)
            self.config.set_ignore_external_provider(self._ignore_external_snapshot)
            self._sliding_window = SlidingWindow(self._reply_config)
            # 忽略名单变化时清扫窗口与锚点里的历史残留：否则刚加入名单的
            # 账号，其旧消息仍会随决策 prompt 送到模型，甚至被写进 watch。
            self._sender_gate.on_change = self._on_ignore_list_changed
            self._context_packager = ContextPackager(
                self._reply_config, self_id_get=lambda: self._self_id
            )
            self._signals = SignalGate(self._reply_config, self._state)
            self._decision_core = DecisionCore(
                self._reply_config, self._state, self._sliding_window, self._context_packager,
                time_hint_get=lambda gid: resolve_datetime_reminder(
                    self.context, self._group_umo.get(gid),
                ),
                ignore_senders_get=self._ignore_senders_snapshot,
            )
            self._admin = AdminCommands(self._state)
            self._stats = StatsCollector()
            self._reply_in_progress: dict[str, float] = {}
            self._passive_active: dict[str, float] = {}
            self._triggering: dict[str, float] = {}
            self._decision_tickets = DecisionTicketRegistry(_IRIS_ACTIVE_TIMEOUT)
            self._passive_watch_last: dict[str, float] = {}
            self._passive_watch_hash: dict[str, str] = {}
            self._follow_pending: set[str] = set()
            self._group_umo: dict[str, str] = {}
            self._umo_dirty: bool = False
            self._self_id: str = ""
            self._save_task: asyncio.Task | None = None
            self._save_interval = 30
            self._proactive = ProactiveEngine(
                self.context,
                self._reply_config,
                self._state,
                self._sliding_window,
                self._signals,
                self._decision_core,
                self._stats,
                llm_manager=self._llm_manager,
                packager=self._context_packager,
                umo_get=lambda gid: self._group_umo.get(gid),
                is_busy=self._is_busy,
                self_id_get=lambda: self._self_id,
                save_fn=lambda: self._state.save_dirty(self._kv_save),
                on_initiate_sent=self._on_initiate_sent,
                text_transform=self._strip_initiate_text,
                watch_filter=self._filter_watch_users,
                ignore_senders_get=self._ignore_senders_snapshot,
            )

            # ── 外部注册动作（依赖就绪后在构造函数末尾统一执行） ──
            self._register_llm_tools()
            self._register_command_handlers()
            self._register_web_api()

            self._warn_empty_mention_conflict()

            logger.info("Iris Memory 整合插件已加载（等待异步初始化）")
        except Exception:
            logger.error(
                "Iris Memory 插件初始化失败，真实错误如下（框架可能将其掩盖为 "
                "'missing 1 required positional argument: config'，请以下方堆栈为准）：",
                exc_info=True,
            )
            raise

    def _warn_empty_mention_conflict(self) -> None:
        """Guide users to disable AstrBot's built-in pure-mention waiter."""
        try:
            if not self.config.get("extras.pure_at_reply.enable", False):
                return
            platform_settings = self.context.get_config().get("platform_settings", {})
            if platform_settings.get("empty_mention_waiting", True):
                logger.warning(
                    "检测到 AstrBot「只 @ 机器人触发等待」已开启：纯 @ 会先被 "
                    "AstrBot 内置处理器拦截，Iris 无法接管。若要让纯 @ 直接使用 "
                    "L1 上下文，请关闭 platform_settings.empty_mention_waiting。"
                )
        except Exception as exc:
            logger.debug(f"检查 AstrBot 纯 @ 配置失败（已忽略）：{exc}")

    async def _prepare_pure_at_request(self, event: AstrMessageEvent) -> bool:
        """Route a pure @ through the standard agent pipeline using existing L1.

        AstrBot rejects a truly empty ProviderRequest before on_llm_request hooks
        run. A whitespace-only transport prompt keeps the request alive through
        that guard; ProviderRequest.assemble_context() drops it before provider
        dispatch, so the model receives no synthetic user content and relies on
        the normal Iris L1 injection.
        """
        if not _is_pure_at_self(event) or not self.component_manager:
            return False

        try:
            if not self.config.get("extras.pure_at_reply.enable", False):
                return False
            cfg = self.context.get_config(umo=event.unified_msg_origin)
            if cfg.get("platform_settings", {}).get("empty_mention_waiting", True):
                return False

            from .iris_memory.platform import get_adapter

            adapter = get_adapter(event)
            buffer = self.component_manager.get_available_component("l1_buffer")
            if not buffer or not buffer.get_context(adapter.get_session_id(event), 1):
                logger.debug("纯 @ 前没有可注入的 L1 上下文，保持 AstrBot 默认空请求行为")
                return False

            conv_mgr = getattr(self.context, "conversation_manager", None)
            if conv_mgr is None:
                logger.warning("纯 @ 接管失败：AstrBot conversation_manager 不可用")
                return False

            umo = event.unified_msg_origin
            conversation_id = await conv_mgr.get_curr_conversation_id(umo)
            if not conversation_id:
                conversation_id = await conv_mgr.new_conversation(
                    umo, platform_id=event.get_platform_id()
                )
            conversation = await conv_mgr.get_conversation(umo, conversation_id)
            if conversation is None:
                logger.warning("纯 @ 接管失败：无法取得标准会话")
                return False

            request = event.request_llm(
                prompt=" ",
                session_id=conversation_id,
                contexts=[],
                system_prompt="",
                conversation=conversation,
            )
            event.set_extra("provider_request", request)
            event.set_extra("iris_pure_at", True)
            logger.info("已接管纯 @ 消息，将通过标准管道使用现有 L1 上下文")
            return True
        except Exception as exc:
            logger.error(f"纯 @ 消息接管失败，已回退 AstrBot 默认流程：{exc}", exc_info=True)
            return False

    # ========================================================================
    # 记忆侧注册
    # ========================================================================

    def _register_llm_tools(self) -> None:
        """统一注册全部 LLM Tool"""
        try:
            self._registered_llm_tools = register_llm_tools(
                context=self.context,
                owner_module=self.__class__.__module__,
                state=self._state,
                tool_context=self._tool_ctx,
            )
            logger.info(f"已注册 {len(self._registered_llm_tools)} 个 Iris LLM Tool")
        except Exception as e:
            logger.error(f"注册 Iris LLM Tool 失败：{e}", exc_info=True)

    def _register_command_handlers(self) -> None:
        """注册记忆侧指令处理器"""
        try:
            registry = get_registry()
            handlers = [
                L1CommandHandler(),
                L2CommandHandler(),
                L3CommandHandler(),
                ProfileCommandHandler(),
                AllCommandHandler(),
                LearningCommandHandler(),
                EvolutionCommandHandler(),
            ]
            for handler in handlers:
                registry.register(handler)
            logger.info(f"已注册 {len(handlers)} 个记忆指令处理器")
        except Exception as e:
            logger.error(f"注册记忆指令处理器失败：{e}", exc_info=True)

    def _register_web_api(self) -> None:
        try:
            register_all_routes(self.context)
        except Exception as e:
            logger.error(f"注册记忆 Web API 失败：{e}", exc_info=True)

    # ========================================================================
    # 生命周期
    # ========================================================================

    async def initialize(self) -> None:
        # 0. 后台预热 tiktoken 编码器：首次运行需联网下载 BPE 文件，
        #    放线程池执行，预热期间 token 计数临时走字符估算
        self._encoder_warmup_task = asyncio.create_task(warm_up_encoders_async())
        # 让预热协程先设置 pending 标志，再进入其余组件初始化；否则本轮
        # 事件循环中更早发生的 token 计数仍可能同步触发首次下载。
        await asyncio.sleep(0)

        # 1. 记忆组件初始化
        try:
            await initialize_components(self.component_manager)
        except Exception as e:
            logger.error(f"记忆组件初始化失败：{e}", exc_info=True)

        # 2. 旧版（v2.x）数据自动迁移（独立模块，失败不阻断启动）
        if LEGACY_MIGRATION_ENABLED:
            try:
                from .iris_memory.legacy_migration import migrate_if_needed

                await migrate_if_needed(
                    self.context, self, StarTools.get_data_dir(), self.component_manager
                )
            except Exception:
                logger.error("旧数据迁移失败（不影响插件启动）", exc_info=True)

        # 3. 主动回复侧初始化
        await self._state.load_all(self._kv_load)
        umo_data = await self._kv_load(_UMO_KV_KEY)
        if isinstance(umo_data, dict):
            self._group_umo = {str(k): str(v) for k, v in umo_data.items()}
        # 旧版页面配置（KV overrides）一次性迁移到隐藏参数，迁移后清空避免重复覆盖
        config_overrides = await self._kv_load("iris_reply:config_overrides")
        migrated = ReplyConfigManager.legacy_overrides_to_hidden(config_overrides)
        if migrated:
            self.config.update_hidden(migrated)
            await self._kv_save("iris_reply:config_overrides", {})
            logger.info(f"已将 {len(migrated)} 项旧版主动回复页面配置迁移至隐藏参数")
        self._save_task = asyncio.create_task(self._periodic_save())
        self._stats.enabled = self._reply_config.stats_enabled
        register_reply_web_apis(
            context=self.context,
            plugin_name=PLUGIN_NAME,
            state=self._state,
            stats=self._stats,
            window=self._sliding_window,
            kv_save=self._kv_save,
        )
        await self._proactive.start()

        # 4. 功能重叠插件检测（重复注入/门控警告）
        for other in ("astrbot_plugin_iris_chat_memory", "astrbot_plugin_iris_reply"):
            try:
                if self.context.get_registered_star(other):
                    logger.warning(
                        f"检测到插件 {other} 已安装，与本插件功能重叠，"
                        "建议停用其一，避免记忆重复注入与主动回复重复门控"
                    )
            except Exception:
                pass

        logger.info("Iris Memory 整合插件异步初始化完成")

    async def terminate(self):
        """插件卸载清理"""
        logger.info("开始关闭插件组件...")
        warmup = getattr(self, "_encoder_warmup_task", None)
        if warmup and not warmup.done():
            warmup.cancel()
            try:
                await warmup
            except asyncio.CancelledError:
                pass
        # 主动回复侧
        await self._proactive.stop()
        if self._save_task and not self._save_task.done():
            self._save_task.cancel()
            try:
                await self._save_task
            except asyncio.CancelledError:
                pass
        await self._state.save_all(self._kv_save)
        await self._kv_save(_UMO_KV_KEY, dict(self._group_umo))
        self._follow_pending.clear()
        self._reply_in_progress.clear()
        self._passive_active.clear()
        self._triggering.clear()
        self._decision_tickets.clear()
        self._passive_watch_last.clear()
        self._passive_watch_hash.clear()
        # 记忆侧
        await shutdown_components(self.component_manager)
        logger.info("Iris Memory 整合插件已卸载")

    # ========================================================================
    # 主动回复侧：状态保存与互斥
    # ========================================================================

    async def _periodic_save(self) -> None:
        while True:
            await asyncio.sleep(self._save_interval)
            try:
                await self._state.save_dirty(self._kv_save)
                if self._umo_dirty:
                    self._umo_dirty = False
                    await self._kv_save(_UMO_KV_KEY, dict(self._group_umo))
                self._sliding_window.cleanup(self._state.get_whitelist())
                self._cleanup_stale_active()
                sync_stats_group_state(self._state, self._stats)
            except Exception as e:
                logger.warning(f"Iris Reply: periodic save error: {e}")

    def _cleanup_stale_active(self) -> None:
        now = time.time()
        stale_rip = [gid for gid, ts in self._reply_in_progress.items() if now - ts > _IRIS_ACTIVE_TIMEOUT]
        for gid in stale_rip:
            logger.info(f"Iris Reply: cleaning up stale reply_in_progress for group {gid} (timeout)")
            self._reply_in_progress.pop(gid, None)
        stale_passive = [gid for gid, ts in self._passive_active.items() if now - ts > _IRIS_ACTIVE_TIMEOUT]
        for gid in stale_passive:
            logger.info(f"Iris Reply: cleaning up stale passive for group {gid} (timeout)")
            self._passive_active.pop(gid, None)
        stale_triggering = [gid for gid, ts in self._triggering.items()
                            if gid not in self._reply_in_progress and now - ts > _IRIS_ACTIVE_TIMEOUT]
        for gid in stale_triggering:
            logger.info(f"Iris Reply: cleaning up stale triggering for group {gid}")
            self._triggering.pop(gid, None)
        registry = getattr(self, "_decision_tickets", None)
        if registry is not None:
            for ticket in registry.cleanup(now):
                logger.info(
                    "Iris Reply: cleaning up stale decision ticket "
                    f"for group {ticket.group_id}, event={ticket.event_id}"
                )
                if ticket.group_id not in self._reply_in_progress:
                    self._triggering.pop(ticket.group_id, None)

    @staticmethod
    def _decision_event_id(event: AstrMessageEvent) -> str:
        """取得平台消息 ID；缺失时退化为当前事件对象 ID。"""
        message_obj = getattr(event, "message_obj", None)
        raw_id = getattr(message_obj, "message_id", None) if message_obj else None
        if raw_id not in (None, ""):
            return f"message:{raw_id}"
        return f"event:{id(event)}"

    def _release_decision_ticket(self, group_id: str, ticket_id: str = "") -> None:
        """仅由 ticket 所有者释放，避免迟到事件清掉新事件的决策状态。"""
        registry = getattr(self, "_decision_tickets", None)
        if registry is None:
            self._triggering.pop(group_id, None)
            return
        active = registry.get(group_id)
        if active is not None and ticket_id and active.ticket_id != ticket_id:
            return
        registry.release(group_id, ticket_id)
        self._triggering.pop(group_id, None)

    def _is_busy(self, group_id: str) -> bool:
        return (
            group_id in self._reply_in_progress
            or group_id in self._triggering
            or group_id in self._passive_active
        )

    def _is_ignored_sender(self, sender_id: Any, self_id: Any = "") -> bool:
        """主动回复侧的发送者忽略判定（对旧测试实例保持兼容）。

        ``object.__new__`` 构造的兼容测试实例没有 ``_sender_gate``；
        此时退化为「不忽略」，不改变任何既有行为。
        """
        gate = getattr(self, "_sender_gate", None)
        if gate is None:
            return False
        try:
            return bool(gate.is_ignored(sender_id, self_id=str(self_id or "")))
        except Exception as exc:
            logger.debug(f"Iris Reply: 忽略名单判定异常（已放行）：{exc}")
            return False

    def _filter_watch_users(self, users: Any) -> list[str]:
        """移除决策模型给出的、属于忽略名单的关注用户。

        决策 prompt 里能看到被忽略账号的历史消息（滑窗早期残留），模型可能
        把它们写进 watch。若不过滤，下一次发言就会命中
        ``match_anchor_user`` → follow_up，等于从决策侧绕过忽略名单。
        """
        if not users:
            return []
        result: list[str] = []
        for uid in users:
            sid = str(uid or "").strip()
            if not sid or sid in result:
                continue
            # 模型常把字面量 "None"/"无" 写进 watch；这类值永远匹配不到真人，
            # 却会一直挂在锚点上，每轮都参与匹配，直接丢弃。
            # 注意不要求「必须是纯数字」：非 OneBot 平台（Lark/WebChat 等）
            # 的 sender_id 不是数字。
            if sid.lower() in _WATCH_JUNK_VALUES:
                logger.debug(f"Iris Reply: watch user {sid!r} dropped (placeholder)")
                continue
            # 传 self_id：否则 bot 自己的 ID 会被从 watch 里剔掉
            if self._is_ignored_sender(sid, getattr(self, "_self_id", "")):
                logger.debug(f"Iris Reply: watch user {sid} dropped (ignored sender)")
                continue
            result.append(sid)
        return result

    def _ignore_senders_snapshot(self) -> set[str]:
        """当前生效的忽略发送者集合（供 prompt 打码 / initiate 准入）。

        统一在这里剔除自身 ID：``is_ignored``、``package``、``purge_senders``、
        ``_filter_watch_users``、``_has_substantive`` 五处都消费本方法的结果，
        一处过滤即可保证语义一致。

        否则用户一旦手滑把 bot 自己的 ID 写进名单：
        - bot 发起话题后窗口只剩自己那行 → ``_has_substantive`` 为假
          → 该群被无限重排期、主动发起永久静默（且无任何日志）
        - bot 的历史发言会被打码，上下文断裂
        """
        gate = getattr(self, "_sender_gate", None)
        if gate is None:
            return set()
        try:
            ids = set(gate.snapshot())
        except Exception as exc:
            logger.debug(f"Iris Reply: 忽略名单快照失败（按空处理）：{exc}")
            return set()
        self_id = str(getattr(self, "_self_id", "") or "")
        if self_id:
            ids.discard(self_id)
        return ids

    def _ignore_external_snapshot(self) -> set[str]:
        """从外部黑名单同步来的 ID（供 Web 页面区分来源，旧测试实例返回空集）。"""
        gate = getattr(self, "_sender_gate", None)
        if gate is None:
            return set()
        try:
            return gate.external_snapshot()
        except Exception as exc:
            logger.debug(f"Iris Reply: 外部同步集合获取失败（按空处理）：{exc}")
            return set()

    def _on_ignore_list_changed(self, ids: set[str]) -> None:
        """忽略名单变化后的就地清理（滑窗 + 各群锚点）。

        在读取配置的同步路径上被调用，因此只做内存操作，
        持久化交给既有的周期保存任务。
        """
        window = getattr(self, "_sliding_window", None)
        state = getattr(self, "_state", None)
        if window is None or state is None:
            return

        # 名单被清空时无需清理：purge_senders 传空集合会变成「删掉所有发送者
        # 的消息」，而 remove_anchor_watch 在 user_ids 为空时会走
        # ``anchor.clear()`` 分支，把每个群的跟进锚点整体清空。
        # 那是「名单变空」之外的语义，必须提前返回。
        if not ids:
            return

        # 自身 ID 全程豁免：purge / 锚点 / is_ignored / package 语义必须一致。
        # _self_id 为空（尚未收到过消息）时无法判断哪个是自身，此时保守跳过
        # 清理，等下一次名单变更或首条消息之后再对齐。
        self_id = str(getattr(self, "_self_id", "") or "")
        targets = set(ids)
        if not self_id:
            logger.debug(
                "Iris Reply: 自身 ID 尚未知悉，跳过本次忽略名单清理以免误伤"
            )
            return
        targets.discard(self_id)
        if not targets:
            return

        try:
            removed = window.purge_senders(
                set(), targets, protected_ids={self_id}
            )
        except Exception as exc:
            logger.debug(f"Iris Reply: 忽略名单清理滑窗失败（已忽略）：{exc}")
            removed = 0
        try:
            for gid in state.get_whitelist():
                # 显式传非空 user_ids，避免命中 remove_anchor_watch 的
                # 「两者都为空则整体 clear」分支。
                state.remove_anchor_watch(gid, user_ids=sorted(targets))
        except Exception as exc:
            logger.debug(f"Iris Reply: 忽略名单清理锚点失败（已忽略）：{exc}")
        if removed:
            logger.info(f"Iris Reply: 忽略名单生效，已从滑动窗口移除 {removed} 条历史消息")

    async def _kv_save(self, key: str, value: Any) -> None:
        await self.put_kv_data(key, value)

    async def _kv_load(self, key: str) -> Any:
        return await self.get_kv_data(key, None)

    def _get_group_id(self, event) -> str | None:
        group_id = event.get_group_id()
        if not group_id:
            event.set_result("无法获取群ID")
            return None
        return group_id

    async def _get_provider_id(self, event, preferred: str = "") -> str | None:
        if preferred:
            return preferred
        try:
            return await self.context.get_current_chat_provider_id(
                event.unified_msg_origin
            )
        except Exception:
            logger.error("Iris Reply: failed to get provider ID")
            return None

    def _strip_initiate_text(self, text: str) -> str:
        """initiate 直发消息的 Markdown 去除

        直发通路（context.send_message）不触发 on_decorating_result 钩子，
        消息始终以纯文本发送到平台，此处补齐与管线消息一致的 Markdown 去除，
        避免同群内跟话消息与主动发起消息格式处理不一致。
        """
        stripper = self._markdown_stripper
        if not stripper or not text:
            return text
        try:
            if not stripper.should_strip(text, use_t2i=False):
                return text
            return stripper.strip(text)
        except Exception as e:
            logger.warning(f"initiate 消息 Markdown 去除失败：{e}")
            return text

    async def _on_initiate_sent(self, group_id: str, text: str) -> None:
        """initiate 直发成功后，把 bot 发言回填进 L1 缓冲

        直发通路（context.send_message）不触发任何事件钩子，
        若不回填，L1 上下文中将看不到这类发起消息。
        """
        if not self.component_manager or not text:
            return
        try:
            await handle_initiate_backfill(group_id, text, self.component_manager)
        except Exception as e:
            logger.warning(f"initiate 消息回填 L1 失败：{e}")

    # ========================================================================
    # 主动回复侧：LLM 工具（已迁移至 iris_memory/tools/proactive.py，
    # 由 _register_llm_tools() 统一注册）
    # ========================================================================

    # ========================================================================
    # 主动回复侧：管理指令
    # ========================================================================

    @filter.command_group("iris_reply")
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    def iris_reply_group(self):
        pass

    @iris_reply_group.command("enable")
    async def cmd_enable(self, event) -> None:
        group_id = self._get_group_id(event)
        if not group_id:
            return
        self._state.add_to_whitelist(group_id)
        await self._state.save_dirty(self._kv_save)
        event.set_result(f"群 {group_id} 已启用主动回复")

    @iris_reply_group.command("disable")
    async def cmd_disable(self, event) -> None:
        group_id = self._get_group_id(event)
        if not group_id:
            return
        self._state.remove_from_whitelist(group_id)
        self._sliding_window.remove_group(group_id)
        self._state.remove_group_lock(group_id)
        await self._state.save_dirty(self._kv_save)
        event.set_result(f"群 {group_id} 已禁用主动回复")

    @iris_reply_group.command("status")
    async def cmd_status(self, event) -> None:
        group_id = self._get_group_id(event)
        if not group_id:
            return
        text = self._admin.get_status(group_id)
        event.set_result(text)

    @iris_reply_group.command("reset")
    async def cmd_reset(self, event) -> None:
        group_id = self._get_group_id(event)
        if not group_id:
            return
        msg = self._admin.reset_group(group_id)
        self._sliding_window.remove_group(group_id)
        await self._state.save_dirty(self._kv_save)
        event.set_result(msg)

    @iris_reply_group.command("cooldown")
    async def cmd_cooldown(self, event, minutes: int = 5) -> None:
        group_id = self._get_group_id(event)
        if not group_id:
            return
        msg = self._admin.set_cooldown(group_id, minutes)
        await self._state.save_dirty(self._kv_save)
        event.set_result(msg)

    @iris_reply_group.command("willingness")
    async def cmd_willingness(self, event, level: str = "") -> None:
        group_id = self._get_group_id(event)
        if not group_id:
            return
        if not level.strip():
            current = self._admin.get_willingness(group_id)
            event.set_result(f"群 {group_id} 当前回复意愿: {current}\n可选: 低/中/高 (low/medium/high)")
            return
        msg = self._admin.set_willingness(group_id, level.strip())
        await self._state.save_dirty(self._kv_save)
        event.set_result(msg)

    @iris_reply_group.command("initiate")
    async def cmd_initiate(self, event) -> None:
        group_id = self._get_group_id(event)
        if not group_id:
            return
        result = await self._proactive.attempt_initiate(group_id, force=True)
        event.set_result(f"主动发起: {result}")

    @iris_reply_group.command("ignore")
    async def cmd_ignore(self, event, action: str = "list", target_id: str = "") -> None:
        """查看/调整主动回复的发送者忽略名单（管理员）。

        用法：/iris_reply ignore list | add <ID> | remove <ID> | sync
        """
        current = self._reply_config.ignore_sender_ids
        action = (action or "list").strip().lower()

        if action == "list":
            effective = self._sender_gate.snapshot()
            ext = "开启" if self._reply_config.ignore_sync_external else "关闭"
            event.set_result(
                f"主动回复忽略名单（{len(effective)} 个）\n"
                f"生效: {', '.join(sorted(effective)) or '无'}\n"
                f"手写: {current or '无'}\n"
                f"同步外部黑名单: {ext}"
            )
            return

        if action == "sync":
            self._sender_gate.refresh(force=True)
            effective = self._sender_gate.snapshot()
            event.set_result(
                f"已刷新，当前生效 {len(effective)} 个: "
                f"{', '.join(sorted(effective)) or '无'}"
            )
            return

        if action not in ("add", "remove"):
            event.set_result("用法：/iris_reply ignore list|add <ID>|remove <ID>|sync")
            return

        ids = extract_ids([target_id])
        if not ids:
            raw = str(target_id or "").strip()
            # 有内容但解析不出 ID：含字母/下划线说明是非数字 ID
            # （Lark 的 ou_xxx、WebChat 的 username），明确告知，
            # 避免用户以为配上了却毫无效果。
            has_alpha = any(ch.isalpha() or ch == "_" for ch in raw)
            if raw and has_alpha:
                event.set_result(
                    f"「{raw}」不是数字 ID，当前忽略名单只支持数字 ID"
                    "（OneBot/QQ 等平台）。该平台的发送者暂无法加入忽略名单。"
                )
            else:
                event.set_result(
                    "请提供有效的用户 ID，例如：/iris_reply ignore add 100000005"
                )
            return
        if len(ids) > 1:
            # 一条命令写多个 ID 时无法判断「加哪一个」，静默取第一个会让
            # 用户以为全加了；更糟的是 remove 会连带删掉其他 ID。
            event.set_result(
                "请一次只提供一个用户 ID，例如：/iris_reply ignore add 100000005"
            )
            return
        target = next(iter(ids))

        existing = _split_id_list(current)
        # 手写名单里可能混着 "@昵称(ID)" / "qq:FriendMessage:ID" 等写法，
        # 比较时统一归一化成纯 ID，避免出现「同一个人写两遍」。
        # 注意：一个条目里可能含多个 ID（"123/456"），必须用集合判断归属，
        # 不能用 next(iter(...)) 取单个值 —— set 的迭代顺序不确定。
        def _entry_ids(item: str) -> set[str]:
            parsed = extract_ids([item])
            return parsed or {item.strip()}

        existing_ids = {i for item in existing for i in _entry_ids(item)}
        if action == "add":
            if target in existing_ids:
                event.set_result(f"{target} 已在忽略名单中")
                return
            existing.append(target)
        elif action == "remove":
            if target not in existing_ids:
                event.set_result(
                    f"{target} 不在手写忽略名单中（可能来自外部黑名单同步）"
                )
                return
            kept: list[str] = []
            for item in existing:
                entry = _entry_ids(item)
                if target not in entry:
                    kept.append(item)
                    continue
                # 条目里还有别的 ID：保留它们，不能整条删掉
                rest = entry - {target}
                if rest:
                    kept.append(",".join(sorted(rest)))
            existing = kept

        self.config.set_hidden("reply_ignore_sender_ids", ",".join(existing))
        self._sender_gate.refresh(force=True)
        await self._state.save_dirty(self._kv_save)
        event.set_result(
            f"已{'加入' if action == 'add' else '移出'}忽略名单：{target}\n"
            f"当前生效 {len(self._sender_gate.snapshot())} 个"
        )

    # ========================================================================
    # AstrBot 钩子
    # ========================================================================

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_message(self, event) -> None:
        """主动回复消息唤醒：门控 → 标记 → 交由 on_llm_request 决策"""
        if await self._prepare_pure_at_request(event):
            return

        if not self._reply_config.enabled:
            return

        if not self._gatekeeper.should_process(event):
            return

        group_id = event.get_group_id()
        if not group_id:
            return

        # 缓存会话标识与自身 ID，供主动发起通路使用
        umo = getattr(event, "unified_msg_origin", "")
        if umo and self._group_umo.get(group_id) != umo:
            self._group_umo[group_id] = umo
            self._umo_dirty = True
        if not self._self_id:
            self._self_id = event.get_self_id() or ""

        message_str = event.message_str or ""
        sender_id = event.get_sender_id()
        sender_name = event.get_sender_name() or sender_id

        # 发送者级忽略：被忽略账号的普通群消息不进入滑动窗口、不触发插话/
        # 跟进采样、也不推进主动发起计时。@ 唤醒 / 唤醒前缀的被动回复不受
        # 影响（那条通路要正常应答）。这是补在「完全禁用」类黑名单插件之外
        # 的一层，因为那类插件只在 is_at_or_wake_command 为真时拦截，
        # 而 Iris 的插话采样读的正是普通群消息。
        self_id_str = event.get_self_id() or self._self_id
        ignored_sender = self._is_ignored_sender(sender_id, self_id_str)

        # 被忽略的发送者：普通发言整条跳过（不入窗、不采样、不建立跟进锚点），
        # 但直接 @ / 唤醒前缀的被动回复仍然照常处理 —— 用户要拦的是「主动回复」，
        # 不是「被点名也不理」。
        passive_wake = bool(event.is_at_or_wake_command)
        if ignored_sender and not passive_wake:
            # 被忽略账号的发言同样算「有人接话」：消费掉 pending，
            # 否则 ProactiveEngine 超时后会记成「发起无人接话」，
            # streak++ → fatigue = 0.4**n，把该群的主动发起系统性压低。
            if self._state.consume_initiate_pending(group_id):
                await self._state.save_dirty(self._kv_save)
            # 仍告知主动发起计时「群里有动静」：否则被忽略账号正在刷屏时
            # 会被判成冷场，Iris 反而开口发起话题。
            self._proactive.notify_human_message(group_id, time.time())
            logger.debug(
                f"Iris Reply: ignored sender {sender_id} in group {group_id}, "
                "window/sampling skipped"
            )
            return

        # 发起后的首次接话：清除 pending，该消息直接获得一次跟进评估资格
        pending_reply = self._state.consume_initiate_pending(group_id)
        is_followed = bool(sender_id and self._state.match_anchor_user(group_id, sender_id))

        score = self._gatekeeper.quality_score(message_str)
        if score < self._reply_config.quality_threshold and not is_followed and not pending_reply:
            return

        message_timestamp = time.time()
        self._sliding_window.append(
            group_id,
            WindowMessage(
                sender_id=sender_id,
                sender_name=sender_name,
                content=message_str,
                timestamp=message_timestamp,
            ),
        )
        # 每条真实群消息都会使旧主动候选失效，并为该群独立重新预约。
        self._proactive.notify_human_message(group_id, message_timestamp)

        if passive_wake:
            self._release_decision_ticket(group_id)
            self._state.increment_msg_count(group_id)
            self._state.open_wake_window(group_id, WAKE_WINDOW_SECONDS)
            self._passive_active[group_id] = time.time()
            event.set_extra("iris_mode", "passive")
            return

        if self._is_busy(group_id) or self._proactive.is_initiating(group_id):
            logger.debug(f"Iris Reply: reply already in progress for group {group_id}")
            return

        async with self._state.get_lock(group_id):
            motive = self._signals.evaluate_message(group_id, sender_id, message_str)

        if not motive and pending_reply:
            motive = "follow_up"
        if not motive:
            return

        is_follow_up = motive == "follow_up"

        if is_follow_up:
            if group_id in self._follow_pending:
                logger.debug(f"Iris Reply: follow-up aggregation pending for group {group_id}")
                return
            self._follow_pending.add(group_id)
            try:
                # @ 唤醒窗口内话题正围绕 bot，不排队聚合，只留极短间隔避免连发
                if self._state.in_wake_window(group_id):
                    await asyncio.sleep(1)
                else:
                    await asyncio.sleep(self._reply_config.follow_up_aggregate_window)
            finally:
                self._follow_pending.discard(group_id)

            if self._is_busy(group_id):
                return
            if not pending_reply and not self._state.get_anchor(group_id).active:
                return

        if not self._state.can_detect(group_id, follow_up=is_follow_up):
            logger.debug(f"Iris Reply: trigger rate-limited for group {group_id}")
            return

        provider_id = self._reply_config.provider_id
        if not provider_id:
            provider_id = await self._get_provider_id(event)
            if not provider_id:
                logger.error(f"Iris Reply: failed to get provider ID for group {group_id}")
                return

        async with self._state.get_lock(group_id):
            if group_id in self._triggering:
                logger.debug(f"Iris Reply: trigger already in progress for group {group_id}")
                return
            event_id = self._decision_event_id(event)
            registry = getattr(self, "_decision_tickets", None)
            ticket = registry.claim(group_id, event_id) if registry else None
            if registry is not None and ticket is None:
                logger.debug(
                    f"Iris Reply: duplicate decision event rejected for group {group_id}, "
                    f"event={event_id}"
                )
                return
            self._state.record_detect_time(group_id)
            self._triggering[group_id] = time.time()

        event.set_extra("iris_decision", {
            "motive": motive,
            "provider_id": provider_id,
            "event_id": event_id,
            "ticket_id": ticket.ticket_id if ticket else "",
        })

        event.is_at_or_wake_command = True
        event.is_wake = True
        if provider_id:
            event.set_extra("selected_provider", provider_id)
        self._tool_ctx.set_context(group_id)
        logger.info(
            f"Iris Reply: {motive} candidate activated for group {group_id}, deferred to on_llm_request"
        )

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_all_message(self, event: AstrMessageEvent) -> None:
        """记忆侧：全类型消息入 L1 缓冲、图片入队"""
        if self.component_manager:
            await handle_user_message(event, self.component_manager)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("iris_mem")
    async def iris_mem(self, event: AstrMessageEvent) -> None:
        if self.component_manager:
            result = await execute_command(event)
            if result:
                yield event.plain_result(result)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("memory")
    async def memory_legacy_alias(self, event: AstrMessageEvent) -> None:
        """旧版 /memory 指令指引别名

        v2 插件的 /memory 指令在本插件中已更名为 /iris_mem。旧指令输入
        此前不会被任何处理器响应（无反馈、无删除），迁移用户容易误以为
        清除已生效。此处仅返回迁移指引，不执行任何操作。
        """
        yield event.plain_result(
            "ℹ️ /memory 指令已迁移到 /iris_mem\n"
            "按层清除：/iris_mem l1|l2|l3 clear [--group|--all|@用户]\n"
            "全部清除：/iris_mem all clear [--group|--all]\n"
            "完整用法：/iris_mem help"
        )

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        # 1. 主动回复统一决策（仅处理 _triggering 中的群；可能 stop_event 终止请求）
        if await self._handle_reply_decision(event):
            return

        # 2. 记忆侧：被动触发检测 + 上下文接管 + L1/L2/L3/画像注入
        if self.component_manager:
            _detect_passive_trigger(event, req, self.context)
            await handle_pre_request_cleanup(
                event, req, self.context, self.component_manager
            )
            await preprocess_llm_request(event, req, self.component_manager)

        # 3. 主动回复发言提示：决策通过后由 _handle_reply_decision 暂存，
        # 在记忆注入之后追加，保证 LLM 先看到上下文、再看到发言指令
        hint = event.get_extra("iris_speak_hint")
        if hint:
            req.extra_user_content_parts.append(TextPart(text=hint).mark_as_temp())

        # 所有 AstrBot 主管线请求也必须取得 Governor lease。此处刻意位于
        # 查询改写/图片关联解析等预处理之后，避免单并发配置下嵌套申请死锁。
        mode = event.get_extra("iris_mode")
        if self._llm_manager:
            provider_id = event.get_extra("iris_llm_provider_id") or ""
            if not provider_id:
                provider_id = await self._get_provider_id(event) or ""
            module = (
                proactive_reply_module(mode)
                if mode in ("chime_in", "follow_up", "passive")
                else FRAMEWORK_REPLY
            )
            lease = await self._llm_manager.acquire_framework_lease(
                module=module,
                provider_id=provider_id,
            )
            try:
                # 先把 lease ID 写入事件，确保后续任一步骤抛错时都能在
                # 当前栈内释放，而不必等待 watchdog 回收。
                event.set_extra("iris_llm_lease_id", lease.lease_id)
                await self._llm_manager.record_framework_attempt(module)
                event.set_extra(
                    "iris_llm_tracking",
                    {
                        "module": module,
                        "provider_id": provider_id,
                        "started_at": time.time(),
                        "prompt": getattr(req, "prompt", "") or "",
                        "priority": lease.priority.name,
                        "queue_wait_ms": lease.queue_wait_ms,
                        "in_flight_at_start": lease.in_flight_at_start,
                        "queue_depth_at_start": lease.queue_depth_at_start,
                    },
                )
            except Exception:
                event.set_extra("iris_llm_lease_id", None)
                await self._llm_manager.release_framework_lease(
                    lease.lease_id,
                    provider_id=provider_id,
                    success=False,
                )
                raise

    async def _handle_reply_decision(self, event: AstrMessageEvent) -> bool:
        """主动回复统一决策执行点。

        Returns:
            True 表示已调用 event.stop_event()，调用方应立即返回，
            不再进行记忆注入。
        """
        group_id = event.get_group_id()
        if not group_id or group_id not in self._triggering:
            return False

        info = event.get_extra("iris_decision")
        if not info:
            self._release_decision_ticket(group_id)
            return False

        motive = info.get("motive", "")
        provider_id = info.get("provider_id", "")
        ticket_id = str(info.get("ticket_id") or "")
        event_id = str(info.get("event_id") or "")
        registry = getattr(self, "_decision_tickets", None)
        if registry is not None:
            active = registry.get(group_id)
            # 旧测试/升级中的遗留事件没有 ticket_id，继续走兼容路径；生产事件
            # 一旦携带 ticket，就必须同时匹配 ticket_id + event_id。
            if ticket_id and not registry.owns(group_id, ticket_id, event_id):
                logger.warning(
                    f"Iris Reply: stale decision ticket rejected for group {group_id}, "
                    f"event={event_id}"
                )
                event.stop_event()
                return True
            if active is not None and not ticket_id:
                logger.warning(
                    f"Iris Reply: decision event without owner ticket rejected for group {group_id}"
                )
                event.stop_event()
                return True

        req = DecisionRequest(
            group_id=group_id,
            wake="message",
            motive=motive,
            wake_window=self._state.in_wake_window(group_id),
        )
        outcome = await self._decision_core.decide(req, self._llm_manager, provider_id)

        if outcome.error or outcome.decision is None:
            if outcome.error_kind == "input_content_safety_1026":
                cleanup, cooldown = await self._clear_rejected_reply_context(
                    group_id,
                    outcome.dynamic_context_sources,
                    record_skip=True,
                )
                logger.error(
                    "Iris Reply: decision input rejected by provider safety filter "
                    f"for group {group_id} (1026, retryable=false, "
                    f"dynamic_sources={cleanup.dynamic_source_count}, "
                    f"window_removed={cleanup.window_removed}, "
                    f"observation_cleared={cleanup.observation_cleared}, "
                    f"anchor_cleared={cleanup.anchor_cleared}, "
                    f"cooldown={cooldown}min): "
                    f"{outcome.error}"
                )
            else:
                logger.error(f"Iris Reply: decision LLM call failed for group {group_id}: {outcome.error}")
                async with self._state.get_lock(group_id):
                    self._state.record_skip_reply(group_id)
                await self._state.save_dirty(self._kv_save)
            self._stats.record_decision_error(group_id, motive)
            self._release_decision_ticket(group_id, ticket_id)
            event.stop_event()
            return True

        decision = outcome.decision
        logger.info(
            f"Iris Reply: decision raw for group {group_id} (motive={motive}, "
            f"len={len(outcome.raw_text)}): {outcome.raw_text:.500s}"
        )
        self._stats.record_decision(
            group_id, motive,
            system_prompt=outcome.system_prompt,
            user_prompt=outcome.user_prompt,
            response_text=outcome.raw_text,
            decision=decision,
            duration_ms=outcome.duration_ms,
        )
        logger.info(
            f"Iris Reply: decision parsed for group {group_id}: speak={decision.should_speak}, "
            f"drifted={decision.drifted}, watch={decision.watch}, "
            f"watch_keywords={decision.watch_keywords}, cooldown={decision.cooldown_minutes}"
        )

        async with self._state.get_lock(group_id):
            if decision.observation:
                self._state.set_observation(group_id, decision.observation)

        if decision.parse_failed:
            logger.warning(f"Iris Reply: decision parse failed for group {group_id}")
            async with self._state.get_lock(group_id):
                self._state.record_skip_reply(group_id)
            await self._state.save_dirty(self._kv_save)
            self._release_decision_ticket(group_id, ticket_id)
            event.stop_event()
            return True

        if group_id in self._passive_active:
            logger.info(f"Iris Reply: aborting {motive} for group {group_id}, passive reply in progress")
            async with self._state.get_lock(group_id):
                self._state.record_skip_reply(group_id)
            await self._state.save_dirty(self._kv_save)
            self._release_decision_ticket(group_id, ticket_id)
            event.stop_event()
            return True

        if decision.cooldown_minutes:
            async with self._state.get_lock(group_id):
                actual = self._state.set_cooldown(group_id, decision.cooldown_minutes)
                self._state.record_skip_reply(group_id)
            await self._state.save_dirty(self._kv_save)
            logger.info(f"Iris Reply: decision requested cooldown {actual} min for group {group_id}")
            self._release_decision_ticket(group_id, ticket_id)
            event.stop_event()
            return True

        if decision.drifted:
            async with self._state.get_lock(group_id):
                self._state.close_anchor(group_id)
                self._state.record_drift(group_id)
            await self._state.save_dirty(self._kv_save)
            logger.info(f"Iris Reply: topic drifted for group {group_id}, anchor closed")
            self._release_decision_ticket(group_id, ticket_id)
            event.stop_event()
            return True

        # 决策模型可能把忽略名单里的账号写进 watch；这里统一清洗一次，
        # 保证「被忽略的账号不会成为关注对象」在决策侧也成立。
        filtered_watch = self._filter_watch_users(decision.watch)
        decision.watch = filtered_watch
        if filtered_watch or decision.watch_keywords:
            if decision.should_speak:
                event.set_extra("iris_pending_watch", (
                    decision.watch, decision.watch_keywords, decision.why,
                ))
            else:
                async with self._state.get_lock(group_id):
                    self._state.add_anchor_watch(
                        group_id,
                        users=decision.watch or None,
                        keywords=decision.watch_keywords or None,
                        reason=decision.why,
                    )
            logger.info(
                f"Iris Reply: decision watch for group {group_id}, users={decision.watch}, "
                f"keywords={decision.watch_keywords}, reason={decision.why} (speak={decision.should_speak})"
            )

        if not decision.should_speak:
            async with self._state.get_lock(group_id):
                self._state.record_skip_reply(group_id)
            await self._state.save_dirty(self._kv_save)
            logger.debug(f"Iris Reply: decision skip for group {group_id}")
            self._release_decision_ticket(group_id, ticket_id)
            event.stop_event()
            return True

        self._reply_in_progress[group_id] = time.time()
        self._release_decision_ticket(group_id, ticket_id)

        event.set_extra("iris_mode", motive)
        event.set_extra("iris_llm_provider_id", provider_id)
        event.set_extra("iris_decision_obs", decision.observation)

        hint = SPEAK_HINTS.get(motive, SPEAK_HINTS["chime_in"])
        # 发言提示延迟到记忆注入之后追加（见 on_llm_request），
        # 保持「上下文在前、指令在后」的提示顺序
        event.set_extra("iris_speak_hint", hint)
        logger.info(f"Iris Reply: decision speak ({motive}) for group {group_id}")
        return False

    async def _clear_rejected_reply_context(
        self,
        group_id: str,
        dynamic_context_sources: list[dict[str, Any]] | None,
        *,
        record_skip: bool,
    ) -> tuple[SafetyCleanupResult, int]:
        """隔离一次被 Provider 安全过滤拒绝的主动回复动态上下文。"""
        async with self._state.get_lock(group_id):
            cleanup = self._decision_core.clear_rejected_dynamic_context(
                group_id,
                dynamic_context_sources,
            )
            if record_skip:
                self._state.record_skip_reply(group_id)
            cooldown = self._state.set_cooldown(
                group_id,
                INPUT_SAFETY_COOLDOWN_MINUTES,
            )
        await self._state.save_dirty(self._kv_save)
        return cleanup, cooldown

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, resp: LLMResponse) -> None:
        tracking = event.get_extra("iris_llm_tracking")
        lease_id = event.get_extra("iris_llm_lease_id")
        if lease_id and self._llm_manager:
            try:
                await self._llm_manager.release_framework_lease(
                    lease_id,
                    provider_id=(tracking or {}).get("provider_id", ""),
                    success=True,
                )
            except Exception as e:
                # 响应已经产生，租约统计故障不应中断消息回填；Governor
                # watchdog 仍会兜底回收尚未释放的 framework lease。
                logger.warning(f"Iris Reply: 主管线 lease 释放失败：{e}")
            finally:
                event.set_extra("iris_llm_lease_id", None)

        if tracking and self._llm_manager:
            try:
                await self._llm_manager.record_framework_response(
                    module=tracking["module"],
                    provider_id=tracking.get("provider_id", ""),
                    response=resp,
                    started_at=tracking.get("started_at"),
                    prompt=tracking.get("prompt", ""),
                    priority=tracking.get("priority", "INTERACTIVE"),
                    queue_wait_ms=tracking.get("queue_wait_ms", 0),
                    in_flight_at_start=tracking.get("in_flight_at_start", 0),
                    queue_depth_at_start=tracking.get("queue_depth_at_start", 0),
                )
                event.set_extra("iris_llm_tracking", None)
            except Exception as e:
                logger.warning(f"Iris Reply: 主管线 LLM 统计失败：{e}")

        # 1. 记忆侧：bot 回复入 L1
        if self.component_manager:
            await handle_llm_response(event, resp, self.component_manager)
        # 2. 主动回复侧：按 iris_mode 记账
        await self._reply_on_llm_response(event, resp)

    async def _reply_on_llm_response(self, event: AstrMessageEvent, response: LLMResponse) -> None:
        group_id = event.get_group_id()
        if not group_id:
            return

        event.set_extra("iris_llm_replied", True)
        mode = event.get_extra("iris_mode")

        if mode in ("chime_in", "follow_up"):
            self._reply_in_progress.pop(group_id, None)
            self._passive_active.pop(group_id, None)

            async with self._state.get_lock(group_id):
                self._state.record_actual_reply(group_id)

            self._tool_ctx.clear_context()
            await self._state.save_dirty(self._kv_save)
            try:
                get_run_log_manager().record(
                    "proactive",
                    f"{'插话' if mode == 'chime_in' else '跟进'}回复已发送",
                    success=True,
                    group_id=group_id,
                    wake="message",
                    motive=mode,
                    stage="reply",
                    message=(response.completion_text or "").strip(),
                )
            except Exception:
                pass
            logger.info(f"Iris Reply: {mode} reply sent for group {group_id}")
        elif mode == "passive":
            self._passive_active.pop(group_id, None)

            async with self._state.get_lock(group_id):
                self._state.record_actual_reply(group_id, count_consecutive=False)

            self._stats.record_passive_reply(group_id)
            await self._state.save_dirty(self._kv_save)
            logger.info(f"Iris Reply: passive reply boost applied for group {group_id}")
        else:
            if not self._state.is_whitelisted(group_id):
                return
            async with self._state.get_lock(group_id):
                self._state.record_actual_reply(group_id, count_consecutive=False)
            await self._state.save_dirty(self._kv_save)
            logger.info(f"Iris Reply: normal LLM reply for group {group_id}, boost applied")

    @filter.after_message_sent()
    async def on_message_sent(self, event) -> None:
        """主动回复侧：bot 消息入滑动窗口 + 写 ThreadAnchor"""
        group_id = event.get_group_id()
        if not group_id:
            return
        if not self._state.is_whitelisted(group_id):
            return
        sender_id = event.get_sender_id()
        if not sender_id:
            return
        result = event.get_result()
        bot_text = result.get_plain_text().strip() if result else ""
        if bot_text:
            self._sliding_window.append(group_id, WindowMessage(
                sender_id=event.get_self_id() or "iris",
                sender_name="我",
                content=bot_text,
                timestamp=time.time(),
            ))
        # 被忽略的发送者不写关注锚点：否则他的下一条发言会命中
        # match_anchor_user → follow_up → 主动跟进，等于绕开忽略名单。
        # 但机器人自己的发言仍要入窗（上面已追加），保证上下文完整。
        if self._is_ignored_sender(sender_id, event.get_self_id() or self._self_id):
            logger.debug(
                f"Iris Reply: ignored sender {sender_id} in group {group_id}, "
                "anchor skipped"
            )
            return
        mode = event.get_extra("iris_mode")
        if mode in ("chime_in", "follow_up"):
            pending = event.get_extra("iris_pending_watch")
            users = self._filter_watch_users(list(pending[0]) if pending else [])
            if sender_id not in users:
                users.append(sender_id)
            keywords = list(pending[1]) if pending else []
            reason = pending[2] if pending else ""
            topic = event.get_extra("iris_decision_obs", "")
            async with self._state.get_lock(group_id):
                self._state.write_anchor(
                    group_id,
                    kind=mode,
                    topic=topic,
                    bot_message=bot_text,
                    users=users,
                    keywords=keywords or None,
                    reason=reason,
                )
            await self._state.save_dirty(self._kv_save)
            logger.debug(f"Iris Reply: anchor written ({mode}) for group {group_id}")
        elif mode == "passive":
            provider_id = self._reply_config.provider_id or await self._get_provider_id(event)
            if provider_id:
                await self._passive_watch_eval(group_id, provider_id, sender_id, bot_text)
            else:
                async with self._state.get_lock(group_id):
                    self._state.write_anchor(
                        group_id, kind="passive", bot_message=bot_text, users=[sender_id],
                    )
                await self._state.save_dirty(self._kv_save)
        elif event.get_extra("iris_llm_replied"):
            async with self._state.get_lock(group_id):
                self._state.write_anchor(
                    group_id, kind="reply", bot_message=bot_text, users=[sender_id],
                )
            await self._state.save_dirty(self._kv_save)
            logger.debug(f"Iris Reply: anchor written (reply) for group {group_id}")

    async def _passive_watch_eval(
        self, group_id: str, provider_id: str, fallback_sender: str, bot_text: str,
    ) -> None:
        """被动回复后的跟进评估（motive=watch）：只决定是否建立关注锚点。"""
        messages = self._sliding_window.get_messages(group_id)
        if not messages:
            return
        reply_config = getattr(self, "_reply_config", None)
        if reply_config is not None and not getattr(
            reply_config, "passive_watch_enabled", True
        ):
            return

        # object.__new__ 构造的兼容测试实例没有这些映射；正常插件实例始终
        # 存在。门控只影响生产路径，不改变旧对象的错误清理语义。
        watch_last = getattr(self, "_passive_watch_last", None)
        watch_hashes = getattr(self, "_passive_watch_hash", None)
        if isinstance(watch_last, dict) and isinstance(watch_hashes, dict):
            if not bot_text or not _PASSIVE_WATCH_SIGNAL.search(bot_text):
                logger.debug(
                    f"Iris Reply: passive watch local gate skipped group {group_id}"
                )
                return

            now = time.monotonic()
            min_interval = float(self._reply_config.passive_watch_min_interval)
            if now - watch_last.get(group_id, 0.0) < min_interval:
                return

            digest_source = "\n".join(
                [
                    *(str(getattr(item, "content", "")) for item in messages[-10:]),
                    bot_text,
                ]
            )
            digest = hashlib.sha256(digest_source.encode("utf-8")).hexdigest()
            if watch_hashes.get(group_id) == digest:
                return

            if self._llm_manager:
                metrics = self._llm_manager.get_governor_metrics()
                queue_limit = max(1, int(metrics.get("queue_limit", 500)))
                if int(metrics.get("queue_depth", 0)) >= int(queue_limit * 0.7):
                    logger.info(
                        f"Iris Reply: passive watch skipped by backpressure for group {group_id}"
                    )
                    return

            # 在调用前记账：失败也遵守冷却，避免后续每条回复立即重新采样。
            watch_last[group_id] = now
            watch_hashes[group_id] = digest

        req = DecisionRequest(group_id=group_id, wake="message", motive="watch")
        outcome = await self._decision_core.decide(req, self._llm_manager, provider_id)

        if outcome.error or outcome.decision is None:
            if outcome.error_kind == "input_content_safety_1026":
                cleanup, cooldown = await self._clear_rejected_reply_context(
                    group_id,
                    outcome.dynamic_context_sources,
                    record_skip=False,
                )
                logger.warning(
                    "Iris Reply: passive watch input rejected by provider safety filter "
                    f"for group {group_id} (1026, retryable=false, "
                    f"dynamic_sources={cleanup.dynamic_source_count}, "
                    f"window_removed={cleanup.window_removed}, "
                    f"observation_cleared={cleanup.observation_cleared}, "
                    f"anchor_cleared={cleanup.anchor_cleared}, "
                    f"cooldown={cooldown}min): "
                    f"{outcome.error}"
                )
                self._stats.record_decision_error(group_id, "watch")
                return
            else:
                logger.warning(f"Iris Reply: passive watch eval failed for group {group_id}: {outcome.error}")
            self._stats.record_decision_error(group_id, "watch")
            async with self._state.get_lock(group_id):
                self._state.write_anchor(
                    group_id, kind="passive", bot_message=bot_text, users=[fallback_sender],
                )
            await self._state.save_dirty(self._kv_save)
            return

        decision = outcome.decision
        # 决策侧 watch 清洗：被忽略账号不进入关注锚点，
        # 否则下一次他发消息就会命中 match_anchor_user 触发跟进。
        decision.watch = self._filter_watch_users(decision.watch)
        # 兜底锚点（模型没给 watch 时用触发者）同样不能落在忽略账号上
        fallback_users = self._filter_watch_users([fallback_sender])
        self._stats.record_decision(
            group_id, "watch",
            system_prompt=outcome.system_prompt,
            user_prompt=outcome.user_prompt,
            response_text=outcome.raw_text,
            decision=decision,
            duration_ms=outcome.duration_ms,
        )
        logger.info(
            f"Iris Reply: passive watch eval for group {group_id}: watch={decision.watch}, "
            f"keywords={decision.watch_keywords}, drifted={decision.drifted}"
        )

        async with self._state.get_lock(group_id):
            if decision.observation:
                self._state.set_observation(group_id, decision.observation)
            if decision.parse_failed:
                self._state.write_anchor(
                    group_id, kind="passive", bot_message=bot_text,
                    users=fallback_users or None,
                )
            elif decision.drifted:
                self._state.close_anchor(group_id)
                self._state.record_drift(group_id)
                logger.info(f"Iris Reply: topic drifted (passive) for group {group_id}, anchor closed")
            elif decision.watch or decision.watch_keywords:
                self._state.write_anchor(
                    group_id,
                    kind="passive",
                    bot_message=bot_text,
                    users=decision.watch or None,
                    keywords=decision.watch_keywords or None,
                    reason=decision.why,
                )
            else:
                self._state.write_anchor(
                    group_id, kind="passive", bot_message=bot_text,
                    users=fallback_users or None,
                )
        await self._state.save_dirty(self._kv_save)

    # AstrBot >= 4.23 才将 on_agent_done 暴露为插件钩子（旧版对话清理路径，默认不走）；
    # 低版本 AstrBot 下不注册该钩子，保证插件可正常加载
    if hasattr(filter, "on_agent_done"):
        @filter.on_agent_done()
        async def on_agent_done(self, event: AstrMessageEvent, run_context, resp) -> None:
            if self.component_manager:
                await handle_agent_done(event, resp, self.context, self.component_manager)

    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent) -> None:
        """消息发送前处理：错误友好化 + Markdown 去除"""
        result = event.get_result()
        if not result:
            return

        if self._error_processor and self._error_processor.should_process(event):
            self._error_processor.process_result(result)

        if self._markdown_stripper and self._markdown_stripper.should_process(event):
            self._markdown_stripper.process_result(result)
