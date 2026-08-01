"""Memory Beyond —— 为 AstrBot 重新设计的长期记忆与上下文压缩系统。

核心理念：真相在磁盘，上下文只带索引，细节按需取回，压缩后从磁盘重新注入。

分层约定：
    main.py  —— 平台适配层（本文件）：钩子、LLM 工具、指令、provider 调用
    core/    —— 纯逻辑：记忆库、索引、token 估算、轮次切分、压缩状态机

兼容性立场：只依赖 @filter.on_llm_request() / @filter.on_llm_response() /
@filter.llm_tool() 等公开插件接口，不 import AstrBot 内部实现、不 monkey-patch。
压缩采用抢先策略：在钩子里把 req.contexts 压到低于内置压缩器 0.82 的阈值，
内置压缩器的 should_compress() 因此永远为 False、自动退化为 no-op，
同时留在下游当安全网。平台的会话历史永远一个字不动。
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

from .core import compress, memstore, prompts, tokens
from .core.turns import split_leading_system

PLUGIN_NAME = "astrbot_plugin_memory_beyond"
LOG_PREFIX = "[memory_beyond] "

# 摘要提供商回退链中，当前对话提供商作为兜底时的尝试次数
# （作为首选时按配置的 summary_retry_count）。
FALLBACK_ATTEMPTS = 2
# 函数工具 schema、人设开场白等无法逐项估算的部分，给一个固定余量。
TOOL_SCHEMA_ALLOWANCE = 800
# 应急裁剪时留给正文消息的最小预算。
MIN_TAIL_BUDGET = 1000
# 会话运行时缓存上限（LRU 淘汰，仅内存快照，磁盘状态不受影响）。
MAX_RUNTIME_SESSIONS = 256

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


@dataclass
class RuntimeSession:
    """一个会话的内存态：压缩状态 + 索引快照缓存。

    索引在会话首次请求时读盘一次、缓存快照复用（prompt caching 按前缀匹配，
    注入内容逐轮变化会让缓存全量失效）；中途新存的记忆只写磁盘。
    压缩发生时清空快照、从磁盘重新加载——这是记忆扛过压缩的核心机制。
    """

    state: compress.SessionState
    index_block: str | None = None
    index_loaded: bool = False
    last_estimate: int = 0
    awaiting_calibration: bool = False
    warned_window: bool = False
    warned_summarizer: bool = False
    warned_clip: bool = False


class MemoryBeyondPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config if config is not None else {}
        data_dir = self._resolve_data_dir()
        self._store = memstore.MemoryStore(data_dir / "memories")
        self._state_dir = data_dir / "state"
        self._sessions: OrderedDict[str, RuntimeSession] = OrderedDict()
        self._warned_threshold = False

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

    def _state_path(self, umo: str) -> Path:
        return self._state_dir / f"{memstore.safe_key(umo)}.json"

    async def _load_state(self, umo: str) -> compress.SessionState:
        path = self._state_path(umo)
        try:
            if path.is_file():
                raw = await asyncio.to_thread(path.read_text, "utf-8")
                return compress.SessionState.from_dict(json.loads(raw))
        except (OSError, ValueError) as exc:
            logger.warning(LOG_PREFIX + f"读取压缩状态失败（{umo}）：{exc}")
        return compress.SessionState()

    async def _save_state(self, umo: str, state: compress.SessionState) -> None:
        path = self._state_path(umo)

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
            logger.warning(LOG_PREFIX + f"持久化压缩状态失败（{umo}）：{exc}")

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

    def _global_scope(self) -> memstore.ScopeStore:
        """机器人自我的全局记忆，所有会话共享一个目录。"""
        return self._store.global_scope()

    def _session_scope(self, event: AstrMessageEvent) -> memstore.ScopeStore:
        return self._store.session_scope(str(event.unified_msg_origin))

    def _scope_for(
        self, event: AstrMessageEvent, scope: str
    ) -> memstore.ScopeStore | None:
        scope = (scope or "").strip().lower()
        if scope == "global":
            return self._global_scope()
        if scope == "session":
            return self._session_scope(event)
        return None

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
        rt = await self._runtime(umo)
        state = rt.state
        contexts = [m for m in (req.contexts or []) if isinstance(m, dict)]

        # ---- 校验压缩状态仍锚定在当前对话与历史前缀上 ----
        cid = await self._conversation_id(event, req)
        if not compress.state_matches(state, cid, contexts):
            if state.summary:
                logger.info(
                    LOG_PREFIX
                    + f"会话 {umo} 的历史与水位线不再匹配（对话切换或被重置），"
                    "压缩状态作废重建"
                )
            state.cid = cid
            state.reset_compression()
            await self._save_state(umo, state)
            # 对话已切换/重置，索引快照跟着作废，本轮从磁盘重读
            rt.index_block = None
            rt.index_loaded = False

        # /reset 后 cid 不变、水位线为 0 时上面的校验不会触发：
        # 历史为空本身就说明是新对话，索引快照同样要刷新
        if not contexts and rt.index_loaded:
            rt.index_block = None
            rt.index_loaded = False

        # ---- 发送者标注：注入数字 ID，让记人不依赖可变可冒用的昵称 ----
        if self._bool("annotate_sender", True):
            self._annotate_sender(event, req)

        # ---- 记忆索引块（会话内快照；新对话、压缩后、写入后从磁盘刷新） ----
        memory_on = self._bool("enable_memory", True)
        index_msg = None
        if memory_on:
            block = await self._index_block(rt, event)
            if block:
                index_msg = prompts.build_index_message(block)

        guidance = prompts.MEMORY_GUIDANCE if memory_on else ""
        estimator = tokens.TokenEstimator(state.ratio)

        n_system = split_leading_system(contexts)
        overhead = (
            estimator.messages(contexts[:n_system])
            + estimator.text(str(req.system_prompt or "") + guidance)
            + estimator.text(str(req.prompt or ""))
            + len(getattr(req, "image_urls", None) or []) * tokens.IMAGE_TOKENS
            + TOOL_SCHEMA_ALLOWANCE
        )
        if index_msg:
            overhead += estimator.text(index_msg["content"]) + tokens.MESSAGE_OVERHEAD

        def current_tail() -> list[dict]:
            mark = max(state.watermark, n_system) if state.watermark else n_system
            return contexts[mark:]

        # ---- 抢先压缩 ----
        window, window_note = await self._resolve_window(event)
        compress_on = self._bool("enable_compression", True)
        if compress_on and window <= 0 and not rt.warned_window:
            rt.warned_window = True
            logger.warning(LOG_PREFIX + f"会话 {umo} 压缩已停用：{window_note}")

        tail = current_tail()
        clipped = False
        if compress_on and window > 0:
            total = (
                overhead
                + estimator.text(state.summary)
                + estimator.messages(tail)
            )
            if total > int(window * self._threshold()):
                await self._compress(event, rt, umo, contexts, total)
                tail = current_tail()

            # 兜底：摘要后仍超预算（或摘要模型不可用）时，把本轮发送视图
            # 裁进目标预算。只影响本轮，水位线与平台历史都不动。
            budget = (
                int(window * self._target_ratio())
                - overhead
                - estimator.text(state.summary)
            )
            tail, clipped = compress.fit_tail_to_budget(
                tail, estimator, max(budget, MIN_TAIL_BUDGET)
            )
            if clipped:
                message = (
                    LOG_PREFIX
                    + f"会话 {umo} 本轮启用应急裁剪视图（摘要不可用或单轮过大），"
                    "平台历史未受影响"
                )
                if rt.warned_clip:
                    logger.debug(message)
                else:
                    rt.warned_clip = True
                    logger.warning(message)

        # ---- 拼装发送视图：system 消息 + 索引块 + 摘要块 + 原文尾部 ----
        view = list(contexts[:n_system])
        if index_msg:
            view.append(index_msg)
        if state.summary:
            view.append(prompts.build_summary_message(state.summary))
        view.extend(tail)
        req.contexts = view

        if guidance and guidance not in str(req.system_prompt or ""):
            req.system_prompt = str(req.system_prompt or "") + guidance

        rt.last_estimate = (
            overhead
            + estimator.text(state.summary)
            + estimator.messages(tail)
        )
        rt.awaiting_calibration = True

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
        if rt.index_loaded:
            return rt.index_block
        global_snap = await self._global_scope().load_index()
        session_snap = await self._session_scope(event).load_index()
        rt.index_block = prompts.build_index_block(global_snap.text, session_snap.text)
        rt.index_loaded = True
        return rt.index_block

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
        plan = compress.plan_compression(
            contexts, state.watermark, self._int("keep_recent_turns", 3)
        )
        if plan is None:
            return
        transcript = compress.render_transcript(plan.to_summarize)
        if not transcript.strip():
            return

        extract = self._bool("extract_memories", True)
        prompt = prompts.build_summary_prompt(
            self._str("summary_prompt_template"),
            state.summary,
            transcript,
            extract,
        )
        text, attempts_log = await self._call_summarizer(event, prompt)
        if text:
            summary, drafts = prompts.parse_summary_response(text)
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
                    "冷却期内使用应急裁剪视图，历史无损，冷却后自动重试"
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

        if extract and drafts:
            await self._store_extracted(umo, drafts)
        # 压缩后从磁盘重新注入记忆索引：清空快照，下次请求重新加载。
        # 索引不是靠"被摘要保留下来"，而是每次从源头重新加载。
        rt.index_block = None
        rt.index_loaded = False

    async def _store_extracted(
        self, umo: str, drafts: list[prompts.MemoryDraft]
    ) -> None:
        """压缩-记忆联动：摘要的同一次 LLM 调用里提取的事实落盘为记忆文件。

        自动提取的信息可能源自群聊，按隐私边界只进会话作用域，绝不进全局。
        """
        scope = self._store.session_scope(umo)
        stored = 0
        for draft in drafts:
            try:
                if await scope.read(draft.filename) is not None:
                    continue
                report = await scope.write(
                    draft.filename, prompts.render_memory_file(draft)
                )
                if report.ok:
                    # 索引行由 write 按 frontmatter description 自动同步
                    stored += 1
            except OSError as exc:
                logger.warning(
                    LOG_PREFIX + f"压缩联动写入记忆 {draft.filename} 失败：{exc}"
                )
        if stored:
            logger.info(LOG_PREFIX + f"会话 {umo} 压缩联动提取了 {stored} 条记忆入库")

    # ============================================================ 摘要调用链

    async def _call_summarizer(
        self, event: AstrMessageEvent, prompt: str
    ) -> tuple[str | None, list[str]]:
        """按回退链调用摘要模型：配置的提供商（重试 N 次）→ 当前对话提供商。

        返回 (成功文本或 None, 尝试记录)。全部失败的兜底不在这里——由调用方
        记录失败进入退避冷却，本轮走应急裁剪视图。
        """
        timeout = max(10, self._int("summary_timeout", 120))
        retries = max(1, self._int("summary_retry_count", 3))
        attempts_log: list[str] = []

        chain: list[tuple[str, object, int]] = []
        configured_id = self._str("summary_provider_id").strip()
        if configured_id:
            provider = await self._provider_by_id(configured_id)
            if provider is None:
                attempts_log.append(f"配置的提供商 {configured_id} 未找到")
                logger.warning(
                    LOG_PREFIX
                    + f"配置的摘要提供商 {configured_id} 未找到，回退当前对话提供商"
                )
            else:
                chain.append((f"配置提供商 {configured_id}", provider, retries))
        current = await self._current_provider(event)
        if current is not None and all(current is not p for _, p, _ in chain):
            chain.append(
                ("当前对话提供商", current, retries if not chain else FALLBACK_ATTEMPTS)
            )

        for label, provider, attempts in chain:
            for attempt in range(1, attempts + 1):
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
                if attempt < attempts:
                    await asyncio.sleep(min(2 ** (attempt - 1), 4))
        return None, attempts_log

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

    async def _current_provider(self, event: AstrMessageEvent):
        try:
            return await self._maybe_await(
                self.context.get_using_provider(umo=event.unified_msg_origin)
            )
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
        provider = await self._current_provider(event)
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
        """provider 返回真实用量时，用真实值校准估算比例（优先用真实值）。"""
        try:
            umo = str(getattr(event, "unified_msg_origin", ""))
            rt = self._sessions.get(umo)
            if rt is None or not rt.awaiting_calibration or rt.last_estimate <= 0:
                return
            rt.awaiting_calibration = False
            usage = getattr(getattr(resp, "raw_completion", None), "usage", None)
            if isinstance(usage, dict):
                actual = usage.get("prompt_tokens")
            else:
                actual = getattr(usage, "prompt_tokens", None)
            actual = int(actual or 0)
            if actual <= 0:
                return
            state = rt.state
            old_ratio = state.ratio
            old_cache_hit = state.cache_hit_tokens
            estimator = tokens.TokenEstimator(state.ratio)
            estimator.calibrate(rt.last_estimate, actual)
            state.ratio = estimator.ratio
            cache_hit = tokens.extract_cache_hit(usage)
            state.cache_hit_tokens = -1 if cache_hit is None else cache_hit
            # 校准结果落盘，重启后不回退；比例漂移小且缓存命中数没变时跳过，
            # 避免收敛后每轮响应都写盘。
            if (
                abs(state.ratio - old_ratio) > 0.005
                or state.cache_hit_tokens != old_cache_hit
            ):
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
            scope(string): 作用域，global（机器人自我的全局记忆）或 session（当前会话的记忆）
            file(string): 记忆文件名，如 user-12345678.md；省略时读 MEMORY.md 索引全文
        """
        if not self._bool("enable_memory", True):
            return "记忆功能已在插件配置中停用。"
        store = self._scope_for(event, scope)
        if store is None:
            return "scope 参数必须是 global 或 session。"
        content = await store.read(file)
        if content is None:
            existing = store.list_files()
            listing = "、".join(existing) if existing else "（该作用域还没有记忆文件）"
            return f"文件 {file} 不存在。该作用域现有文件：{listing}"
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
        """写入、修改或删除一个长期记忆文件（整文件覆盖；修改＝读出后改写重写入，删除＝delete 设 true）。一个文件只记一条事实，content 必须以 frontmatter 开头：--- / name: 与文件名一致 / description: 一句话钩子 / metadata: / type: user|feedback|project|reference / ---，正文写事实本身。MEMORY.md 索引由插件自动维护，禁止直接写：要改索引行就重写对应文件的 description，要删索引行就删除对应文件。写前先查重，同一个人、同一件事始终更新同一个文件，不另建重复文件。作用域边界：关于具体用户或本会话的信息一律写 session，记人以发送者标注中的数字 ID 为锚（文件名 user-<数字ID>.md，昵称只作正文附注）；global 只存机器人自身的偏好与行为准则。

        Args:
            scope(string): 作用域，global（机器人自我的全局记忆）或 session（当前会话的记忆）
            file(string): 记忆文件名，须以 .md 结尾，如 user-12345678.md；不可为 MEMORY.md
            content(string): 完整的文件内容，含 frontmatter（delete 为 false 时必填）
            delete(boolean): 设为 true 时删除该文件并自动移除其索引行，此时忽略 content
        """
        if not self._bool("enable_memory", True):
            return "记忆功能已在插件配置中停用。"
        scope = (scope or "").strip().lower()
        store = self._scope_for(event, scope)
        if store is None:
            return "scope 参数必须是 global 或 session。"
        if delete:
            report = await store.delete(file)
        else:
            if not content.strip():
                return "content 为空。写入需提供完整文件内容；如要删除请设 delete=true。"
            report = await store.write(file, content)
        if report.ok:
            # 记忆已变化，作废索引快照：session 只影响本会话，global 影响全部
            umo = str(getattr(event, "unified_msg_origin", "") or "")
            self._invalidate_index_cache(umo if scope == "session" else None)
        return report.message

    @filter.llm_tool(name="memory_search")
    async def memory_search(
        self, event: AstrMessageEvent, query: str, scope: str = "all"
    ) -> str:
        """在长期记忆里全文搜索。写入前查重、索引被截断、或不确定某条记忆是否存在时，用本工具查找；它直接搜文件内容，不依赖索引。

        Args:
            query(string): 搜索词，可用空格分隔多个词（须同时命中）
            scope(string): 搜索范围，global、session 或 all（默认，两个作用域都搜）
        """
        if not self._bool("enable_memory", True):
            return "记忆功能已在插件配置中停用。"
        query = (query or "").strip()
        if not query:
            return "query 不能为空。"
        scope = (scope or "all").strip().lower()
        stores: list[tuple[str, memstore.ScopeStore]] = []
        if scope in ("global", "all"):
            stores.append(("global", self._global_scope()))
        if scope in ("session", "all"):
            stores.append(("session", self._session_scope(event)))
        if not stores:
            return "scope 参数必须是 global、session 或 all。"
        lines: list[str] = []
        for label, store in stores:
            for hit in await store.search(query):
                lines.append(f"[{label}] {hit}")
        if not lines:
            return f"没有找到匹配「{query}」的记忆。"
        return "搜索结果（用 memory_read 读取完整内容）：\n" + "\n".join(lines)

    # ============================================================ 管理指令

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("mb_status")
    async def show_status(self, event: AstrMessageEvent):
        """查看 Memory Beyond 当前会话的记忆与压缩状态。"""
        umo = str(event.unified_msg_origin)
        rt = await self._runtime(umo)
        state = rt.state
        window, window_note = await self._resolve_window(event)
        global_files = self._global_scope().list_files()
        session_files = self._session_scope(event).list_files()
        configured_id = self._str("summary_provider_id").strip() or "（未配置，用当前对话提供商）"

        now = time.time()
        cooldown = (
            f"退避中，剩余 {int(state.fail_until - now)} 秒（已连续失败 {state.fail_count} 次）"
            if state.in_cooldown(now)
            else "正常"
        )
        lines = [
            "Memory Beyond 状态",
            f"记忆：{'启用' if self._bool('enable_memory', True) else '停用'}"
            f"（全局 {len(global_files)} 个文件 / 会话 {len(session_files)} 个文件）",
            f"压缩：{'启用' if self._bool('enable_compression', True) else '停用'}",
            f"上下文窗口：{window if window > 0 else '未知'}（{window_note}）",
            f"触发阈值：{self._threshold()}，压缩目标：{self._target_ratio()}",
            f"水位线：{state.watermark}，摘要：{len(state.summary)} 字",
            f"最近一次请求估算：{rt.last_estimate} tokens（校准比例 {state.ratio:.2f}）",
            "最近一次缓存命中："
            + (
                f"{state.cache_hit_tokens} tokens"
                if state.cache_hit_tokens >= 0
                else "提供商未上报"
            ),
            f"摘要提供商：{configured_id}",
            f"摘要链路：{cooldown}",
        ]
        yield event.plain_result("\n".join(lines))

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
            "已清空本会话的压缩状态（摘要与水位线）。平台历史完整保留，"
            "上下文超过阈值时会重新滚动摘要。"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("mb_probe")
    async def probe_summarizer(self, event: AstrMessageEvent):
        """测试摘要模型回退链是否可用（配置提供商 → 当前对话提供商）。"""
        yield event.plain_result("正在测试摘要链路……")
        text, attempts_log = await self._call_summarizer(
            event, "请只回复两个字符：OK"
        )
        detail = "\n".join(attempts_log) if attempts_log else "（没有可尝试的提供商）"
        if text:
            yield event.plain_result(f"摘要链路可用。\n{detail}")
        else:
            yield event.plain_result(
                f"摘要链路全部失败，压缩时将启用应急裁剪视图并退避重试。\n{detail}"
            )

    async def terminate(self):
        """状态在每次变更时已即时落盘，无需额外清理。"""
