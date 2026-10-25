"""Memory Beyond —— 为 AstrBot 重新设计的长期记忆与上下文压缩系统。

核心理念：真相在磁盘，上下文只带索引，细节按需取回，压缩后从磁盘重新注入。

分层约定：
    main.py     —— 平台适配层（本文件）：钩子、LLM 工具、指令、provider 调用
    web_api.py  —— WebUI 记忆管理页的后端接口（页面在 pages/memory）
    core/       —— 纯逻辑：记忆库、索引、召回、token 估算、轮次切分、
                   压缩状态机、会话登记、压缩归档、记忆整理

兼容性立场：只依赖 @filter.on_llm_request() / @filter.on_llm_response() /
@filter.llm_tool() 等公开插件接口，不 import AstrBot 内部实现、不 monkey-patch。
压缩采用抢先策略：在钩子里把 req.contexts 压到低于内置压缩器 0.82 的阈值，
内置压缩器的 should_compress() 因此永远为 False、自动退化为 no-op，
同时留在下游当安全网。AstrBot 每轮会把实际发送的消息列表回存为会话历史，
因此注入块一律带 _no_save 声明不入史；压缩后被摘要覆盖的原文会随回存从
平台历史卸下，由持久化的摘要继续代表（详见 core/compress.py 模块注释）。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star, StarTools

from .core import archive, compress, memstore, prompts, recall, registry, tidy, tokens
from .core.turns import split_leading_system, split_turns
from .web_api import MemoryDashboard

PLUGIN_NAME = "astrbot_plugin_memory_beyond"
LOG_PREFIX = "[memory_beyond] "

# 摘要提供商回退链中，当前对话提供商作为兜底时的尝试次数
# （作为首选时按配置的 summary_retry_count）。
FALLBACK_ATTEMPTS = 2
# 拿不到本轮工具声明时（结构异常等）估算工具 schema 的兜底余量；
# 正常路径按 req.func_tool 的实际 schema 序列化后实测。
TOOL_SCHEMA_ALLOWANCE = 800
# 应急裁剪时留给正文消息的最小预算。
MIN_TAIL_BUDGET = 1000
# 全局记忆关闭时对 scope=global 工具调用的固定回执；工具说明是静态的
# 仍会列出 global，回执要把模型引导回 session。
GLOBAL_MEMORY_OFF = "全局记忆已在插件配置中停用；请改用 session 作用域。"
PEOPLE_MEMORY_OFF = "人物记忆已在插件配置中停用；关于人的信息请写 session 作用域。"
PEOPLE_FILE_RULE = "people 作用域一人一个文件，文件名必须是 user-<数字ID>.md。"
SCOPE_ARG_ERROR = "scope 参数必须是 global、session 或 people。"
# 会话运行时缓存上限（LRU 淘汰，仅内存快照，磁盘状态不受影响）。
MAX_RUNTIME_SESSIONS = 256
# 模型挑选召回记忆时的单次超时：挑选在回复之前完成，等太久不如退回关键词
RECALL_SELECT_TIMEOUT = 15
# 遗忘扫描：每个作用域目录至多每隔这么久扫一次（秒）；默认多久没用到算遗忘（天）
FORGET_SWEEP_INTERVAL = 6 * 3600
FORGET_DEFAULT_DAYS = 90

# 自动探测模型上下文窗口的对照表：子串匹配，先命中先用。
# 探测只是兜底，README 中明确建议手动配置 max_context_tokens。
MODEL_WINDOWS: list[tuple[str, int]] = [
    ("gpt-5", 400_000),
    ("gpt-4.1", 1_000_000),
    ("gpt-4o", 128_000),
    ("gpt-4-turbo", 128_000),
    ("o4", 200_000),
    ("o3", 200_000),
    ("o1", 200_000),
    ("claude", 200_000),
    ("gemini-2", 1_000_000),
    ("gemini", 1_000_000),
    ("deepseek", 128_000),
    ("glm", 128_000),
    ("qwen3", 128_000),
    ("qwen-max", 128_000),
    ("qwen-plus", 128_000),
    ("qwen", 32_000),
    ("kimi", 128_000),
    ("moonshot", 128_000),
    ("grok-4", 256_000),
    ("grok", 128_000),
    ("doubao", 128_000),
]


def tool_schema_cost(toolset, estimator: tokens.TokenEstimator) -> int:
    """实测本轮声明的工具 schema 占用；没有工具为 0，读取失败退回固定余量。

    provider 发给 API 的就是 openai_schema() 的序列化结果（其余厂商格式
    同量级），照实测比猜一个常数可靠——工具的数量和描述长短因装的插件
    而异，差距可以数以千计。
    """
    if toolset is None:
        return 0
    try:
        if toolset.empty():
            return 0
        payload = json.dumps(toolset.openai_schema(), ensure_ascii=False)
        return estimator.text(payload)
    except Exception:  # noqa: BLE001
        return estimator.scale(TOOL_SCHEMA_ALLOWANCE)


@dataclass
class RuntimeSession:
    """一个会话的内存态：压缩状态 + 索引快照缓存。

    索引在对话开始时读盘一次、之后复用快照（prompt caching 按前缀匹配，
    开头的注入内容一变，整段历史的缓存全部失效）。对话中途模型新写的
    记忆只落盘、不刷新快照——需要时由每轮的相关记忆召回带上全文。
    压缩发生时清空快照、从磁盘重新加载——这是记忆扛过压缩的核心机制；
    对话切换、管理员在 WebUI 改动记忆时同样刷新。
    """

    state: compress.SessionState
    index_block: str | None = None
    index_loaded: bool = False
    # 快照构建时的全局记忆开关；配置热改后据此判定快照失效
    index_global_on: bool = False
    awaiting_calibration: bool = False
    # 上一轮的 usage 样本待消费：校准延迟到下一轮请求开头，那时才能从
    # 回存的历史里看出上一轮是否走了工具循环（那样的样本不能学）
    pending_calibration: bool = False
    warned_window: bool = False
    warned_summarizer: bool = False
    warned_clip: bool = False
    # 后台提取进行中（同一会话不并发提取）
    extracting: bool = False


class MemoryBeyondPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context, config)
        self.config = config if config is not None else {}
        data_dir = self._resolve_data_dir()
        self.data_dir = data_dir
        self._store = memstore.MemoryStore(data_dir / "memories")
        self._state_dir = data_dir / "state"
        self._registry = registry.SessionRegistry(data_dir / "sessions.json")
        self._archive = archive.ArchiveStore(data_dir / "archive")
        self._sessions: OrderedDict[str, RuntimeSession] = OrderedDict()
        self._tasks: set[asyncio.Task] = set()
        self._warned_threshold = False
        # 作用域目录 -> 上次遗忘扫描时间
        self._last_sweep: dict[Path, float] = {}
        # 定期整理：每处的记录与进行中标记；作用域 -> 上次检查时间；
        # 模型调用一次只跑一处，避免一波消息同时触发好几次复盘
        self._tidy = tidy.TidyBook(data_dir / "tidy")
        self._tidy_checked: dict[str, float] = {}
        self._tidy_gate = asyncio.Semaphore(1)
        self.dashboard = MemoryDashboard(self)
        self.dashboard.register()

        threshold, warning = compress.validate_threshold(
            self.config.get("compress_threshold", compress.THRESHOLD_FALLBACK)
        )
        if warning:
            logger.error(LOG_PREFIX + warning)
        logger.info(
            LOG_PREFIX
            + f"已加载：压缩阈值 {threshold}，数据目录 {data_dir}"
        )

    @staticmethod
    def _resolve_data_dir() -> Path:
        try:
            return Path(StarTools.get_data_dir(PLUGIN_NAME))
        except Exception:  # noqa: BLE001 - 旧版本 StarTools 行为不一，逐级回退
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            return Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME

    # ================================================================ 配置

    def _bool(self, key: str, default: bool) -> bool:
        return bool(self.config.get(key, default))

    def _int(self, key: str, default: int) -> int:
        try:
            return int(self.config.get(key, default))
        except (TypeError, ValueError):
            return default

    def _str(self, key: str, default: str = "") -> str:
        value = self.config.get(key, default)
        return value if isinstance(value, str) else default

    def _threshold(self) -> float:
        value, warning = compress.validate_threshold(
            self.config.get("compress_threshold", compress.THRESHOLD_FALLBACK)
        )
        if warning and not self._warned_threshold:
            self._warned_threshold = True
            logger.error(LOG_PREFIX + warning)
        return value

    def _target_ratio(self) -> float:
        """压缩完成后的目标占用 = 阈值 - 缓冲区，预留空间避免压完立刻又逼近上限。

        缓冲区默认 0.165，约留出窗口六分之一的余量。
        """
        try:
            buffer = float(self.config.get("compress_buffer", 0.165))
        except (TypeError, ValueError):
            buffer = 0.165
        buffer = min(max(buffer, 0.0), 0.5)
        return max(0.15, self._threshold() - buffer)

    # ============================================================ 会话运行时

    async def _runtime(self, umo: str) -> RuntimeSession:
        rt = self._sessions.get(umo)
        if rt is not None:
            self._sessions.move_to_end(umo)
            return rt
        rt = RuntimeSession(state=await self._load_state(umo))
        self._sessions[umo] = rt
        while len(self._sessions) > MAX_RUNTIME_SESSIONS:
            self._sessions.popitem(last=False)
        return rt

    def _invalidate_index_cache(self, umo: str | None) -> None:
        """作废索引快照，下次请求从磁盘重读。umo 为 None 时作废全部会话
        （global 记忆变更对所有会话可见）。"""
        if umo is None:
            targets = list(self._sessions.values())
        else:
            rt = self._sessions.get(umo)
            targets = [rt] if rt is not None else []
        for rt in targets:
            rt.index_block = None
            rt.index_loaded = False

    def runtime_for_key(self, key: str) -> tuple[str, RuntimeSession] | None:
        """按会话目录名找到已加载的运行时（WebUI 改动后同步内存态用）。"""
        for umo, rt in self._sessions.items():
            if memstore.safe_key(umo) == key:
                return umo, rt
        return None

    def invalidate_for_key(self, key: str | None) -> None:
        """WebUI 改动记忆后作废对应会话的索引快照；key 为 None 表示全局记忆。"""
        if key is None:
            self._invalidate_index_cache(None)
            return
        found = self.runtime_for_key(key)
        if found is not None:
            self._invalidate_index_cache(found[0])

    def state_path_for_key(self, key: str) -> Path:
        return self._state_dir / f"{key}.json"

    def _state_path(self, umo: str) -> Path:
        return self.state_path_for_key(memstore.safe_key(umo))

    async def load_state_file(self, path: Path) -> compress.SessionState:
        try:
            if path.is_file():
                raw = await asyncio.to_thread(path.read_text, "utf-8")
                return compress.SessionState.from_dict(json.loads(raw))
        except (OSError, ValueError) as exc:
            logger.warning(LOG_PREFIX + f"读取压缩状态失败（{path.name}）：{exc}")
        return compress.SessionState()

    async def _load_state(self, umo: str) -> compress.SessionState:
        return await self.load_state_file(self._state_path(umo))

    async def _save_state(self, umo: str, state: compress.SessionState) -> None:
        await self.save_state_file(self._state_path(umo), state)

    async def save_state_file(
        self, path: Path, state: compress.SessionState
    ) -> None:

        def _write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(
                json.dumps(state.to_dict(), ensure_ascii=False), encoding="utf-8"
            )
            tmp.replace(path)

        try:
            await asyncio.to_thread(_write)
        except OSError as exc:
            logger.warning(LOG_PREFIX + f"持久化压缩状态失败（{path.name}）：{exc}")

    def _spawn(self, coro, desc: str) -> None:
        """启动后台任务并保留引用（插件卸载时统一取消）。"""
        task = asyncio.create_task(coro, name=f"memory_beyond:{desc}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _touch_registry(self, event: AstrMessageEvent, umo: str) -> bool:
        """登记会话活跃；返回是否需要落盘。登记失败不影响请求。"""
        try:
            return self._registry.touch(
                memstore.safe_key(umo), umo, self._session_label(event)
            )
        except Exception:  # noqa: BLE001
            logger.debug(LOG_PREFIX + "会话登记失败", exc_info=True)
            return False

    async def _flush_registry(self) -> None:
        try:
            await asyncio.to_thread(self._registry.flush)
        except OSError as exc:
            logger.warning(LOG_PREFIX + f"会话登记簿写盘失败：{exc}")

    # ============================================================ 身份与作用域

    @staticmethod
    def _sender_identity(event: AstrMessageEvent) -> tuple[str, str]:
        """当前发送者的 (数字 ID, 昵称)。ID 不可伪造，昵称仅作附注。"""
        sender_id = ""
        try:
            sender_id = str(event.get_sender_id() or "").strip()
        except Exception:  # noqa: BLE001
            pass
        name = ""
        try:
            name = str(event.get_sender_name() or "").strip()
        except Exception:  # noqa: BLE001
            pass
        return sender_id, name

    @staticmethod
    def _sender_raw_fields(
        event: AstrMessageEvent,
    ) -> tuple[str | None, str | None, bool]:
        """从平台原始事件提取 (QQ昵称, 群名片, 是否群聊)。

        OneBot（aiocqhttp）的 sender.nickname 是 QQ 昵称、sender.card 是
        群名片；AstrBot 的 MessageMember 只保留了"群名片优先"的单一名称，
        所以这两个字段必须从 raw_message 里取。取不到时返回 (None, None, …)，
        标注退化为通用的 名称｜ID 形式。
        """
        msg_obj = getattr(event, "message_obj", None)
        is_group = bool(getattr(msg_obj, "group_id", "") or "")
        raw = getattr(msg_obj, "raw_message", None)
        raw_sender = None
        if isinstance(raw, dict):
            raw_sender = raw.get("sender")
        if raw_sender is None:
            raw_sender = getattr(raw, "sender", None)
        if isinstance(raw_sender, dict) and (
            "nickname" in raw_sender or "card" in raw_sender
        ):
            qq_name = str(raw_sender.get("nickname") or "")
            group_card = str(raw_sender.get("card") or "")
            return qq_name, group_card, is_group
        return None, None, is_group

    @classmethod
    def _session_label(cls, event: AstrMessageEvent) -> str:
        """会话的可读名称：群聊取平台提供的群名，私聊取对方昵称。"""
        msg_obj = getattr(event, "message_obj", None)
        group = getattr(msg_obj, "group", None)
        group_name = str(getattr(group, "group_name", "") or "").strip()
        if group_name and group_name.upper() != "N/A":
            return group_name
        if getattr(msg_obj, "group_id", ""):
            return ""
        return cls._sender_identity(event)[1]

    @staticmethod
    def _mentioned_ids(event: AstrMessageEvent) -> list[str]:
        """本条消息 @ 到的人与被回复消息的发送者（数字 ID），不含机器人自己。"""
        try:
            self_id = str(event.get_self_id() or "")
        except Exception:  # noqa: BLE001
            self_id = ""
        try:
            segments = list(event.get_messages() or [])
        except Exception:  # noqa: BLE001
            return []
        ids: list[str] = []
        for seg in segments:
            if type(seg).__name__ == "Reply":
                raw = getattr(seg, "sender_id", None)
            else:
                raw = getattr(seg, "qq", None)
            uid = str(raw or "").strip()
            if uid.isdigit() and uid != self_id and uid not in ids:
                ids.append(uid)
        return ids

    def _global_on(self) -> bool:
        """全局记忆开关，默认关闭。关闭时不注入全局索引、拒绝全局读写，
        机器人只使用当前会话的记忆；磁盘上已有的全局文件原样保留。"""
        return self._bool("enable_global_memory", False)

    def _global_scope(self) -> memstore.ScopeStore:
        """机器人自我的全局记忆，所有会话共享一个目录。"""
        return self._store.global_scope()

    def _session_scope(self, event: AstrMessageEvent) -> memstore.ScopeStore:
        return self._store.session_scope(str(event.unified_msg_origin))

    def _people_on(self) -> bool:
        """人物记忆开关，默认关闭。开启后每个平台一份人物目录、一人一个文件，
        该平台的所有会话共享；关闭时各会话各记各的，已有文件原样保留。"""
        return self._bool("enable_people_memory", False)

    def _forgetting_on(self) -> bool:
        """记忆遗忘开关，默认关闭。开启后长期没被召回或读取的记忆移出索引与
        召回（文件保留在 forgotten/ 下），再被用到时自动恢复；关闭时什么都不动，
        使用统计照常记录。"""
        return self._bool("enable_forgetting", False)

    def _forget_after_days(self) -> int:
        return max(1, self._int("forget_after_days", FORGET_DEFAULT_DAYS))

    def tidy_mode(self) -> str:
        """定期整理方式：off 关闭（默认）、confirm 有方案先问管理员、auto 直接应用。"""
        return tidy.normalize_mode(self._str("auto_consolidate", tidy.MODE_OFF))

    def tidy_hours(self) -> int:
        return tidy.clamp_hours(
            self._int("auto_consolidate_hours", tidy.DEFAULT_HOURS)
        )

    async def save_tidy_settings(self, mode: str, hours: int | None) -> None:
        """管理页改定期整理设置：写回插件配置，配置页里看到的随之变化。"""
        self.config["auto_consolidate"] = mode
        if hours is not None:
            self.config["auto_consolidate_hours"] = hours
        self._tidy_checked.clear()
        saver = getattr(self.config, "save_config", None)
        if callable(saver):
            await asyncio.to_thread(saver)

    def platform_ids(self) -> list[str]:
        """当前启用的机器人（平台适配器）ID，迁移时给管理员选。"""
        manager = getattr(self.context, "platform_manager", None)
        try:
            getter = getattr(manager, "get_insts", None)
            insts = getter() if callable(getter) else getattr(manager, "platform_insts", [])
        except Exception:  # noqa: BLE001 - 平台管理器的实现随版本变化
            return []
        ids: list[str] = []
        for inst in insts or []:
            try:
                platform_id = str(getattr(inst.meta(), "id", "") or "")
            except Exception:  # noqa: BLE001
                continue
            if platform_id and platform_id not in ids:
                ids.append(platform_id)
        return ids

    def _guidance(self) -> str:
        return prompts.memory_guidance(
            self._global_on(), self._people_on(), self._forgetting_on()
        )

    @staticmethod
    def _platform_of(umo: str) -> str:
        platform, _, _ = registry.parse_umo(umo)
        return platform or str(umo or "").split(":", 1)[0] or "unknown"

    def _people_scope(self, umo: str) -> memstore.ScopeStore:
        return self._store.people_scope(self._platform_of(umo))

    def _scope_for(
        self, event: AstrMessageEvent, scope: str
    ) -> memstore.ScopeStore | None:
        scope = (scope or "").strip().lower()
        if scope == "global":
            return self._global_scope()
        if scope == "session":
            return self._session_scope(event)
        if scope == "people":
            return self._people_scope(str(event.unified_msg_origin))
        return None

    def _tool_store(
        self, event: AstrMessageEvent, scope: str
    ) -> tuple[memstore.ScopeStore | None, str, str]:
        """记忆工具的作用域解析：返回 (作用域, 归一化名称, 拒绝原因)。"""
        scope = (scope or "").strip().lower()
        if scope == "global" and not self._global_on():
            return None, scope, GLOBAL_MEMORY_OFF
        if scope == "people" and not self._people_on():
            return None, scope, PEOPLE_MEMORY_OFF
        store = self._scope_for(event, scope)
        if store is None:
            return None, scope, SCOPE_ARG_ERROR
        return store, scope, ""

    # ================================================================ 钩子

    @filter.on_llm_request()
    async def inject_and_compress(
        self, event: AstrMessageEvent, req: ProviderRequest
    ):
        """唯一介入点：注入记忆索引、必要时抢先压缩，拼出本轮发送视图。"""
        try:
            await self._process_request(event, req)
        except Exception:  # noqa: BLE001 - 插件缺陷不能拖垮 LLM 请求链路
            logger.exception(LOG_PREFIX + "on_llm_request 处理失败，本轮跳过注入与压缩")

    async def _process_request(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        if not umo:
            return
        # 其他插件自行发起的模型调用（event.request_llm，不带对话也没有历史）
        # 同样经过这个钩子：它不是本会话的对话请求，不注入也不动压缩状态——
        # 否则"没有历史"会被当成对话被清空，把摘要作废
        if getattr(req, "conversation", None) is None and not (req.contexts or []):
            return
        rt = await self._runtime(umo)
        state = rt.state
        now = time.time()
        if self._touch_registry(event, umo):
            await self._flush_registry()
        # v1.2.5 之前注入的索引块会被 AstrBot 回存进平台历史，读入时清掉
        # 这些副本（现在注入块带 _no_save 不再入史，此过滤兼作历史自愈）
        contexts = [
            m
            for m in (req.contexts or [])
            if isinstance(m, dict) and not prompts.is_index_block(m)
        ]

        # ---- 消费上一轮的用量校准样本 ----
        # 估算只覆盖一轮的首次调用，而 usage 来自最后一次调用：上一轮若走
        # 了工具循环，最后一次调用会多出中途塞入的工具结果，这种样本会把
        # 比例系统性推高。工具痕迹要等历史回存后才能看到，所以校准延迟到
        # 这里；比例仍在本轮估算之前更新完毕，生效时机与原来一致。
        if rt.pending_calibration:
            rt.pending_calibration = False
            if compress.last_round_used_tools(contexts):
                logger.debug(
                    LOG_PREFIX
                    + f"会话 {umo} 上一轮走了工具循环，usage 样本不用于校准"
                )
            elif state.last_estimate > 0 and state.last_actual > 0:
                calibrator = tokens.TokenEstimator(state.ratio)
                calibrator.calibrate(state.last_estimate, state.last_actual)
                state.ratio = calibrator.ratio

        # ---- 状态与当前历史对齐：对话切换则作废，压缩被固化则重定位 ----
        cid = await self._conversation_id(event, req)
        had_summary = bool(state.summary)
        outcome = compress.reconcile(state, cid, contexts)
        if outcome == compress.RECONCILE_RESET:
            if had_summary:
                logger.info(
                    LOG_PREFIX
                    + f"会话 {umo} 的历史与压缩状态不再匹配（对话切换或历史清空），"
                    "压缩状态作废重建"
                )
            await self._save_state(umo, state)
            # 对话已切换/重置，索引快照跟着作废，本轮从磁盘重读
            rt.index_block = None
            rt.index_loaded = False
        elif outcome == compress.RECONCILE_COLLAPSED:
            logger.info(
                LOG_PREFIX
                + f"会话 {umo} 的压缩视图已被平台回存固化，水位线重定位为 0"
                "（摘要保留，当前历史整体视为摘要之后的尾部）"
            )
            await self._save_state(umo, state)

        # /reset 后 cid 不变、水位线为 0 时上面的校验不会触发：
        # 历史为空本身就说明是新对话，索引快照同样要刷新
        if not contexts and rt.index_loaded:
            rt.index_block = None
            rt.index_loaded = False

        # 召回打分用用户原话：取在加发送者标注之前
        query = str(req.prompt or "")

        # ---- 发送者标注：注入数字 ID，让记人不依赖可变可冒用的昵称 ----
        if self._bool("annotate_sender", True):
            self._annotate_sender(event, req)

        n_system = split_leading_system(contexts)
        memory_on = self._bool("enable_memory", True)

        # ---- 后台定期提取：每隔 N 轮把最近的对话交给模型补记长期事实 ----
        if memory_on:
            self._maybe_extract(rt, umo, contexts, n_system, now)
            # ---- 遗忘扫描：长期没用到的记忆退出索引与召回（后台，按目录节流） ----
            self._maybe_forget(umo, now)
            # ---- 定期整理：到了时间又攒够新记忆的作用域在后台复盘 ----
            self._maybe_tidy(umo, now)

        # ---- 记忆索引块（对话内快照；新对话、压缩后、WebUI 改动后从磁盘刷新） ----
        index_msg = None
        recall_msg = None
        if memory_on:
            block = await self._index_block(rt, event)
            if block:
                index_msg = prompts.build_index_message(block)
            # ---- 相关记忆召回：本轮用得上的记忆全文附在新消息之前 ----
            recall_msg = await self._recall_message(event, umo, query)

        guidance = self._guidance() if memory_on else ""
        estimator = tokens.TokenEstimator(state.ratio)

        overhead = (
            estimator.messages(contexts[:n_system])
            + estimator.text(str(req.system_prompt or "") + guidance)
            + estimator.text(str(req.prompt or ""))
            + estimator.scale(
                len(getattr(req, "image_urls", None) or []) * tokens.IMAGE_TOKENS
            )
        )
        tools_cost = tool_schema_cost(getattr(req, "func_tool", None), estimator)
        index_cost = estimator.messages([index_msg]) if index_msg else 0
        recall_cost = estimator.messages([recall_msg]) if recall_msg else 0

        # ---- 缓存已冷时清理旧工具结果：整段前缀反正要重新计费 ----
        idle_minutes = self._int("tool_result_idle_minutes", 60)
        cache_cold = (
            idle_minutes > 0
            and state.last_request_at > 0
            and now - state.last_request_at >= idle_minutes * 60
        )
        cleared_total = 0

        def current_tail() -> list[dict]:
            nonlocal cleared_total
            mark = max(state.watermark, n_system) if state.watermark else n_system
            tail = contexts[mark:]
            if cache_cold:
                tail, cleared = compress.clear_old_tool_results(
                    tail, self._int("keep_recent_turns", 3)
                )
                cleared_total = cleared
            return tail

        # ---- 抢先压缩 ----
        window, window_note = await self._resolve_window(event)
        compress_on = self._bool("enable_compression", True)
        if compress_on and window <= 0 and not rt.warned_window:
            rt.warned_window = True
            logger.warning(LOG_PREFIX + f"会话 {umo} 压缩已停用：{window_note}")

        tail = current_tail()
        clipped = False
        if compress_on and window > 0:
            threshold_tokens = int(window * self._threshold())
            fixed = overhead + tools_cost + index_cost + recall_cost

            def total_now() -> int:
                return fixed + estimator.text(state.summary) + estimator.messages(tail)

            total = total_now()
            if total > threshold_tokens:
                await self._compress(event, rt, umo, contexts, total)
                tail = current_tail()
                total = total_now()

            # 兜底裁剪只在压缩尝试之后仍超阈值时接管（摘要失败、冷却期、
            # 单轮过大）。它的目标水位低于触发阈值，若每轮无条件执行，
            # 会把总量永远摁在触发线之下，滚动摘要一次也轮不到。
            # 水位线不动；被裁内容会随平台回存历史而丢失，属有损最后手段。
            if total > threshold_tokens:
                budget = (
                    int(window * self._target_ratio())
                    - fixed
                    - estimator.text(state.summary)
                )
                tail, clipped = compress.fit_tail_to_budget(
                    tail, estimator, max(budget, MIN_TAIL_BUDGET)
                )
            if clipped:
                message = (
                    LOG_PREFIX
                    + f"会话 {umo} 本轮启用应急裁剪视图（摘要不可用或单轮过大），"
                    "被裁内容可能随平台回存历史而丢失"
                )
                if rt.warned_clip:
                    logger.debug(message)
                else:
                    rt.warned_clip = True
                    logger.warning(message)
        if cleared_total:
            logger.info(
                LOG_PREFIX
                + f"会话 {umo} 空闲超过 {idle_minutes} 分钟，提示词缓存已冷，"
                f"清理了 {cleared_total} 条较早的工具结果"
            )

        # ---- 拼装发送视图：system 消息 + 索引块 + 摘要块 + 原文尾部 + 召回块 ----
        summary_msg = (
            prompts.build_summary_message(state.summary, state.compressed_at)
            if state.summary
            else None
        )
        view = list(contexts[:n_system])
        if index_msg:
            view.append(index_msg)
        if summary_msg:
            view.append(summary_msg)
        view.extend(tail)
        # 召回块放在最末（紧挨着本轮新消息）：逐轮变化也不破坏前缀缓存
        if recall_msg:
            view.append(recall_msg)
        req.contexts = view

        if guidance and guidance not in str(req.system_prompt or ""):
            req.system_prompt = str(req.system_prompt or "") + guidance

        summary_cost = estimator.messages([summary_msg]) if summary_msg else 0
        tail_cost = estimator.messages(tail)
        # 分项构成随估算落盘，mb_status 与 WebUI 用它解释数值由什么组成
        state.last_parts = {
            "overhead": overhead,
            "tools": tools_cost,
            "index": index_cost,
            "summary": summary_cost,
            "recall": recall_cost,
            "history": tail_cost,
        }
        state.last_estimate = sum(state.last_parts.values())
        state.last_request_at = now
        rt.awaiting_calibration = True
        # 估算值随状态落盘，重启/重载插件后 mb_status 仍能给出上一次的统计
        await self._save_state(umo, state)

    async def _conversation_id(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> str:
        conversation = getattr(req, "conversation", None)
        cid = str(getattr(conversation, "cid", "") or "")
        if cid:
            return cid
        try:
            mgr = self.context.conversation_manager
            cid = await mgr.get_curr_conversation_id(event.unified_msg_origin)
            return str(cid or "")
        except Exception:  # noqa: BLE001
            return ""

    def _annotate_sender(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        """在用户消息开头注入 [发送者：…] 标注。

        QQ 群聊 → 群名片｜QQ名｜QQ号，QQ 私聊 → QQ名｜QQ号，
        其他平台 → 名称｜ID。标注随消息进入会话历史，群聊里跨轮次
        也能分清谁说了什么；LLM 记人时以数字 ID 为锚（名称可变、可被冒用）。
        正文里伪造的标注前缀会先被改写为全角括号，半角前缀因此只可能来自插件。
        """
        prompt = str(req.prompt or "")
        if not prompt:
            return
        sender_id, sender_name = self._sender_identity(event)
        if not sender_id:
            return
        qq_name, group_card, is_group = self._sender_raw_fields(event)
        tag = prompts.build_sender_tag(
            sender_id,
            display_name=sender_name,
            qq_name=qq_name,
            group_card=group_card,
            is_group=is_group,
        )
        if not tag:
            return
        first_line = prompt.split("\n", 1)[0]
        if first_line == tag:
            return  # 同一请求被重复处理时不叠加标注
        req.prompt = f"{tag}\n{prompts.neutralize_sender_forgery(prompt)}"

    async def _index_block(
        self, rt: RuntimeSession, event: AstrMessageEvent
    ) -> str | None:
        global_on = self._global_on()
        if rt.index_loaded and rt.index_global_on == global_on:
            return rt.index_block
        global_index = None
        if global_on:
            global_index = (await self._global_scope().load_index()).text
        session_snap = await self._session_scope(event).load_index()
        rt.index_block = prompts.build_index_block(global_index, session_snap.text)
        rt.index_loaded = True
        rt.index_global_on = global_on
        return rt.index_block

    # ============================================================ 召回

    async def _recall_message(
        self, event: AstrMessageEvent, umo: str, query: str
    ) -> dict | None:
        """挑出本轮相关的记忆，拼成附在新消息之前的召回块；失败只记日志。"""
        mode = self._str("recall_mode", "keyword").strip().lower()
        limit = self._int("recall_max_files", recall.DEFAULT_MAX_FILES)
        if mode == "off" or limit <= 0:
            return None
        try:
            scopes: list[tuple[str, memstore.ScopeStore]] = [
                ("session", self._store.session_scope(umo))
            ]
            if self._global_on():
                scopes.append(("global", self._global_scope()))
            if self._people_on():
                scopes.append(("people", self._people_scope(umo)))
            query = prompts.strip_sender_tag(query)
            sender_id, _ = self._sender_identity(event)
            forced = recall.identity_files(
                sender_id, self._mentioned_ids(event), query
            )
            if forced and self._forgetting_on():
                await self._remember_identity(scopes, forced)
            candidates: list[recall.Candidate] = []
            stores: dict[str, memstore.ScopeStore] = {}
            for label, store in scopes:
                stores[label] = store
                for header in await asyncio.to_thread(store.headers):
                    candidates.append(recall.Candidate(label, header))
            if not candidates:
                return None
            chosen = None
            if mode == "llm" and query.strip():
                # 人物文件只走身份必选，不让模型从别的群的人里挑
                chosen = await self._select_by_model(
                    umo, [c for c in candidates if c.scope != "people"], query, limit
                )
                if chosen is not None:
                    keyword_forced = recall.select_by_keywords(
                        candidates, "", forced, limit
                    )
                    merged = keyword_forced + [c for c in chosen if c not in keyword_forced]
                    chosen = merged[:limit]
            if chosen is None:
                chosen = recall.select_by_keywords(candidates, query, forced, limit)
            items: list[recall.Recalled] = []
            used: dict[str, list[str]] = {}
            for cand in chosen:
                content = await stores[cand.scope].read(cand.header.file)
                if content:
                    items.append(
                        recall.Recalled(cand.ref, content, cand.header.updated)
                    )
                    used.setdefault(cand.scope, []).append(cand.header.file)
            # 被召回就算用到：使用统计是遗忘与整理的依据
            for label, names in used.items():
                await asyncio.to_thread(stores[label].note_used, names)
            block = recall.build_recall_block(items, prompts.DATA_NOTICE)
            return recall.build_recall_message(block) if block else None
        except Exception:  # noqa: BLE001 - 召回是锦上添花，失败不影响本轮
            logger.warning(LOG_PREFIX + f"会话 {umo} 相关记忆召回失败", exc_info=True)
            return None

    async def _remember_identity(
        self, scopes: list[tuple[str, memstore.ScopeStore]], forced: list[str]
    ) -> None:
        """身份必选的人物文件若已被遗忘，这个人又出现了——先恢复再召回。"""
        for label, store in scopes:
            if label not in recall.FORCED_SCOPES:
                continue
            for name in forced:
                if store.exists(name) or not store.has_forgotten(name):
                    continue
                report = await store.restore(name)
                if report.ok:
                    logger.info(
                        LOG_PREFIX + f"{label} 记忆 {name} 已从遗忘中恢复（此人再次出现）"
                    )
                else:
                    logger.debug(LOG_PREFIX + f"恢复 {label}/{name} 失败：{report.message}")

    # ============================================================ 遗忘

    def _maybe_forget(self, umo: str, now: float) -> None:
        """遗忘扫描：本会话涉及的每个作用域目录至多每隔一段时间扫一次，后台进行。"""
        if not self._forgetting_on():
            return
        # (索引快照要作废的会话, 作用域)；None 作废全部会话（global 对所有会话可见），
        # "" 不作废任何会话（people 没有索引注入）
        targets: list[tuple[str | None, memstore.ScopeStore]] = [
            (umo, self._store.session_scope(umo))
        ]
        if self._global_on():
            targets.append((None, self._global_scope()))
        if self._people_on():
            targets.append(("", self._people_scope(umo)))
        due = [
            (key, store)
            for key, store in targets
            if now - self._last_sweep.get(store.root, 0.0) >= FORGET_SWEEP_INTERVAL
        ]
        if not due:
            return
        for _, store in due:
            self._last_sweep[store.root] = now
        self._spawn(self._forget_sweep(due, now), f"forget:{umo}")

    async def _forget_sweep(
        self, targets: list[tuple[str | None, memstore.ScopeStore]], now: float
    ) -> int:
        """对给定作用域做一次遗忘扫描，返回遗忘的文件总数。"""
        max_idle = self._forget_after_days() * 86400.0
        total = 0
        for key, store in targets:
            try:
                forgotten = await store.forget_stale(max_idle, now)
            except Exception:  # noqa: BLE001 - 遗忘失败不影响对话
                logger.warning(LOG_PREFIX + f"遗忘扫描失败：{store.root}", exc_info=True)
                continue
            if not forgotten:
                continue
            total += len(forgotten)
            logger.info(
                LOG_PREFIX
                + f"{store.root.parent.name}/{store.root.name} 遗忘了 {len(forgotten)} 条"
                f"长期未用的记忆：{', '.join(forgotten)}"
            )
            self._invalidate_index_cache(key)
        return total

    # ============================================================ 定期整理

    def _maybe_tidy(self, umo: str, now: float) -> None:
        """本会话涉及的每个作用域至多每隔几分钟看一眼该不该整理，要整理的放后台。"""
        mode = self.tidy_mode()
        if mode == tidy.MODE_OFF:
            return
        # (作用域, 目录名, 索引快照要作废的会话)；含义同 _maybe_forget
        targets: list[tuple[str, str | None, str | None]] = [
            ("session", self._store.session_scope(umo).root.name, umo)
        ]
        if self._global_on():
            targets.append(("global", None, None))
        if self._people_on():
            targets.append(("people", self._people_scope(umo).root.name, ""))
        due = []
        for scope, key, cache_key in targets:
            sid = tidy.scope_id(scope, key)
            if now - self._tidy_checked.get(sid, 0.0) < tidy.SCAN_INTERVAL:
                continue
            self._tidy_checked[sid] = now
            due.append((scope, key, cache_key))
        if due:
            self._spawn(self._auto_tidy(due, mode), f"tidy:{umo}")

    async def _auto_tidy(
        self, targets: list[tuple[str, str | None, str | None]], mode: str
    ) -> None:
        async with self._tidy_gate:
            for scope, key, cache_key in targets:
                try:
                    await self.tidy_scope(scope, key, cache_key, mode)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - 定期整理失败不影响对话
                    logger.warning(
                        LOG_PREFIX + f"定期整理 {tidy.scope_id(scope, key)} 出错",
                        exc_info=True,
                    )

    def _tidy_store(self, scope: str, key: str | None) -> memstore.ScopeStore | None:
        if scope == "global":
            return self._global_scope()
        if scope == "people":
            return self._store.people_scope_by_key(key or "")
        return self._store.session_scope_by_key(key or "")

    async def tidy_scope(
        self, scope: str, key: str | None, cache_key: str | None, mode: str
    ) -> str:
        """对一个作用域做一次定期整理，返回结论；没到时候或正忙返回空串。

        模型认为不需要整理就什么都不改；confirm 把方案留给管理员确认，
        auto 直接应用并记一份可撤销的记录。
        """
        store = self._tidy_store(scope, key)
        if store is None:
            return ""
        book = self._tidy
        sid = tidy.scope_id(scope, key)
        now = time.time()
        meta, headers = await asyncio.to_thread(
            lambda: (book.meta(sid), store.headers())
        )
        if not headers or tidy.due_reason(meta, headers, now, self.tidy_hours()):
            return ""
        if not book.claim(sid):
            return ""
        try:
            record = self._registry.get(key or "") if scope == "session" else None
            try:
                proposal = await tidy.propose(
                    store, tidy.scope_label(scope, key, record), self.call_consolidator
                )
            except tidy.TidyError as exc:

                def failed(meta: tidy.TidyMeta) -> None:
                    meta.last_attempt = now
                    meta.failures += 1
                    meta.outcome = tidy.OUTCOME_FAILED
                    meta.message = str(exc)

                await asyncio.to_thread(book.update_meta, sid, failed)
                logger.warning(LOG_PREFIX + f"定期整理 {sid} 失败：{exc}")
                return tidy.OUTCOME_FAILED
            ops = proposal.operations
            pending = None
            if not ops:
                outcome, message = tidy.OUTCOME_NOTHING, "模型认为不需要整理"
            elif mode == tidy.MODE_CONFIRM:
                pending = {
                    "createdAt": round(time.time(), 3),
                    "operations": [op.to_dict() for op in ops],
                    "skipped": proposal.skipped,
                    "files": proposal.files,
                }
                outcome, message = tidy.OUTCOME_PENDING, f"有 {len(ops)} 项修改等你确认"
            else:
                results = await tidy.apply(store, ops, people=scope == "people")
                run = tidy.make_run(tidy.TRIGGER_AUTO, results, time.time())
                if run is not None:
                    await asyncio.to_thread(book.add_run, sid, run)
                done = sum(1 for item in results if item["ok"])
                outcome = tidy.OUTCOME_APPLIED
                message = f"自动应用了 {done} / {len(results)} 项修改"
                self._invalidate_index_cache(cache_key)
            # 计时从整理结束算起：这次自己写的文件不算下一轮的"新记忆"
            finished = time.time()

            def succeeded(meta: tidy.TidyMeta) -> None:
                meta.last_run = finished
                meta.last_attempt = now
                meta.failures = 0
                meta.outcome = outcome
                meta.message = message
                if pending is not None:
                    meta.pending = pending

            await asyncio.to_thread(book.update_meta, sid, succeeded)
            logger.info(LOG_PREFIX + f"定期整理 {sid}：{message}")
            return outcome
        finally:
            book.release(sid)

    async def _select_by_model(
        self,
        umo: str,
        candidates: list[recall.Candidate],
        query: str,
        limit: int,
    ) -> list[recall.Candidate] | None:
        """让摘要模型从记忆清单里挑选；调用失败返回 None（退回关键词）。"""
        prompt = recall.build_selector_prompt(candidates, query, limit)
        text, attempts_log = await self._call_summarizer(
            umo, prompt, attempts=1, timeout=RECALL_SELECT_TIMEOUT
        )
        if not text:
            logger.debug(
                LOG_PREFIX + f"模型挑选召回失败，退回关键词：{'; '.join(attempts_log)}"
            )
            return None
        chosen = recall.parse_selector_response(text, candidates)
        if chosen is None:
            logger.debug(LOG_PREFIX + "模型挑选结果无法解析，退回关键词")
            return None
        return chosen[:limit]

    # ============================================================ 记忆提取

    async def _existing_for_extract(
        self,
        scope: memstore.ScopeStore,
        transcript: str,
        people: memstore.ScopeStore | None = None,
    ) -> tuple[str, dict[str, set[str]]]:
        """抽取前给模型看的已有记忆：相关的附全文（可合并改写），其余只列名字；
        人物记忆开启时，对话里说话人的人物文件也附全文（文件名前缀 people/）。

        返回 (提示词片段, 各作用域允许被改写的文件名集合)。
        """
        updatable: dict[str, set[str]] = {"session": set(), "people": set()}
        headers = await asyncio.to_thread(scope.headers)
        speakers = [f"user-{uid}.md" for uid in prompts.sender_ids_in(transcript)]
        full: list[prompts.ExistingMemory] = []
        used = 0
        if headers:
            candidates = [recall.Candidate("session", h) for h in headers]
            related = recall.select_by_keywords(
                candidates, transcript, speakers, recall.DEFAULT_MAX_FILES
            )
            for cand in related:
                content = await scope.read(cand.header.file)
                if not content or used + len(content) > prompts.EXISTING_FULL_CHARS:
                    continue
                used += len(content)
                full.append(
                    prompts.ExistingMemory(
                        cand.header.file, cand.header.description, content
                    )
                )
            updatable["session"] = {item.file for item in full}
        if people is not None:
            for name in speakers:
                content = await people.read(name)
                if not content or used + len(content) > prompts.EXISTING_FULL_CHARS:
                    continue
                used += len(content)
                doc = memstore.parse_memory(content)
                full.append(
                    prompts.ExistingMemory(
                        prompts.PEOPLE_PREFIX + name, doc.description, content
                    )
                )
                updatable["people"].add(name)
        listed = [
            prompts.ExistingMemory(h.file, h.description)
            for h in headers
            if h.file not in updatable["session"]
        ]
        if not full and not listed:
            return "", updatable
        return prompts.build_existing_section(full, listed), updatable

    async def _store_drafts(
        self,
        umo: str,
        drafts: list[prompts.MemoryDraft],
        updatable: dict[str, set[str]],
        source: str,
    ) -> int:
        """抽取出的记忆落盘：默认进会话作用域；人物记忆开启时，模型标为
        people 的人物文件进该平台的人物作用域。绝不进全局。

        已存在的文件只有在抽取时附过全文（模型据此合并）才允许改写，
        其余同名条目跳过——看不到原文就覆盖会丢内容。
        """
        stores = {"session": self._store.session_scope(umo)}
        if self._people_on():
            stores["people"] = self._people_scope(umo)
        stored = 0
        for draft in drafts:
            label = draft.scope if draft.scope in stores else "session"
            scope = stores[label]
            try:
                if draft.filename not in updatable.get(label, set()) and scope.exists(
                    draft.filename
                ):
                    continue
                report = await scope.write(
                    draft.filename, prompts.render_memory_file(draft)
                )
                if report.ok:
                    # 索引行由 write 按 frontmatter description 自动同步
                    stored += 1
            except OSError as exc:
                logger.warning(
                    LOG_PREFIX + f"{source}写入记忆 {label}/{draft.filename} 失败：{exc}"
                )
        if stored:
            logger.info(LOG_PREFIX + f"会话 {umo} {source}写入了 {stored} 条记忆")
        return stored

    def _maybe_extract(
        self,
        rt: RuntimeSession,
        umo: str,
        contexts: list[dict],
        n_system: int,
        now: float,
    ) -> None:
        """每隔 auto_extract_interval 轮，在后台把最近的对话交给模型补记长期事实。

        主模型在这段时间里自己写过记忆就跳过：它已经在记，这一段交给它。
        提取在后台进行，不拖慢本轮回复；同一会话同时只有一个提取任务。
        """
        interval = self._int("auto_extract_interval", 10)
        state = rt.state
        if interval <= 0:
            return
        state.turns_since_extract += 1
        if state.turns_since_extract < interval or rt.extracting:
            return
        state.turns_since_extract = 0
        if state.wrote_since_extract:
            state.wrote_since_extract = False
            return
        turns = split_turns(contexts, n_system)[-interval:]
        messages = [m for turn in turns for m in turn.messages]
        transcript = compress.render_transcript(messages)
        if not transcript.strip():
            return
        state.extracted_at = now
        rt.extracting = True
        self._spawn(self._run_extraction(rt, umo, transcript), f"extract:{umo}")

    async def _run_extraction(
        self, rt: RuntimeSession, umo: str, transcript: str
    ) -> None:
        try:
            people_on = self._people_on()
            scope = self._store.session_scope(umo)
            existing, updatable = await self._existing_for_extract(
                scope, transcript, self._people_scope(umo) if people_on else None
            )
            prompt = prompts.build_background_extract_prompt(
                transcript, existing, people_on
            )
            text, attempts_log = await self._call_summarizer(umo, prompt)
            if not text:
                logger.info(
                    LOG_PREFIX
                    + f"会话 {umo} 后台记忆提取未完成（{'; '.join(attempts_log) or '无可用提供商'}），"
                    "下个周期再试"
                )
                return
            drafts = prompts.parse_memories(text, people_on)
            await self._store_drafts(umo, drafts, updatable, "后台提取")
        except Exception:  # noqa: BLE001
            logger.warning(LOG_PREFIX + f"会话 {umo} 后台记忆提取出错", exc_info=True)
        finally:
            rt.extracting = False

    # ============================================================ 压缩执行

    async def _compress(
        self,
        event: AstrMessageEvent,
        rt: RuntimeSession,
        umo: str,
        contexts: list[dict],
        total_estimate: int,
    ) -> None:
        state = rt.state
        now = time.time()
        if state.in_cooldown(now):
            return
        keep = self._int("keep_recent_turns", 3)
        plan = compress.plan_compression(contexts, state.watermark, keep)
        if plan is None and keep > 1:
            # 历史凑不满配置的保留轮数时，降为只保最新一轮再试——直接放弃
            # 的话下游只剩有损裁剪，能卷进摘要的旧轮不该白白被裁掉
            plan = compress.plan_compression(contexts, state.watermark, 1)
        if plan is None:
            return
        transcript = compress.render_transcript(plan.to_summarize)
        if not transcript.strip():
            return

        extract = self._bool("extract_memories", True) and self._bool(
            "enable_memory", True
        )
        people_on = self._people_on()
        existing, updatable = "", {}
        if extract:
            existing, updatable = await self._existing_for_extract(
                self._store.session_scope(umo),
                transcript,
                self._people_scope(umo) if people_on else None,
            )
        max_chars = max(
            200, self._int("summary_max_chars", prompts.SUMMARY_MAX_CHARS_DEFAULT)
        )
        prompt = prompts.build_summary_prompt(
            self._str("summary_prompt_template"),
            state.summary,
            transcript,
            extract,
            existing,
            now=prompts.format_time(now),
            max_chars=max_chars,
            include_people=people_on,
        )
        text, attempts_log = await self._call_summarizer(umo, prompt)
        if text:
            summary, drafts = prompts.parse_summary_response(text, people_on)
        else:
            summary, drafts = "", []
        if not summary:
            state.record_failure(now)
            await self._save_state(umo, state)
            if not rt.warned_summarizer:
                rt.warned_summarizer = True
                logger.error(
                    LOG_PREFIX
                    + f"会话 {umo} 摘要生成失败（{'; '.join(attempts_log) or '无可用提供商'}），"
                    f"已连续失败 {state.fail_count} 次，进入退避冷却；"
                    "冷却期内启用应急裁剪视图（有损兜底），冷却后自动重试"
                )
            return

        old_watermark = state.watermark
        compress.apply_summary(state, contexts, plan, summary)
        rt.warned_summarizer = False
        rt.warned_clip = False
        await self._save_state(umo, state)
        logger.info(
            LOG_PREFIX
            + f"会话 {umo} 完成滚动摘要：水位线 {old_watermark} → {state.watermark}"
            f"（共 {len(contexts)} 条），压缩前估算 {total_estimate} tokens，"
            f"摘要 {len(state.summary)} 字"
        )
        if len(state.summary) > 2 * max_chars:
            logger.warning(
                LOG_PREFIX
                + f"会话 {umo} 的摘要 {len(state.summary)} 字，远超 summary_max_chars"
                f"（{max_chars}）：摘要模型没守住字数上限，每轮都要多带这些内容；"
                "可考虑换摘要模型或调大上限"
            )
        await self._archive_compressed(umo, state, plan.to_summarize)

        if extract and drafts:
            await self._store_drafts(umo, drafts, updatable, "压缩联动提取")
        # 压缩后从磁盘重新注入记忆索引：清空快照，下次请求重新加载。
        # 索引不是靠"被摘要保留下来"，而是每次从源头重新加载。
        rt.index_block = None
        rt.index_loaded = False

    async def _archive_compressed(
        self, umo: str, state: compress.SessionState, messages: list[dict]
    ) -> None:
        """把刚被摘要覆盖、即将从平台历史卸下的原文另存一份。"""
        keep = self._int("archive_keep", 20)
        if keep <= 0:
            return
        try:
            await asyncio.to_thread(
                lambda: self._archive.save(
                    memstore.safe_key(umo),
                    umo=umo,
                    cid=state.cid,
                    messages=messages,
                    summary=state.summary,
                    keep=keep,
                )
            )
        except OSError as exc:
            logger.warning(LOG_PREFIX + f"会话 {umo} 压缩归档写盘失败：{exc}")

    # ============================================================ 摘要调用链

    async def _call_summarizer(
        self,
        umo: str | None,
        prompt: str,
        *,
        attempts: int | None = None,
        timeout: int | None = None,
        preferred_id: str = "",
    ) -> tuple[str | None, list[str]]:
        """按回退链调用摘要模型：配置的提供商（重试 N 次）→ 当前对话提供商。

        umo 为 None 时（WebUI 发起的整理）当前提供商取默认提供商。
        preferred_id 是某类任务单独指定的提供商（如整理记忆），排在摘要提供商
        之前；两者相同只试一次。
        attempts / timeout 不传时按配置；召回挑选这类要求快的调用只试一次。
        返回 (成功文本或 None, 尝试记录)。全部失败的兜底不在这里——由调用方
        决定（压缩记录失败进入退避冷却，召回退回关键词）。
        """
        if timeout is None:
            timeout = max(10, self._int("summary_timeout", 120))
        retries = attempts or max(1, self._int("summary_retry_count", 3))
        attempts_log: list[str] = []

        chain: list[tuple[str, object, int]] = []
        wanted = [
            ("专用提供商", (preferred_id or "").strip()),
            ("摘要提供商", self._str("summary_provider_id").strip()),
        ]
        seen: set[str] = set()
        for kind, configured_id in wanted:
            if not configured_id or configured_id in seen:
                continue
            seen.add(configured_id)
            provider = await self._provider_by_id(configured_id)
            if provider is None:
                attempts_log.append(f"配置的{kind} {configured_id} 未找到")
                logger.warning(
                    LOG_PREFIX + f"配置的{kind} {configured_id} 未找到，继续回退链"
                )
            elif all(provider is not p for _, p, _ in chain):
                chain.append((f"{kind} {configured_id}", provider, retries))
        current = await self._current_provider(umo)
        if current is not None and all(current is not p for _, p, _ in chain):
            fallback = retries if not chain else min(retries, FALLBACK_ATTEMPTS)
            chain.append(("当前对话提供商", current, fallback))

        for label, provider, tries in chain:
            for attempt in range(1, tries + 1):
                try:
                    resp = await asyncio.wait_for(
                        provider.text_chat(prompt=prompt), timeout=timeout
                    )
                    text = self._completion_text(resp)
                    if text and text.strip():
                        attempts_log.append(f"{label} 第 {attempt} 次成功")
                        return text, attempts_log
                    attempts_log.append(f"{label} 第 {attempt} 次返回空")
                except asyncio.TimeoutError:
                    attempts_log.append(f"{label} 第 {attempt} 次超时（{timeout}s）")
                except Exception as exc:  # noqa: BLE001 - provider 实现不可控
                    attempts_log.append(f"{label} 第 {attempt} 次失败：{exc}")
                if attempt < tries:
                    await asyncio.sleep(min(2 ** (attempt - 1), 4))
        return None, attempts_log

    async def call_summarizer(self, prompt: str) -> tuple[str | None, list[str]]:
        """供 WebUI 使用的摘要模型调用（不绑定会话）。"""
        return await self._call_summarizer(None, prompt)

    async def call_consolidator(self, prompt: str) -> tuple[str | None, list[str]]:
        """管理页整理记忆用的模型调用：整理专用提供商优先，留空同摘要提供商。"""
        return await self._call_summarizer(
            None, prompt, preferred_id=self._str("consolidate_provider_id")
        )

    @staticmethod
    async def _maybe_await(value):
        if inspect.isawaitable(value):
            return await value
        return value

    async def _provider_by_id(self, provider_id: str):
        try:
            getter = getattr(self.context, "get_provider_by_id", None)
            if callable(getter):
                return await self._maybe_await(getter(provider_id=provider_id))
        except Exception as exc:  # noqa: BLE001
            logger.warning(LOG_PREFIX + f"按 ID 获取提供商 {provider_id} 失败：{exc}")
        return None

    async def _current_provider(self, umo: str | None):
        try:
            return await self._maybe_await(self.context.get_using_provider(umo=umo))
        except TypeError:
            try:
                return await self._maybe_await(self.context.get_using_provider())
            except Exception:  # noqa: BLE001
                return None
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _completion_text(resp) -> str:
        if resp is None:
            return ""
        if isinstance(resp, str):
            return resp
        try:
            return str(getattr(resp, "completion_text", "") or "")
        except Exception:  # noqa: BLE001
            return ""

    # ========================================================== 窗口探测

    async def _resolve_window(self, event: AstrMessageEvent) -> tuple[int, str]:
        configured = self._int("max_context_tokens", 0)
        if configured > 0:
            return configured, "手动配置"
        provider = await self._current_provider(
            str(getattr(event, "unified_msg_origin", "") or "") or None
        )
        model = self._provider_model_name(provider)
        if model:
            lowered = model.lower()
            for needle, window in MODEL_WINDOWS:
                if needle in lowered:
                    return window, f"按模型 {model} 自动探测"
        return 0, (
            f"自动探测模型窗口失败（模型名：{model or '未知'}），"
            "请在插件配置中手动指定 max_context_tokens"
        )

    @staticmethod
    def _provider_model_name(provider) -> str:
        if provider is None:
            return ""
        try:
            get_model = getattr(provider, "get_model", None)
            if callable(get_model):
                name = get_model()
                if name:
                    return str(name)
        except Exception:  # noqa: BLE001
            pass
        try:
            cfg = getattr(provider, "provider_config", None)
            if isinstance(cfg, dict):
                model_cfg = cfg.get("model_config")
                if isinstance(model_cfg, dict) and model_cfg.get("model"):
                    return str(model_cfg["model"])
                if cfg.get("model"):
                    return str(cfg["model"])
        except Exception:  # noqa: BLE001
            pass
        return ""

    # ========================================================== token 校准

    @filter.on_llm_response()
    async def calibrate_tokens(self, event: AstrMessageEvent, resp: LLMResponse):
        """provider 返回真实用量时记录样本，校准延迟到下一轮请求时消费。"""
        try:
            umo = str(getattr(event, "unified_msg_origin", ""))
            rt = self._sessions.get(umo)
            if rt is None or not rt.awaiting_calibration:
                return
            # 其他插件自行发起的调用（不带对话、没有历史）与本会话的估算不配对
            preq = getattr(event, "get_extra", lambda _k: None)("provider_request")
            if (
                preq is not None
                and getattr(preq, "conversation", None) is None
                and not (getattr(preq, "contexts", None) or [])
            ):
                return
            rt.awaiting_calibration = False
            if rt.state.last_estimate <= 0:
                return
            raw = getattr(resp, "raw_completion", None)
            usage = (
                raw.get("usage") if isinstance(raw, dict)
                else getattr(raw, "usage", None)
            )
            if isinstance(usage, dict):
                actual = usage.get("prompt_tokens")
            else:
                actual = getattr(usage, "prompt_tokens", None)
            try:
                actual = int(actual or 0)
            except (TypeError, ValueError):
                actual = 0
            cache_hit = tokens.extract_cache_hit(usage)
            if actual <= 0:
                # 原始响应缺 usage 时（部分流式/代理场景），回退 AstrBot 归一化的
                # TokenUsage。它把"未上报"也归为 0，所以缓存命中仅在 >0 时采信。
                normalized = getattr(resp, "usage", None)
                if normalized is not None:
                    try:
                        actual = int(getattr(normalized, "input", 0) or 0)
                        cached = int(getattr(normalized, "input_cached", 0) or 0)
                        cache_hit = cached if cached > 0 else None
                    except (TypeError, ValueError):
                        actual = 0
            if actual <= 0:
                return
            state = rt.state
            old_cache_hit = state.cache_hit_tokens
            # 真实上报值记下来，mb_status 里与估算并排展示可直接对照。
            # 比例不在这里学：本轮是否走了工具循环要等历史回存后才能判断，
            # 样本留到下一轮请求开头消费（compress.last_round_used_tools）。
            state.last_actual = actual
            state.cache_hit_tokens = -1 if cache_hit is None else cache_hit
            rt.pending_calibration = True
            # 缓存命中数变化时落盘；比例更新随下一轮请求末尾的常规落盘。
            if state.cache_hit_tokens != old_cache_hit:
                await self._save_state(umo, state)
        except Exception:  # noqa: BLE001
            logger.debug(LOG_PREFIX + "token 校准失败", exc_info=True)

    # ============================================================ 记忆工具

    @filter.llm_tool(name="memory_read")
    async def memory_read(
        self, event: AstrMessageEvent, scope: str, file: str = "MEMORY.md"
    ) -> str:
        """读取长期记忆文件的完整内容。注入的索引只是目录：回答涉及某人、某事的细节之前，先用本工具取回对应文件再作答，不要凭索引行猜；更新某条记忆之前也先读它的当前内容。

        Args:
            scope(string): 作用域，global（机器人自我的全局记忆）、session（当前会话的记忆）或 people（同一平台所有会话共享的人物记忆，一人一个 user-<数字ID>.md）
            file(string): 记忆文件名，如 user-12345678.md；省略时读 MEMORY.md 索引全文
        """
        if not self._bool("enable_memory", True):
            return "记忆功能已在插件配置中停用。"
        store, scope, refused = self._tool_store(event, scope)
        if store is None:
            return refused
        file = (file or "").strip()
        content = await store.read(file)
        restored = False
        if content is None and self._forgetting_on() and store.has_forgotten(file):
            # 模型主动读一条已遗忘的记忆：读取即恢复在册
            report = await store.restore(file)
            if report.ok:
                content = await store.read(file)
                restored = True
                logger.info(LOG_PREFIX + f"{scope} 记忆 {file} 已从遗忘中恢复（模型读取）")
        if content is None:
            existing = store.list_files()
            listing = "、".join(existing) if existing else "（该作用域还没有记忆文件）"
            return f"文件 {file} 不存在。该作用域现有文件：{listing}"
        if file != memstore.INDEX_FILENAME:
            if not restored:
                # 恢复时已经记过一次使用
                await asyncio.to_thread(store.note_used, [file])
            # 时效按 frontmatter 的 updated 戳算；老文件没有戳才退回 mtime
            written = memstore.parse_stamp(
                memstore.parse_memory(content).updated
            ) or store.mtime(file)
            notes = []
            if restored:
                notes.append("（这条记忆此前因长期未用已被遗忘，刚刚已恢复在册）")
            note = recall.freshness_note(written) if written else ""
            if note:
                notes.append(note)
            if notes:
                return "\n".join(notes) + f"\n\n{content}"
        return content

    @filter.llm_tool(name="memory_write")
    async def memory_write(
        self,
        event: AstrMessageEvent,
        scope: str,
        file: str,
        content: str = "",
        delete: bool = False,
    ) -> str:
        """写入、修改或删除一个长期记忆文件（整文件覆盖；修改＝读出后改写重写入，删除＝delete 设 true）。一个文件只记一条事实，content 必须以 frontmatter 开头：--- / name: 与文件名一致 / description: 一句话钩子 / metadata: / type: user|feedback|project|reference / ---，正文写事实本身。MEMORY.md 索引由插件自动维护，禁止直接写：要改索引行就重写对应文件的 description，要删索引行就删除对应文件。写前先查重，同一个人、同一件事始终更新同一个文件，不另建重复文件。作用域边界：关于具体用户或本会话的信息一律写 session，记人以发送者标注中的数字 ID 为锚（文件名 user-<数字ID>.md，昵称只作正文附注）；global 只存机器人自身的偏好与行为准则；people（启用时）只存某人亲口说的、换个群也成立的身份类事实。

        Args:
            scope(string): 作用域，global（机器人自我的全局记忆）、session（当前会话的记忆）或 people（同一平台所有会话共享的人物记忆，一人一个 user-<数字ID>.md）
            file(string): 记忆文件名，须以 .md 结尾，如 user-12345678.md；不可为 MEMORY.md
            content(string): 完整的文件内容，含 frontmatter（delete 为 false 时必填）
            delete(boolean): 设为 true 时删除该文件并自动移除其索引行，此时忽略 content
        """
        if not self._bool("enable_memory", True):
            return "记忆功能已在插件配置中停用。"
        store, scope, refused = self._tool_store(event, scope)
        if store is None:
            return refused
        if scope == "people" and not memstore.is_person_file(file):
            return PEOPLE_FILE_RULE
        if delete:
            report = await store.delete(file)
        else:
            if not content.strip():
                return "content 为空。写入需提供完整文件内容；如要删除请设 delete=true。"
            existed = store.exists(file)
            report = await store.write(
                file, content, source=self._sender_identity(event)[0]
            )
            if report.ok and not existed:
                hint = await self._duplicate_hint(store, file, content)
                if hint:
                    report = memstore.WriteReport(True, report.message + "\n" + hint)
        if report.ok:
            self._note_model_wrote(event)
        return report.message

    @filter.llm_tool(name="memory_edit")
    async def memory_edit(
        self,
        event: AstrMessageEvent,
        scope: str,
        file: str,
        old_text: str,
        new_text: str,
        replace_all: bool = False,
    ) -> str:
        """局部修改一个已有的长期记忆文件：把 old_text 替换成 new_text，文件其余内容原样保留。改一处、补一句、更新 description 时用本工具，不要用 memory_write 整文件重写（重写容易丢内容）。old_text 必须与文件现有内容逐字一致（空格、换行、标点都算）且只出现一次，所以先用 memory_read 读出当前内容再复制片段；new_text 留空等于删掉这段。改了 frontmatter 的 description，索引行会自动同步。

        Args:
            scope(string): 作用域，global（机器人自我的全局记忆）、session（当前会话的记忆）或 people（同一平台所有会话共享的人物记忆，一人一个 user-<数字ID>.md）
            file(string): 要修改的记忆文件名，如 user-12345678.md；不可为 MEMORY.md
            old_text(string): 要被替换的原文片段，须与文件内容逐字一致且只出现一次
            new_text(string): 替换后的文字；留空表示删掉 old_text
            replace_all(boolean): 设为 true 时替换文件里所有匹配处（如统一改一个称呼），默认只接受唯一匹配
        """
        if not self._bool("enable_memory", True):
            return "记忆功能已在插件配置中停用。"
        store, scope, refused = self._tool_store(event, scope)
        if store is None:
            return refused
        if scope == "people" and not memstore.is_person_file(file):
            return PEOPLE_FILE_RULE
        report = await store.edit(
            file,
            old_text,
            new_text,
            replace_all,
            source=self._sender_identity(event)[0],
        )
        if report.ok:
            self._note_model_wrote(event)
        return report.message

    async def _duplicate_hint(
        self, store: memstore.ScopeStore, file: str, content: str
    ) -> str:
        """新建文件后用召回的打分看看有没有疑似记同一件事的旧文件，有就提醒合并。"""
        try:
            doc = memstore.parse_memory(content)
            text = f"{doc.description} {doc.body[:600]}"
            headers = await asyncio.to_thread(store.headers)
            similar = recall.similar_memories(text, headers, file)
        except Exception:  # noqa: BLE001 - 查重只是提醒，失败不影响写入
            logger.debug(LOG_PREFIX + "写入查重失败", exc_info=True)
            return ""
        if not similar:
            return ""
        listing = "；".join(
            f"{h.file}（{h.description or '无描述'}）" for h in similar
        )
        return (
            f"提醒：新文件可能与已有记忆记的是同一件事：{listing}。"
            "请核对，确属同一件事就合并进一个文件、删掉多余的。"
        )

    def _note_model_wrote(self, event: AstrMessageEvent) -> None:
        # 索引快照不在这里刷新：开头的注入内容一变，整段历史的提示词
        # 缓存都会失效。新写的记忆已在磁盘上，相关时由召回带上全文，
        # 下次压缩或新对话时进入索引。这里只记下"主模型自己记过了"，
        # 后台定期提取据此跳过这一段。
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        rt = self._sessions.get(umo)
        if rt is not None:
            rt.state.wrote_since_extract = True

    @filter.llm_tool(name="memory_search")
    async def memory_search(
        self, event: AstrMessageEvent, query: str, scope: str = "all"
    ) -> str:
        """在长期记忆里全文搜索。写入前查重、索引被截断、或不确定某条记忆是否存在时，用本工具查找；它直接搜文件内容，不依赖索引。

        Args:
            query(string): 搜索词，可用空格分隔多个词（须同时命中）
            scope(string): 搜索范围，global、session、people 或 all（默认，启用的作用域都搜）
        """
        if not self._bool("enable_memory", True):
            return "记忆功能已在插件配置中停用。"
        query = (query or "").strip()
        if not query:
            return "query 不能为空。"
        scope = (scope or "all").strip().lower()
        global_on = self._global_on()
        people_on = self._people_on()
        if scope == "global" and not global_on:
            return GLOBAL_MEMORY_OFF
        if scope == "people" and not people_on:
            return PEOPLE_MEMORY_OFF
        stores: list[tuple[str, memstore.ScopeStore]] = []
        # 停用的作用域在 scope=all 时静默略过
        if scope in ("global", "all") and global_on:
            stores.append(("global", self._global_scope()))
        if scope in ("session", "all"):
            stores.append(("session", self._session_scope(event)))
        if scope in ("people", "all") and people_on:
            stores.append(
                ("people", self._people_scope(str(event.unified_msg_origin)))
            )
        if not stores:
            return "scope 参数必须是 global、session、people 或 all。"
        lines: list[str] = []
        for label, store in stores:
            for hit in await store.search(query):
                lines.append(f"[{label}] {hit}")
        forgotten: list[str] = []
        if self._forgetting_on():
            for label, store in stores:
                for hit in await store.search_forgotten(query):
                    forgotten.append(f"[{label}·已遗忘] {hit}")
        if not lines and not forgotten:
            return f"没有找到匹配「{query}」的记忆。"
        text = "搜索结果（用 memory_read 读取完整内容）：\n" + "\n".join(lines + forgotten)
        if forgotten:
            text += "\n标注「已遗忘」的是长期没用到而移出索引的记忆，用 memory_read 读取即自动恢复。"
        return text

    # ============================================================ 管理指令

    async def _estimate_live(
        self, event: AstrMessageEvent, rt: RuntimeSession
    ) -> tuple[int, dict]:
        """无估算记录时（如旧版本升级来的会话），从平台存储的历史现场估算。

        返回 (总量, 分项构成)。拿不到请求现场的人格提示词与本轮输入，
        结果略低于请求路径的口径。
        """
        state = rt.state
        try:
            umo = str(event.unified_msg_origin)
            mgr = self.context.conversation_manager
            cid = await mgr.get_curr_conversation_id(umo)
            if not cid:
                return 0, {}
            conversation = await mgr.get_conversation(umo, str(cid))
            raw = getattr(conversation, "history", "") or "[]"
            data = json.loads(raw) if isinstance(raw, str) else raw
            if not isinstance(data, list):
                return 0, {}
            # 与请求路径同口径：剔除旧版本固化进历史的索引块副本
            contexts = [
                m
                for m in data
                if isinstance(m, dict) and not prompts.is_index_block(m)
            ]
        except Exception:  # noqa: BLE001
            logger.debug(LOG_PREFIX + "现场估算读取历史失败", exc_info=True)
            return 0, {}
        if not contexts:
            return 0, {}
        memory_on = self._bool("enable_memory", True)
        estimator = tokens.TokenEstimator(state.ratio)
        n_system = split_leading_system(contexts)
        if compress.state_matches(state, str(cid), contexts) and state.watermark:
            mark = max(state.watermark, n_system)
        else:
            mark = n_system
        index_cost = 0
        if memory_on:
            block = await self._index_block(rt, event)
            if block:
                index_cost = estimator.messages([prompts.build_index_message(block)])
        # 工具声明取全局工具管理器里的启用工具（近似请求现场的 func_tool）
        try:
            toolset = self.context.get_llm_tool_manager().get_full_tool_set()
            for tool in list(toolset):
                if not getattr(tool, "active", True):
                    toolset.remove_tool(tool.name)
        except Exception:  # noqa: BLE001
            toolset = None
        tools_cost = (
            tool_schema_cost(toolset, estimator)
            if toolset is not None
            else estimator.scale(TOOL_SCHEMA_ALLOWANCE)
        )
        parts = {
            "overhead": (
                estimator.messages(contexts[:n_system])
                + estimator.text(self._guidance() if memory_on else "")
            ),
            "tools": tools_cost,
            "index": index_cost,
            "summary": (
                estimator.messages(
                    [prompts.build_summary_message(state.summary, state.compressed_at)]
                )
                if state.summary
                else 0
            ),
            "recall": 0,
            "history": estimator.messages(contexts[mark:]),
        }
        return sum(parts.values()), parts

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("mb_status")
    async def show_status(self, event: AstrMessageEvent):
        """查看 Memory Beyond 当前会话的记忆与压缩状态。"""
        umo = str(event.unified_msg_origin)
        rt = await self._runtime(umo)
        state = rt.state
        window, window_note = await self._resolve_window(event)
        global_on = self._global_on()
        global_part = (
            f"全局 {len(self._global_scope().list_files())} 个文件"
            if global_on
            else "全局 停用"
        )
        people_part = (
            f"人物 {len(self._people_scope(umo).list_files())} 个文件"
            if self._people_on()
            else "人物 停用"
        )
        session_files = self._session_scope(event).list_files()
        configured_id = self._str("summary_provider_id").strip() or "（未配置，用当前对话提供商）"
        tidy_id = self._str("consolidate_provider_id").strip() or "（未配置，同摘要提供商）"

        now = time.time()
        cooldown = (
            f"退避中，剩余 {int(state.fail_until - now)} 秒（已连续失败 {state.fail_count} 次）"
            if state.in_cooldown(now)
            else "正常"
        )
        if state.last_estimate > 0:
            estimate_line = (
                f"最近一次请求估算：{state.last_estimate} tokens"
                f"（校准比例 {state.ratio:.2f}）"
            )
            parts = state.last_parts
        else:
            live, live_parts = await self._estimate_live(event, rt)
            if live > 0:
                estimate_line = (
                    f"当前上下文现场估算：{live} tokens"
                    f"（校准比例 {state.ratio:.2f}，未含人格提示词与本轮输入）"
                )
                parts = live_parts
            else:
                estimate_line = (
                    f"最近一次请求估算：0 tokens（校准比例 {state.ratio:.2f}）"
                )
                parts = {}
        estimate_lines = [estimate_line]
        if parts:
            estimate_lines.append(
                f"　构成：固定开销 {parts.get('overhead', 0)}"
                f" + 工具声明 {parts.get('tools', 0)}"
                f" + 记忆索引 {parts.get('index', 0)}"
                f" + 摘要 {parts.get('summary', 0)}"
                f" + 召回 {parts.get('recall', 0)}"
                f" + 历史原文 {parts.get('history', 0)}"
            )
        if state.last_actual > 0:
            estimate_lines.append(
                f"最近一次实际上报：{state.last_actual} tokens"
                "（提供商 usage，估算校准以它为准）"
            )
        if window > 0 and parts:
            fixed_floor = parts.get("overhead", 0) + parts.get("tools", 0)
            if fixed_floor >= int(window * self._target_ratio()):
                estimate_lines.append(
                    "⚠️ 人格提示词与工具声明的固定开销已不低于压缩目标水位，"
                    "摘要腾不出空间，只能反复有损裁剪历史——"
                    "请调大 max_context_tokens，或减少启用的插件工具"
                )
        lines = [
            "Memory Beyond 状态",
            f"记忆：{'启用' if self._bool('enable_memory', True) else '停用'}"
            f"（{global_part} / {people_part} / 会话 {len(session_files)} 个文件）",
            f"压缩：{'启用' if self._bool('enable_compression', True) else '停用'}",
            f"上下文窗口：{window if window > 0 else '未知'}（{window_note}）",
            f"触发阈值：{self._threshold():g}，压缩目标：{self._target_ratio():g}",
            f"水位线：{state.watermark}，摘要：{len(state.summary)} 字",
            *estimate_lines,
            "最近一次缓存命中："
            + (
                f"{state.cache_hit_tokens} tokens"
                if state.cache_hit_tokens >= 0
                else "提供商未上报"
            ),
            f"摘要提供商：{configured_id}",
            f"整理提供商：{tidy_id}",
            f"摘要链路：{cooldown}",
            f"相关记忆召回：{self._recall_label()}",
            f"后台记忆提取：{self._extract_label(state)}",
            f"记忆遗忘：{self._forget_label(umo)}",
            f"定期整理：{self._tidy_label(umo)}",
            "记忆管理页：AstrBot WebUI 侧边栏「插件页面」",
        ]
        yield event.plain_result("\n".join(lines))

    def _forget_label(self, umo: str) -> str:
        if not self._forgetting_on():
            return "关闭"
        count = len(self._store.session_scope(umo).forgotten_files())
        return f"{self._forget_after_days()} 天没用到即遗忘（本会话已遗忘 {count} 条）"

    def _tidy_label(self, umo: str) -> str:
        mode = self.tidy_mode()
        if mode == tidy.MODE_OFF:
            return "关闭"
        name = "有方案先问管理员" if mode == tidy.MODE_CONFIRM else "有方案直接应用"
        label = f"{name}，至少间隔 {self.tidy_hours()} 小时"
        meta = self._tidy.meta(
            tidy.scope_id("session", self._store.session_scope(umo).root.name)
        )
        if meta.pending:
            return label + f"（本会话有 {meta.pending_count()} 项待确认）"
        if meta.last_run:
            when = time.strftime("%m-%d %H:%M", time.localtime(meta.last_run))
            return label + f"（本会话上次 {when}：{meta.message}）"
        return label

    def _recall_label(self) -> str:
        mode = self._str("recall_mode", "keyword").strip().lower()
        limit = self._int("recall_max_files", recall.DEFAULT_MAX_FILES)
        if mode == "off" or limit <= 0:
            return "关闭"
        name = "模型挑选" if mode == "llm" else "关键词匹配"
        return f"{name}，每轮最多 {limit} 条"

    def _extract_label(self, state: compress.SessionState) -> str:
        interval = self._int("auto_extract_interval", 10)
        if interval <= 0:
            return "关闭"
        return f"每 {interval} 轮一次（距上次 {state.turns_since_extract} 轮）"

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("mb_reset")
    async def reset_session(self, event: AstrMessageEvent):
        """清空本会话的压缩状态（摘要与水位线）。平台聊天历史不受影响。"""
        umo = str(event.unified_msg_origin)
        rt = await self._runtime(umo)
        rt.state.reset_compression()
        rt.state.record_success()
        rt.index_block = None
        rt.index_loaded = False
        await self._save_state(umo, rt.state)
        yield event.plain_result(
            "已清空本会话的压缩状态（摘要与水位线）。注意：此前已被压缩"
            "卸下的旧对话不会自动回来；之后超过阈值时会基于当前历史重新"
            "滚动摘要。"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("mb_probe")
    async def probe_summarizer(self, event: AstrMessageEvent):
        """测试摘要模型回退链是否可用（配置提供商 → 当前对话提供商）。"""
        yield event.plain_result("正在测试摘要链路……")
        text, attempts_log = await self._call_summarizer(
            str(event.unified_msg_origin), "请只回复两个字符：OK"
        )
        detail = "\n".join(attempts_log) if attempts_log else "（没有可尝试的提供商）"
        if text:
            yield event.plain_result(f"摘要链路可用。\n{detail}")
        else:
            yield event.plain_result(
                f"摘要链路全部失败，压缩时将启用应急裁剪视图并退避重试。\n{detail}"
            )

    async def terminate(self):
        """卸载/重载时：撤下 WebUI 接口、取消后台提取、登记簿落盘。

        压缩状态与记忆在每次变更时已即时落盘。
        """
        self.dashboard.unregister()
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        await self._flush_registry()
