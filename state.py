"""sisp 状态数据类与持久化封装。

每个聊天流（stream_id）维护两份独立状态：
- SISPState: 局内状态，每次退出性爱后重置。
- SISPBodyState: 跨会话身体状态，由时间推进阶段，用于影响后续会话。
"""

from __future__ import annotations

import random
import time
from dataclasses import asdict, dataclass

from src.app.plugin_system.api import storage_api

_STORE_NAME = "sisp"
_BODY_KEY_PREFIX = "body_"

_SECONDS_PER_HOUR = 3600.0


@dataclass(frozen=True)
class SISPStageDurations:
    """SISP 身体阶段持续时长配置。"""

    extreme_fatigue_hours: float = 24.0
    general_fatigue_hours: float = 24.0
    none_cooldown_hours: float = 72.0
    mild_desire_hours: float = 48.0

    @property
    def extreme_fatigue_seconds(self) -> float:
        """过度疲倦阶段时长，单位秒。"""
        return self.extreme_fatigue_hours * _SECONDS_PER_HOUR

    @property
    def general_fatigue_seconds(self) -> float:
        """一般疲倦阶段时长，单位秒。"""
        return self.general_fatigue_hours * _SECONDS_PER_HOUR

    @property
    def none_cooldown_seconds(self) -> float:
        """none 冷静恢复阶段时长，单位秒。"""
        return self.none_cooldown_hours * _SECONDS_PER_HOUR

    @property
    def mild_desire_seconds(self) -> float:
        """mild_desire 阶段时长，单位秒。"""
        return self.mild_desire_hours * _SECONDS_PER_HOUR


@dataclass
class SISPState:
    """单会话 SISP 状态。

    Attributes:
        in_sex_mode: 是否处于性爱模式
        n: 高潮计数器（初始为 1）
        climax_count: 当前性爱会话内已触发的高潮次数
        x: 当前积累值
        k: 敏感系数
        U: 耐受上限
        foreplay_count: 连续高意愿轮次计数（用于触发启动）
        low_score_count: 连续低行为分轮次计数（用于触发终止）
        current_event: 一次性事件标签（高潮/求饶），由 on_prompt_build 消费后清空；
                      长期状态（氛围/玩坏）由 in_sex_mode 与 n > 4 直接控制，不走此字段。
        beg_announced: 是否已通知过求饶状态（防止每轮重复注入）
    """

    in_sex_mode: bool = False
    n: int = 1
    climax_count: int = 0
    x: float = 0.0
    k: float = 1.0
    U: float = 50.0
    foreplay_count: int = 0
    low_score_count: int = 0
    current_event: str | None = None
    beg_announced: bool = False

    @classmethod
    def new_sex_session(cls) -> "SISPState":
        """创建一个全新的性爱会话状态（随机初始化 k 和 U）。"""
        return cls(
            in_sex_mode=True,
            n=1,
            climax_count=0,
            x=0.0,
            k=round(random.uniform(0.5, 3.0), 4),
            U=round(random.uniform(30.0, 100.0), 2),
            foreplay_count=0,
            low_score_count=0,
            current_event=None,
        )

    def reset_after_climax(self) -> None:
        """高潮后重置局部变量，保留 n（已在外部递增）。"""
        self.x = 0.0
        self.k = round(random.uniform(0.5, 3.0), 4)
        self.U = round(random.uniform(30.0, 100.0), 2)

    def to_dict(self) -> dict:
        """序列化为可 JSON 存储的字典。"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "SISPState":
        """从存储字典反序列化。"""
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


async def load_state(stream_id: str) -> SISPState:
    """从 JSON 存储加载指定会话的状态，不存在则返回初始状态。"""
    data = await storage_api.load_json(_STORE_NAME, stream_id)
    if data is None:
        return SISPState()
    return SISPState.from_dict(data)


async def save_state(stream_id: str, state: SISPState) -> None:
    """将指定会话的状态持久化到 JSON 存储。"""
    await storage_api.save_json(_STORE_NAME, stream_id, state.to_dict())


@dataclass
class SISPBodyState:
    """跨会话身体状态。

    Attributes:
        body_phase: 身体阶段。
            - none: 正常状态；当 phase_until > 0 时，表示正常但处于冷静恢复倒计时。
            - extreme_fatigue: 过度疲倦。
            - general_fatigue: 一般疲倦。
            - mild_desire: 有点想做爱。
            - high_desire: 性欲高涨。
        phase_until: 当前阶段结束时间戳；0 表示无到期时间。
    """

    body_phase: str = "none"
    phase_until: float = 0.0

    def to_dict(self) -> dict:
        """序列化为可 JSON 存储的字典。"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "SISPBodyState":
        """从存储字典反序列化。"""
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


def advance_body_phase(
    body_state: SISPBodyState,
    durations: SISPStageDurations | None = None,
    now: float | None = None,
) -> SISPBodyState:
    """按时间推进身体阶段。

    采用循环推进，避免长时间无消息后只前进一步。
    链路：
    extreme_fatigue -> general_fatigue -> none(倒计时) -> mild_desire -> high_desire
    """
    current = SISPBodyState.from_dict(body_state.to_dict())
    stage_durations = durations or SISPStageDurations()
    current_time = time.time() if now is None else now

    while current.phase_until > 0 and current_time >= current.phase_until:
        phase_end = current.phase_until

        if current.body_phase == "extreme_fatigue":
            current.body_phase = "general_fatigue"
            current.phase_until = phase_end + stage_durations.general_fatigue_seconds
            continue

        if current.body_phase == "general_fatigue":
            current.body_phase = "none"
            current.phase_until = phase_end + stage_durations.none_cooldown_seconds
            continue

        if current.body_phase == "none":
            current.body_phase = "mild_desire"
            current.phase_until = phase_end + stage_durations.mild_desire_seconds
            continue

        if current.body_phase == "mild_desire":
            current.body_phase = "high_desire"
            current.phase_until = 0.0
            continue

        break

    return current


async def load_body_state(stream_id: str) -> SISPBodyState:
    """加载指定会话的跨会话身体状态。"""
    data = await storage_api.load_json(_STORE_NAME, f"{_BODY_KEY_PREFIX}{stream_id}")
    if data is None:
        return SISPBodyState()
    return SISPBodyState.from_dict(data)


async def save_body_state(stream_id: str, body_state: SISPBodyState) -> None:
    """持久化指定会话的跨会话身体状态。"""
    await storage_api.save_json(
        _STORE_NAME,
        f"{_BODY_KEY_PREFIX}{stream_id}",
        body_state.to_dict(),
    )
