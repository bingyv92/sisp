"""sisp 事件处理器。

SISPStateHandler   - 订阅 on_message_received，评估用户消息并推进状态机。
SISPPromptInjector - 订阅 on_prompt_build，向支持的 prompt 模板注入状态标签。

注意：system_reminder 为全局存储（无法按 session 隔离），因此 SISPPromptInjector
只处理带 stream_id 的 prompt build 事件。当前兼容：
- default_chatter_user_prompt -> 写入 values["extra"]
- kfc_user_prompt -> 写入 values["extra"]（由 KFC 转成临时 USER payload）
"""

from __future__ import annotations

from copy import deepcopy
import re
import time
from typing import TYPE_CHECKING, Any

from src.app.plugin_system.api import llm_api, stream_api
from src.app.plugin_system.api.log_api import get_logger
from src.core.components.base import BaseEventHandler
from src.app.plugin_system.types import EventType
from src.kernel.event import EventDecision
from src.kernel.llm import LLMPayload, ROLE, Text

from .state import (
    SISPBodyState,
    SISPState,
    SISPStageDurations,
    advance_body_phase,
    load_body_state,
    load_state,
    save_body_state,
    save_state,
)

if TYPE_CHECKING:
    from .config import SISPConfig

logger = get_logger("sisp")

# SISPPromptInjector 处理带 stream_id 的 prompt（可按会话隔离）
_DEFAULT_CHATTER_PROMPT = "default_chatter_user_prompt"
_KFC_USER_PROMPT = "kfc_user_prompt"
_TARGET_PROMPTS = {_DEFAULT_CHATTER_PROMPT, _KFC_USER_PROMPT}


def _coerce_scope_value(value: Any) -> str:
    """将 scope 匹配相关字段规整为可比较的字符串。"""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value)
    return ""

# ── 评分提示词 ─────────────────────────────────────────────────────────────────

_SCORE_SYSTEM = (
"""
# Role
你是一个冷静、客观的交互氛围评估专家。你的任务是根据上下文，评估最新一条互动的“生理性/性暗示刺激强度”。

# Critical Rules (必读)
1. **拒绝过度解读**：严禁将日常通用词汇（如“去睡觉”、“洗澡”、“累了”、“脱衣服/换衣服”）直接视为暗示。
2. **字面意思优先**：除非上下文中存在明确的调情、肢体触碰或挑逗性语境，否则一律按字面意思评估。
3. **暗示的判定**：只有当言语伴随明显的调情意图、双关语或越界行为时，才计入 1-4 分。

# Scoring Scale (0-10)
- 0: 【纯净/日常】完全无关。例如：“晚安去睡了”、“我刚洗完澡”、“该吃饭了”。
- 1-2: 【轻微暧昧】带有好感的关心或礼貌性肢体接触。例如：摸头、握手、普通的拥抱。
- 3-4: 【隐晦暗示/调情】言语挑逗或带有性意味的试探。例如：“你穿这件真让人分心”、“想看你洗澡”。
- 5-6: 【明确挑逗/边缘触碰】触碰敏感部位或湿吻，有明显的生理唤起描写。
- 7-8: 【直接性行为】具体的性器官接触或直接的性交描写。
- 9-10: 【高强度刺激】多感官、大尺度的深度性交互描写。

# Examples for Calibration (对标示例)
- "我们去睡觉吧 (客观描述疲劳)" -> 0
- "床很大，我们要不要一起睡？(带调情语境)" -> 3
- "我想你了" -> 1
- "我想吃掉你" -> 4

# Output
仅输出 0 到 10 的整数，不输出任何其他文字。""")


def _is_deepseek_model_entry(model_entry: Any) -> bool:
    """判断模型条目是否指向 DeepSeek 提供商。"""
    if not isinstance(model_entry, dict):
        return False

    provider = str(model_entry.get("api_provider") or "").lower()
    base_url = str(model_entry.get("base_url") or "").lower()
    model_identifier = str(model_entry.get("model_identifier") or "").lower()
    return (
        "deepseek" in provider
        or "deepseek" in base_url
        or model_identifier.startswith("deepseek-")
    )


def _prepare_score_model_set(model_name: str) -> Any:
    """为评分任务准备模型集，并对 DeepSeek V4 做请求级兼容。"""
    if model_name:
        model_set = llm_api.get_model_set_by_name(
            model_name, temperature=0.3, max_tokens=30
        )
    else:
        model_set = llm_api.get_model_set_by_task("actor")

    if not isinstance(model_set, list):
        return model_set

    prepared_model_set = deepcopy(model_set)
    for model_entry in prepared_model_set:
        if not isinstance(model_entry, dict):
            continue

        # 评分只需要纯文本整数输出，统一关闭 tool_call 兼容。
        model_entry["tool_call_compat"] = False

        if not _is_deepseek_model_entry(model_entry):
            continue

        extra_params = model_entry.get("extra_params")
        if not isinstance(extra_params, dict):
            extra_params = {}
        else:
            extra_params = dict(extra_params)

        # DeepSeek V4 已切到 thinking 参数；评分任务只需要直接正文，
        # 显式关闭 thinking 可以避免正文为空而仅返回 reasoning 通道。
        # 若模型配置里额外开启了 reasoning_effort，则需先移除，避免与 disabled 冲突。
        extra_params.pop("reasoning_effort", None)
        extra_params["enable_thinking"] = False
        extra_params["thinking"] = {"type": "disabled"}
        model_entry["extra_params"] = extra_params

    return prepared_model_set


async def _resolve_score_response_text(response: Any) -> str:
    """统一提取评分响应正文；正文为空时回退到 reasoning_content。"""
    raw_text = await response
    if isinstance(raw_text, str) and raw_text.strip():
        return raw_text.strip()

    reasoning_text = getattr(response, "reasoning_content", None)
    if isinstance(reasoning_text, str) and reasoning_text.strip():
        logger.debug("sisp: 评分响应 content 为空，回退使用 reasoning_content")
        return reasoning_text.strip()

    return raw_text.strip() if isinstance(raw_text, str) else ""


def _extract_score(raw_text: str) -> int | None:
    """从模型返回文本中提取 0-10 的整数评分。"""
    matches = re.findall(r"(?<!\d)(10|[0-9])(?!\d)", raw_text)
    if not matches:
        return None

    return max(0, min(10, int(matches[-1])))


async def _score_message(stream_id: str, latest_text: str, model_name: str) -> int:
    """以近 6 条对话上下文为输入，对最新互动按性刺激程度评分（0–10）。

    Args:
        stream_id: 当前聊天流 ID，用于拉取历史消息（近 5 条 + 最新 = 共 6 条）
        latest_text: 最新一条用户消息文本
        model_name: 配置中指定的模型名称（空字符串时使用默认任务模型）

    Returns:
        0–10 的整数评分；调用失败时返回 3（保守回退）
    """
    try:
        history = await stream_api.get_stream_messages(stream_id, limit=5)

        context_lines: list[str] = []
        for msg in history:
            role_label = "助手" if getattr(msg, "sender_role", "") == "bot" else "用户"
            text = str(getattr(msg, "processed_plain_text", None) or getattr(msg, "content", "") or "").strip()
            if text:
                context_lines.append(f"[{role_label}] {text[:300]}")

        context_lines.append(f"[用户（最新，请重点评估此条）] {latest_text[:400]}")
        context_text = "\n".join(context_lines)

        model_set = _prepare_score_model_set(model_name)

        request = llm_api.create_llm_request(model_set, "sisp_score")
        request.add_payload(LLMPayload(ROLE.SYSTEM, Text(_SCORE_SYSTEM)))
        request.add_payload(LLMPayload(ROLE.USER, Text(context_text)))

        response = await request.send(stream=False)
        raw = await _resolve_score_response_text(response)

        score = _extract_score(raw)
        if score is not None:
            return score
    except Exception as exc:
        logger.warning(f"sisp: 评分调用失败，回退为 3。原因: {exc}")

    return 3


# ── 作用范围检查 ──────────────────────────────────────────────────────────────


async def _get_runtime_stream_info(stream_id: str) -> dict[str, Any] | None:
    """从已存在的运行时流或数据库记录兜底恢复最小匹配信息。"""
    try:
        from src.core.managers.stream_manager import get_stream_manager

        stream_manager = get_stream_manager()
        chat_stream = stream_manager._streams.get(stream_id)
        if chat_stream is None:
            chat_stream = await stream_manager.build_stream_from_database(stream_id)
            if chat_stream is None:
                return None
            stream_manager._streams[stream_id] = chat_stream

        context = chat_stream.context

        latest_message = None
        if context.unread_messages:
            latest_message = context.unread_messages[-1]
        elif context.history_messages:
            latest_message = context.history_messages[-1]
        else:
            latest_message = context.current_message

        sender_id = ""
        group_id = ""
        if latest_message is not None:
            sender_id = str(getattr(latest_message, "sender_id", "") or "")
            group_id = str(getattr(latest_message, "group_id", "") or "")
            if not group_id:
                group_id = str(latest_message.extra.get("group_id", "") or "")

        return {
            "stream_id": chat_stream.stream_id,
            "platform": chat_stream.platform,
            "chat_type": chat_stream.chat_type,
            "sender_id": sender_id,
            "group_id": group_id or None,
        }
    except Exception as exc:
        logger.debug(
            f"sisp: 运行时流信息兜底失败，stream_id={stream_id!r}, error={exc}"
        )
        return None


async def _get_scope_stream_info(stream_id: str) -> dict[str, Any] | None:
    """获取作用范围判断所需的流信息，必要时回退到运行时流。"""
    info = await stream_api.get_stream_info(stream_id)
    if info is None:
        fallback_info = await _get_runtime_stream_info(stream_id)
        if fallback_info is not None:
            logger.debug(
                f"sisp: stream_info 缺失，使用运行时流信息兜底 stream_id={stream_id!r}"
            )
        return fallback_info

    chat_type = str(info.get("chat_type") or "")
    platform = str(info.get("platform") or "")
    if chat_type == "group" and platform and info.get("group_id") not in (None, ""):
        return info
    if chat_type == "private" and platform and info.get("sender_id") not in (None, ""):
        return info

    fallback_info = await _get_runtime_stream_info(stream_id)

    if fallback_info is None:
        return info

    merged_info = dict(info)
    for key in ("platform", "chat_type", "sender_id", "group_id"):
        if merged_info.get(key) in (None, "") and fallback_info.get(key) not in (None, ""):
            merged_info[key] = fallback_info[key]

    if merged_info != info:
        logger.debug(
            f"sisp: stream_info 字段不完整，已合并运行时流信息 stream_id={stream_id!r}"
        )

    return merged_info


def _build_scope_info_from_message(message: Any) -> dict[str, Any] | None:
    """从当前消息直接提取作用范围匹配所需信息。"""
    stream_id = _coerce_scope_value(getattr(message, "stream_id", ""))
    extra = getattr(message, "extra", None)
    extra_dict = extra if isinstance(extra, dict) else {}
    platform = _coerce_scope_value(getattr(message, "platform", "")) or _coerce_scope_value(
        extra_dict.get("platform", "")
    )
    sender_id = _coerce_scope_value(getattr(message, "sender_id", "")) or _coerce_scope_value(
        extra_dict.get("sender_id", "")
    )
    group_id = _coerce_scope_value(
        getattr(message, "group_id", "")
    ) or _coerce_scope_value(
        extra_dict.get("group_id", "")
    )
    chat_type = _coerce_scope_value(getattr(message, "chat_type", "")) or _coerce_scope_value(
        extra_dict.get("chat_type", "")
    )

    if not chat_type:
        if group_id:
            chat_type = "group"
        elif sender_id:
            chat_type = "private"

    if not stream_id or not platform or not chat_type:
        return None

    return {
        "stream_id": stream_id,
        "platform": platform,
        "chat_type": chat_type,
        "sender_id": sender_id,
        "group_id": group_id or None,
    }


def _scope_matches(
    info: dict[str, Any],
    *,
    stream_id: str,
    config: "SISPConfig",
) -> bool:
    """按配置判断给定流信息是否命中作用范围。"""
    scope_mode = str(config.scope.mode or "whitelist").strip().lower()
    group_targets = config.scope.group_targets
    user_targets = config.scope.user_targets

    if scope_mode not in {"whitelist", "blacklist"}:
        scope_mode = "whitelist"

    chat_type = str(info.get("chat_type") or "")

    if scope_mode == "whitelist" and not group_targets and not user_targets:
        return False

    if chat_type == "group" and group_targets:
        group_id = str(info.get("group_id") or "")
        return group_id in group_targets if scope_mode == "whitelist" else group_id not in group_targets

    if chat_type == "group":
        return scope_mode == "blacklist"

    if chat_type == "private" and user_targets:
        from src.core.models.stream import ChatStream

        sender_id = str(info.get("sender_id") or "")
        if sender_id:
            matched = sender_id in user_targets
            return matched if scope_mode == "whitelist" else not matched

        platform = str(info.get("platform", ""))
        for uid in user_targets:
            try:
                expected = ChatStream.generate_stream_id(platform=platform, user_id=uid)
                if expected == stream_id:
                    return scope_mode == "whitelist"
            except ValueError:
                continue
        return scope_mode == "blacklist"

    if chat_type == "private":
        return scope_mode == "blacklist"

    return scope_mode == "blacklist"


async def _is_message_in_scope(message: Any, config: "SISPConfig") -> bool:
    """用当前消息直接判断是否命中插件作用范围。

    消息入口阶段不能触发 runtime stream 兜底，否则会在分发器建流前
    意外创建缺少 platform 的残缺流对象，进而污染后续会话状态。
    """
    info = _build_scope_info_from_message(message)
    if info is not None:
        return _scope_matches(info, stream_id=str(info["stream_id"]), config=config)

    extra = getattr(message, "extra", None)
    extra_dict = extra if isinstance(extra, dict) else {}
    logger.debug(
        "sisp: 消息作用域信息不完整，跳过 runtime stream 兜底 "
        f"stream_id={_coerce_scope_value(getattr(message, 'stream_id', ''))!r} "
        f"platform={_coerce_scope_value(getattr(message, 'platform', '')) or _coerce_scope_value(extra_dict.get('platform', ''))!r} "
        f"chat_type={_coerce_scope_value(getattr(message, 'chat_type', '')) or _coerce_scope_value(extra_dict.get('chat_type', ''))!r} "
        f"sender_id={_coerce_scope_value(getattr(message, 'sender_id', '')) or _coerce_scope_value(extra_dict.get('sender_id', ''))!r} "
        f"group_id={_coerce_scope_value(getattr(message, 'group_id', '')) or _coerce_scope_value(extra_dict.get('group_id', ''))!r}"
    )
    return False


async def _is_in_scope(stream_id: str, config: "SISPConfig") -> bool:
    """判断当前会话是否在 sisp 的配置作用范围内。

    - whitelist: 填写的对象生效；两个列表都空时不生效
    - blacklist: 填写的对象屏蔽；两个列表都空时全部生效
    """
    info = await _get_scope_stream_info(stream_id)
    if info is None:
        return False

    return _scope_matches(info, stream_id=stream_id, config=config)


def _is_fatigue_phase(body_state: SISPBodyState) -> bool:
    """判断当前身体状态是否属于疲劳期。"""
    return body_state.body_phase in {"extreme_fatigue", "general_fatigue"}


def _entry_threshold(body_state: SISPBodyState, config: "SISPConfig") -> int:
    """根据身体状态决定进入性爱模式所需的连续高分次数。"""
    thresholds = config.thresholds
    if body_state.body_phase == "high_desire":
        return max(1, thresholds.high_desire_entry_required_count)
    return max(1, thresholds.entry_required_count)


def _climax_event_for_y(y: float, config: "SISPConfig") -> str:
    """根据配置的 y 值阈值选择高潮事件。"""
    thresholds = config.thresholds
    if y <= thresholds.mild_climax_y_max:
        return "mild_climax"
    if y <= thresholds.normal_climax_y_max:
        return "normal_climax"
    return "intense_climax"


def _is_broken_stage(n: int, config: "SISPConfig") -> bool:
    """判断当前 n 是否进入玩坏期。"""
    enjoyment_count = max(0, config.thresholds.enjoyment_climax_count)
    return n > enjoyment_count + 1


def _is_beg_stage(n: int, config: "SISPConfig") -> bool:
    """判断当前 n 是否为享受期结束后的单轮求饶期。"""
    enjoyment_count = max(0, config.thresholds.enjoyment_climax_count)
    return n == enjoyment_count + 1


def _advance_beg_to_broken(state: SISPState, config: "SISPConfig") -> bool:
    """将已宣告求饶的状态推进到玩坏期，返回是否发生推进。"""
    if not (
        state.in_sex_mode
        and state.beg_announced
        and state.current_event is None
        and _is_beg_stage(state.n, config)
    ):
        return False

    enjoyment_count = max(0, config.thresholds.enjoyment_climax_count)
    state.n = enjoyment_count + 2
    return True


def _clear_stale_climax_event(state: SISPState) -> bool:
    """清理上一轮未消费的一次性高潮事件，返回是否发生清理。"""
    if state.current_event not in {"mild_climax", "normal_climax", "intense_climax"}:
        return False

    state.current_event = None
    return True


def _body_phase_prompt(
    config: "SISPConfig",
    body_state: SISPBodyState,
    *,
    in_sex_mode: bool,
) -> str | None:
    """根据身体状态获取对应的提示词。"""
    prompts = config.prompts

    if body_state.body_phase == "extreme_fatigue":
        return prompts.extreme_fatigue_passive if in_sex_mode else prompts.extreme_fatigue_idle

    if body_state.body_phase == "general_fatigue":
        return prompts.general_fatigue_passive if in_sex_mode else prompts.general_fatigue_idle

    if body_state.body_phase == "mild_desire":
        return prompts.mild_desire

    if body_state.body_phase == "high_desire":
        return prompts.high_desire

    return None


def _get_stage_durations(config: "SISPConfig") -> SISPStageDurations:
    """从插件配置中读取身体阶段持续时长。"""
    timing = config.timing
    return SISPStageDurations(
        extreme_fatigue_hours=max(0.0, timing.extreme_fatigue_hours),
        general_fatigue_hours=max(0.0, timing.general_fatigue_hours),
        none_cooldown_hours=max(0.0, timing.none_cooldown_hours),
        mild_desire_hours=max(0.0, timing.mild_desire_hours),
    )


def _append_prompt_injection(
    values: dict[str, Any],
    *,
    prompt_name: str,
    injected: str,
) -> None:
    """按目标 prompt 的槽位约定写入注入文本。"""
    if prompt_name == _DEFAULT_CHATTER_PROMPT:
        existing = str(values.get("extra", "") or "")
        values["extra"] = existing + "\n" + injected if existing else injected
        return

    if prompt_name == _KFC_USER_PROMPT:
        block = f"# SISP 状态\n{injected}"
        existing = str(values.get("extra", "") or "")
        values["extra"] = (
            existing + "\n\n" + block if existing else block
        )


def _format_state_snapshot(state: SISPState, body_state: SISPBodyState) -> str:
    """格式化当前状态快照，便于日志对齐运行时注入结果。"""
    return (
        f"in_sex={state.in_sex_mode} body={body_state.body_phase} "
        f"n={state.n} foreplay_count={state.foreplay_count} "
        f"climax_count={state.climax_count} low_score_count={state.low_score_count} "
        f"x={state.x:.1f} U={state.U:.1f} k={state.k:.2f} "
        f"event={state.current_event!r}"
    )


async def _save_state_with_snapshot_log(
    stream_id: str,
    state: SISPState,
    body_state: SISPBodyState,
    *,
    reason: str,
) -> None:
    """保存状态并记录结算后快照日志。"""
    await save_state(stream_id, state)
    logger.info(
        f"[sisp] stream={stream_id[:8]} 结算后状态 reason={reason} "
        f"{_format_state_snapshot(state, body_state)}"
    )


# ── Handler 1：状态推进 ───────────────────────────────────────────────────────


class SISPStateHandler(BaseEventHandler):
    """消息接收时推进 SISP 状态机。

    对每条进入作用范围的消息执行性刺激程度评分（近 6 条上下文），
    按照完整生命周期逻辑更新状态，并将待注入事件写入 current_event 字段。
    """

    handler_name: str = "sisp_state_handler"
    handler_description: str = "SISP 状态机推进器，评分并更新会话状态"
    weight: int = 20
    intercept_message: bool = False
    init_subscribe: list[str] = [EventType.ON_MESSAGE_RECEIVED]

    def _get_config(self) -> "SISPConfig":
        """获取插件配置实例。"""
        from .config import SISPConfig
        return self.plugin.config if isinstance(self.plugin.config, SISPConfig) else SISPConfig()

    async def execute(
        self,
        event_name: str,
        params: dict[str, Any],
    ) -> tuple[EventDecision, dict[str, Any]]:
        """处理消息接收事件，推进状态机。"""
        config = self._get_config()
        if not config.plugin.enabled:
            return EventDecision.SUCCESS, params

        message = params.get("message")
        if message is None:
            return EventDecision.SUCCESS, params

        stream_id: str = getattr(message, "stream_id", "") or ""
        if not stream_id:
            return EventDecision.SUCCESS, params

        if not await _is_message_in_scope(message, config):
            return EventDecision.SUCCESS, params

        # 仅处理非 bot 消息（不对自己发出的消息评分）
        if getattr(message, "sender_role", "") == "bot":
            return EventDecision.SUCCESS, params

        text = str(
            getattr(message, "processed_plain_text", None)
            or getattr(message, "content", "")
            or ""
        ).strip()
        if not text:
            return EventDecision.SUCCESS, params

        state = await load_state(stream_id)
        loaded_body_state = await load_body_state(stream_id)
        stage_durations = _get_stage_durations(config)
        body_state = advance_body_phase(loaded_body_state, durations=stage_durations)
        if _clear_stale_climax_event(state):
            await save_state(stream_id, state)
        if _advance_beg_to_broken(state, config):
            await save_state(stream_id, state)
        model_name = config.plugin.score_model

        if body_state.to_dict() != loaded_body_state.to_dict():
            await save_body_state(stream_id, body_state)

        score = await _score_message(stream_id, text, model_name)
        thresholds = config.thresholds

        logger.info(
            f"[sisp] stream={stream_id[:8]} 评分前状态 score={score} "
            f"{_format_state_snapshot(state, body_state)}"
        )

        # ── 阶段一：未进入性爱模式 ──────────────────────────────────────────
        if not state.in_sex_mode:
            if score >= thresholds.entry_score_min:
                state.foreplay_count += 1
                if state.foreplay_count >= _entry_threshold(body_state, config):
                    state = SISPState.new_sex_session()
                    if body_state.body_phase in {"mild_desire", "high_desire"}:
                        body_state = SISPBodyState()
                        await save_body_state(stream_id, body_state)
                    logger.info(f"[sisp] stream={stream_id[:8]} 进入性爱模式")
                    await _save_state_with_snapshot_log(
                        stream_id,
                        state,
                        body_state,
                        reason="enter_sex_mode",
                    )
                    return EventDecision.SUCCESS, params
            else:
                state.foreplay_count = 0

            await _save_state_with_snapshot_log(
                stream_id,
                state,
                body_state,
                reason="pre_sex_progress",
            )
            return EventDecision.SUCCESS, params

        # ── 阶段二：性爱模式中 ──────────────────────────────────────────────

        passive_mode = _is_fatigue_phase(body_state)

        # 优先判定：低意愿检测
        if score < thresholds.low_score_max_exclusive:
            state.low_score_count += 1
            logger.info(
                f"[sisp] stream={stream_id[:8]} 低意愿累计 low_score_count={state.low_score_count} "
                f"body_before={body_state.body_phase} passive={passive_mode} "
                f"n={state.n} climax_count={state.climax_count}"
            )
            if state.low_score_count >= thresholds.low_score_exit_count:
                if not passive_mode:
                    if (
                        state.climax_count == 0
                        and body_state.body_phase in {"none", "mild_desire", "high_desire"}
                    ):
                        exit_reason = "no_climax_early_exit"
                        body_state = SISPBodyState(
                            body_phase="high_desire",
                            phase_until=0.0,
                        )
                    elif _is_broken_stage(state.n, config):
                        exit_reason = "broken_exit"
                        body_state = SISPBodyState(
                            body_phase="extreme_fatigue",
                            phase_until=time.time() + stage_durations.extreme_fatigue_seconds,
                        )
                    else:
                        exit_reason = "normal_exit"
                        body_state = SISPBodyState(
                            body_phase="general_fatigue",
                            phase_until=time.time() + stage_durations.general_fatigue_seconds,
                        )
                    logger.info(
                        f"[sisp] stream={stream_id[:8]} 退出判定 reason={exit_reason} "
                        f"body_after={body_state.body_phase} n={state.n} "
                        f"climax_count={state.climax_count}"
                    )
                    await save_body_state(stream_id, body_state)
                else:
                    logger.info(
                        f"[sisp] stream={stream_id[:8]} 退出判定 reason=passive_exit "
                        f"body_after={body_state.body_phase} n={state.n} "
                        f"climax_count={state.climax_count}"
                    )

                state = SISPState()  # 完全重置，x/n/k/U/beg_announced 全部归零
                logger.info(
                    f"[sisp] stream={stream_id[:8]} 用户低意愿，退出性爱模式，"
                    f"状态已重置 body={body_state.body_phase}"
                )
                await _save_state_with_snapshot_log(
                    stream_id,
                    state,
                    body_state,
                    reason="exit_sex_mode",
                )
                return EventDecision.SUCCESS, params

            await _save_state_with_snapshot_log(
                stream_id,
                state,
                body_state,
                reason="low_intent_pending_exit",
            )
            return EventDecision.SUCCESS, params
        else:
            state.low_score_count = 0

        if passive_mode:
            logger.info(f"[sisp] stream={stream_id[:8]} 疲劳期被动配合，跳过 n/x/高潮计算")
            await _save_state_with_snapshot_log(
                stream_id,
                state,
                body_state,
                reason="passive_mode_skip",
            )
            return EventDecision.SUCCESS, params

        # 状态分流：基于 n
        # n 表示完成的享受期高潮次数；达到配置上限后的下一轮求饶，再下一轮玩坏。
        if _is_broken_stage(state.n, config):
            # C. 玩坏期：broken 常驻注入，但高潮事件仍可循环触发（n 不再递增）
            state.x += score
            logger.info(f"[sisp] stream={stream_id[:8]} 玩坏期积累 x={state.x:.1f} / U={state.U:.1f}")

            if state.x >= state.U:
                y = state.k * state.x
                state.current_event = _climax_event_for_y(y, config)

                if config.plugin.debug_log:
                    logger.info(
                        f"[sisp] stream={stream_id[:8]} 玩坏期高潮 "
                        f"y={y:.2f} event={state.current_event}"
                    )

                state.climax_count += 1
                state.reset_after_climax()  # 重置 x/k/U，n 不变

        elif _is_beg_stage(state.n, config):
            # B. 求饶期：先宣告 beg，下一条消息再升入玩坏期。
            # 不能在同一轮既把 n 升为玩坏期（触发 broken 注入）又设置 current_event="beg"，
            # 否则注入器会同帧注入互相矛盾的 broken + beg 提示词。
            if not state.beg_announced:
                # 首次进入求饶期：只公告事件，n 保持在上限后的第一个值。
                state.current_event = "beg"
                state.beg_announced = True
            elif score > thresholds.beg_progress_score_min_exclusive:
                # beg 已在上一轮宣告，本轮才升入玩坏期
                state.n += 1  # → 玩坏期（下轮生效）

        else:
            # A. 享受期：正常积累，直到 n 达到求饶期阈值
            state.x += score
            logger.info(f"[sisp] stream={stream_id[:8]} 享受期积累 x={state.x:.1f} / U={state.U:.1f}")

            if state.x >= state.U:
                y = state.k * state.x
                state.current_event = _climax_event_for_y(y, config)

                if config.plugin.debug_log:
                    logger.info(
                        f"[sisp] stream={stream_id[:8]} 高潮触发 "
                        f"y={y:.2f} event={state.current_event}"
                    )

                state.climax_count += 1
                state.n += 1
                state.reset_after_climax()

        await _save_state_with_snapshot_log(
            stream_id,
            state,
            body_state,
            reason="sex_mode_progress",
        )
        return EventDecision.SUCCESS, params


# ── Handler 2：提示词注入 ─────────────────────────────────────────────────────


class SISPPromptInjector(BaseEventHandler):
    """在支持的 prompt 构建前注入 SISP 状态标签。

    注入规则：
    - in_sex_mode=True 时，优先注入 atmosphere。
    - 疲劳期进入性交时，只注入 fatigue passive 提示词，不再注入 broken 与一次性事件。
    - 正常性交时，仍按 n / current_event 注入 broken 与高潮/beg 事件。
    - in_sex_mode=False 时，仅根据身体状态注入 fatigue/desire 提示词。

    注入槽位：
    - default_chatter_user_prompt -> values["extra"]
    - kfc_user_prompt -> values["extra"]（由 KFC 转成独立 USER payload）

    两种事件都携带 stream_id，因此都能实现会话级别隔离。
    """

    handler_name: str = "sisp_prompt_injector"
    handler_description: str = "SISP 提示词注入器，按当前状态向 user prompt extra 追加标签"
    weight: int = 10
    intercept_message: bool = False
    init_subscribe: list[str] = ["on_prompt_build"]

    def _get_config(self) -> "SISPConfig":
        """获取插件配置实例。"""
        from .config import SISPConfig
        return self.plugin.config if isinstance(self.plugin.config, SISPConfig) else SISPConfig()

    async def execute(
        self,
        event_name: str,
        params: dict[str, Any],
    ) -> tuple[EventDecision, dict[str, Any]]:
        """处理 on_prompt_build 事件，按状态向 extra 注入提示词标签。"""
        prompt_name = str(params.get("name") or "")
        if prompt_name not in _TARGET_PROMPTS:
            return EventDecision.SUCCESS, params

        config = self._get_config()
        if not config.plugin.enabled:
            return EventDecision.SUCCESS, params

        values = params.get("values", {})
        stream_id = str(values.get("stream_id", "") or "")
        if not stream_id:
            return EventDecision.SUCCESS, params

        if not await _is_in_scope(stream_id, config):
            return EventDecision.SUCCESS, params

        state = await load_state(stream_id)
        loaded_body_state = await load_body_state(stream_id)
        body_state = advance_body_phase(
            loaded_body_state,
            durations=_get_stage_durations(config),
        )
        beg_promoted = _advance_beg_to_broken(state, config)

        if body_state.to_dict() != loaded_body_state.to_dict():
            await save_body_state(stream_id, body_state)

        p = config.prompts
        inject_parts: list[str] = []
        event = None

        if state.in_sex_mode:
            inject_parts.append(p.atmosphere)

            body_prompt = _body_phase_prompt(config, body_state, in_sex_mode=True)
            if body_prompt:
                inject_parts.append(body_prompt)

            passive_mode = _is_fatigue_phase(body_state)
            if not passive_mode:
                if _is_broken_stage(state.n, config):
                    inject_parts.append(p.broken)

                event = state.current_event
                event_map: dict[str, str] = {
                    "mild_climax": p.mild_climax,
                    "normal_climax": p.normal_climax,
                    "intense_climax": p.intense_climax,
                    "beg": p.beg,
                }
                if event and event in event_map:
                    inject_parts.append(event_map[event])
                    state.current_event = None
                    beg_promoted = _advance_beg_to_broken(state, config) or beg_promoted
                    await save_state(stream_id, state)

                elif beg_promoted:
                    await save_state(stream_id, state)
        else:
            body_prompt = _body_phase_prompt(config, body_state, in_sex_mode=False)
            if not body_prompt:
                return EventDecision.SUCCESS, params
            inject_parts.append(body_prompt)

        injected = "\n".join(inject_parts)
        _append_prompt_injection(values, prompt_name=prompt_name, injected=injected)

        if config.plugin.debug_log:
            logger.info(
                f"[sisp] prompt={prompt_name} 注入快照 stream={stream_id[:8]} "
                f"{_format_state_snapshot(state, body_state)} "
                f"event_before_consume={event!r} inject_parts={inject_parts!r}"
            )

        return EventDecision.SUCCESS, params
