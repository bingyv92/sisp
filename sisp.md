# 性交互动模拟插件架构设计文档 (SISP)

这是一个完整的、结构化的程序架构文档，整合了我们讨论过的所有数学模型、逻辑分支、状态流转和提示词策略。此文档可直接用于开发参考。

## 0. 插件应用范围 (Scope Config)

本插件仅在以下指定会话中生效，其他会话中所有逻辑均不激活，且需要用户自行配置。

| 类型 | ID |
| :--- | :--- |
| **群聊** | `123` |
| **私聊** | `321` |

## 1. 核心变量定义 (State Variables)

本系统遵循**“极简可靠”**原则，仅使用以下 5 个核心变量控制全流程，不引入额外的体力槽或复杂属性。

| 变量符号 | 名称 | 作用域 | 定义与逻辑 |
| :---: | :--- | :--- | :--- |
| **n** | 高潮计数器 | 全局 | **疲劳阀门**。初始为 1。每次高潮结算后 +1。决定角色处于“享受”、“求饶”还是“玩坏”阶段。 |
| **x** | 当前积累值 | 局部 | **进度条**。初始为 0。每轮根据 LLM 对用户输入的评分 (S) 累加：新的x值 = 旧的x值 + 评分S。 |
| **k** | 敏感系数 | 局部 | **随机惊喜**。每轮高潮后重新生成：k 在 0.5 到 3 之间均匀随机取值。决定本轮高潮的爆发强度。 |
| **U** | 耐受上限 | 局部 | **时长控制**。每轮高潮后重新生成：U 在 30 到 100 之间均匀随机取值。决定本轮互动需要积累多久。 |
| **y** | 强度结算值 | 瞬时 | **结果判定**。仅在高潮时刻计算：y = k 乘以 x。用于判定高潮等级。 |

## 2. 数学模型与概率分布 (The Math Core)

基于随机变量 y = k 乘以 x，为了保证体验的层级感，设定界限 a 和 b。

*   **模型公式**：
# 随机变量 y 的分位数计算（y 等于 k 乘以 x）

## 问题描述
考虑随机变量 y = k 乘以 x，其中：
- k 在 0.5 到 3 之间均匀随机分布
- x 在 0 到 U 之间均匀随机分布，而 U 本身在 30 到 100 之间均匀随机分布
- k、U 和 x 三者相互独立

需要确定两个界限 a 和 b，使得：
- y 小于等于 a 的概率为 20%（0.2）
- y 大于 a 且小于等于 b 的概率为 50%（0.5）
- y 大于 b 的概率为 30%（0.3）

## 求解方法
首先推导 y 的累积分布函数（记为 F(y)），然后通过数值求解方程：
- F(a) = 0.2（即 20% 的 y 值小于等于 a）
- F(b) = 0.7（即 70% 的 y 值小于等于 b，因为 0.2 + 0.5 = 0.7）

通过计算得到 a 和 b 的近似值。

## 结果
- a 约等于 16.25
- b 约等于 71.26

因此，界限为 a = 16.25，b = 71.26，分别对应：
- 轻度（Mild）：y 小于等于 16.25 的概率为 20%
- 普通（Normal）：y 大于 16.25 且小于等于 71.26 的概率为 50%
- 高强度（Intense）：y 大于 71.26 的概率为 30%

## 3. 完整生命周期逻辑 (Lifecycle Logic)

### 阶段一：启动判定 (Initialization)
在非性交模式 (`in_sex_mode = False`) 下，后台对用户输入进行**性刺激程度评分**（0 到 10，基于近 6 条对话上下文，重点评估最新一条）。

*   **判定逻辑**：
    *   **累积触发**：评分在 7 到 10 之间（暧昧前戏）时，前戏计数器加 1（`foreplay_count += 1`）。若计数器大于等于 2 则启动。
    *   **重置**：评分小于 7 时，前戏计数器重置为 0（`foreplay_count = 0`）。
*   **启动动作**：
    *   `in_sex_mode = True`
    *   `n = 1`, `x = 0`
    *   随机生成初始 k, U

### 阶段二：过程循环 (The Loop)
进入模式后，每回合执行以下逻辑树：

1.  **优先判定：用户终止检测**
    *   继续依靠 LLM 对用户本轮输入的评分（评分范围 0 到 10）进行检测。
    *   **低意愿逻辑**：当本轮评分 **< 5** 时，**本次评分不累加**至 x，同时低意愿计数器加 1（`low_score_count += 1`）。
    *   **终止触发**：当 `low_score_count >= 2`（即第二次评分 < 5）时，触发终止逻辑：`in_sex_mode = False`，退出模式（LLM 自行完成过渡，无需额外提示词注入）。
    *   **重置**：当本轮评分 **>= 5** 时，`low_score_count` 重置为 0，正常继续流程。

2.  **状态分流判定 (基于 n)**
    *   **A. 享受期 (n < 3)**：正常执行“步骤 3：进度计算”。
    *   **B. 求饶期 (n = 3)**：
        *   逻辑：达到体力极限。
        *   若 x 约等于 0（刚开始）：强制发送 `[求饶提示词]`。**不累加 x**。
        *   若用户回复且评分大于 0：**无视求饶，强行继续**，跳转至 C。
        *   若用户同意停止：跳转至"用户终止"。
    *   **C. 玩坏期 (n > 3)**：
        *   逻辑：防线崩溃，意识模糊。
        *   强制发送 `[玩坏提示词]`。
        *   累加 x（仅用于维持互动，不再触发正常高潮，或触发仅有生理抽搐的假性高潮）。

3.  **进度计算与高潮 (仅针对 n < 3)**
    *   **累加**：x 加上 LLM 对输入的评分。
    *   **判定**：
        *   若 x 小于 U：发送 `[氛围提示词]`（基于 x 除以 U 的进度比例）。
        *   若 x 大于等于 U：**触发高潮**。
            *   计算 y = k 乘以 x。
            *   根据 y 值发送 `[高潮提示词]`。
            *   **更新状态**：n 加 1，x 重置为 0，重新随机生成 k 和 U。

## 4. 提示词注入库 (Prompt Injection Library)

为了给主 LLM 留出发挥空间，采用 `[System Tag]` 格式注入，而非长指令。

### A. 氛围组 (Atmosphere)

全程注入单条固定提示词（无论进度如何，始终保持注入）：

`[状态：性爱过程进行中。请维持当前互动氛围，根据角色的实时状态自然回应用户。]`

### B. 高潮组 (Climax) - 仅在 x 大于等于 U 时发送

| 等级 (基于 y) | 标签 (Tag) |
| :--- | :--- |
| **轻度**（y 小于等于 16.25）| `[事件：轻度高潮。反应：轻柔颤抖，低声呻吟，缓慢释放。]` |
| **普通**（y 大于 16.25 且小于等于 71.26）| `[事件：标准高潮。反应：热烈呻吟，肌肉强烈收缩，深度满足感。]` |
| **高强**（y 大于 71.26）| `[事件：强烈高潮。反应：完全失控，视线模糊，全身痉挛，尖叫。]` |

### C. 特殊状态组 (Status) - 优先级最高

| 触发条件 | 标签 (Tag) |
| :--- | :--- |
| **求饶期** (n=3) | `[状态：体力耗尽。不想继续。]` |
| **玩坏期** (n>3) | `[状态：意识崩溃。逻辑离线，纯粹本能反应，口水横流，抽搐，言语支离破碎。]` |

## 5. 可自定义配置项 (Customizable Config)

以下提示词内容均支持用户自行修改，修改后将在整个运行周期中生效。

### 5.1 氛围组提示词

全程注入的固定提示词，可替换为任意符合当前情境的描述：

```
默认值：
[状态：性爱过程进行中。请维持当前互动氛围，根据角色状态自然回应用户。]
```

### 5.2 高潮组提示词（三档强度）

分别对应轻度、普通、高强三个结算等级，可独立自定义：

```
轻度高潮（y ≤ 16.25）默认值：
[事件：轻度高潮。反应：轻柔颤抖，低声呻吟，缓慢释放。]

普通高潮（16.25 < y ≤ 71.26）默认值：
[事件：标准高潮。反应：热烈呻吟，肌肉强烈收缩，深度满足感。]

强烈高潮（y > 71.26）默认值：
[事件：强烈高潮。反应：完全失控，视线模糊，全身痉挛，尖叫。]
```



## 6. 总结

此架构通过 n（次数）和 x（进度）的极简组合，完美覆盖了从前戏到积累到不同等级高潮到疲劳求饶到玩坏/结束的全流程，且每一阶段都有明确的数学或逻辑支撑。

---

## 7. 插件实现架构（Plugin Implementation Architecture）

本章将以上所有设计映射到 Neo-MoFox 插件系统的具体代码结构，按照框架规范可直接落地开发。

### 7.1 目录结构

```
plugins/sisp/
├── __init__.py          # 空文件，标识 Python 包
├── manifest.json        # 插件声明，系统加载入口
├── plugin.py            # 插件主类 + 组件注册
├── config.py            # 配置模型（BaseConfig）
├── state.py             # 状态数据类 + JSON 持久化封装
└── event_handler.py     # 两个 EventHandler + 内部评分函数
```

### 7.2 核心组件映射表

| 设计模块 | Neo-MoFox 组件类型 | 说明 |
| :--- | :--- | :--- |
| 启用范围（群号/私聊） | `BaseConfig` → `ScopeSection` | 等同 `prompt_injector` 的 `group_targets`/`user_targets` |
| 可自定义提示词 | `BaseConfig` → `PromptsSection` | 氛围/高潮/求饶/玩坏文本均由配置驱动，注入 system_reminder |
| 状态变量 n/x/k/U | `storage_api.save_json()` | 按 `stream_id` 隔离、跨轮次持久化 |
| 性刺激程度评分 | 内部 LLM 工具函数 | 单一评分提示词，以近 6 条上下文为输入，返回 0–10 整数 |
| 进度积累 + 高潮判定 | `SISPStateHandler`（`on_message_received`） | 消息到达时评分、更新状态、标记待注入事件 |
| 提示词注入 | `SISPPromptInjector`（`on_prompt_build`） | LLM 构建 system prompt 前读取状态、注入 `system_reminder` |

---

## 8. `manifest.json`

```json
{
  "name": "sisp",
  "version": "1.0.0",
  "description": "性交互动模拟插件，通过数学模型驱动多阶段状态机并向 LLM 动态注入提示词",
  "author": "",
  "dependencies": {
    "plugins": [],
    "components": []
  },
  "include": [
    {
      "component_type": "event_handler",
      "component_name": "sisp_state_handler",
      "dependencies": [],
      "enabled": true
    },
    {
      "component_type": "event_handler",
      "component_name": "sisp_prompt_injector",
      "dependencies": [],
      "enabled": true
    }
  ],
  "entry_point": "plugin.py",
  "min_core_version": "1.0.0",
  "python_dependencies": [],
  "dependencies_required": false
}
```

---

## 9. 配置系统实现（`config.py`）

`BaseConfig` 读取路径为 `config/plugins/sisp/config.toml`，所有提示词和作用范围均由此文件控制，修改后无需重启（热加载）。

```python
"""sisp 插件配置。

配置文件默认路径：config/plugins/sisp/config.toml
"""

from __future__ import annotations

from typing import ClassVar

from src.core.components.base.config import BaseConfig, Field, SectionBase, config_section


class SISPConfig(BaseConfig):
    """SISP 插件配置模型。"""

    config_name: ClassVar[str] = "config"
    config_description: ClassVar[str] = "性交互动模拟插件配置"

    @config_section("plugin")
    class PluginSection(SectionBase):
        """插件基础配置。"""

        enabled: bool = Field(default=True, description="是否启用插件")
        debug_log: bool = Field(
            default=False,
            description="开启后在日志中输出评分与状态变更详情，便于调试",
        )
        score_model: str = Field(
            default="",
            description=(
                "用于性刺激评分的模型名称（对应 config/model.toml 中的 name）。\n"
                "留空时回退到 model_tasks.actor 任务模型。"
            ),
        )

    @config_section("scope")
    class ScopeSection(SectionBase):
        """作用范围配置。

        - group_targets 与 user_targets 均为空 → 插件不生效（防止误操作全局激活）
        - 仅 group_targets 非空 → 只对指定群聊生效
        - 仅 user_targets 非空 → 只对指定私聊生效
        - 两者均非空 → 指定群聊 + 指定私聊的并集
        """

        group_targets: list[str] = Field(
            default_factory=list,
            description='生效的群号列表，示例：group_targets = ["123456789"]',
        )
        user_targets: list[str] = Field(
            default_factory=list,
            description='生效的私聊 QQ 号列表，示例：user_targets = ["987654321"]',
        )

    @config_section("prompts")
    class PromptsSection(SectionBase):
        """提示词内容配置（所有标签均可自定义）。"""

        atmosphere: str = Field(
            default="[状态：性爱过程进行中。请维持当前互动氛围，根据角色的实时状态自然回应用户。]",
            description="全程注入的氛围提示词（性爱进行时每轮都会注入）",
        )
        mild_climax: str = Field(
            default="[事件：轻度高潮。反应：轻柔颤抖，低声呻吟，缓慢释放。]",
            description="轻度高潮提示词（y ≤ 16.25 时触发）",
        )
        normal_climax: str = Field(
            default="[事件：标准高潮。反应：热烈呻吟，肌肉强烈收缩，深度满足感。]",
            description="标准高潮提示词（16.25 < y ≤ 71.26 时触发）",
        )
        intense_climax: str = Field(
            default="[事件：强烈高潮。反应：完全失控，视线模糊，全身痉挛，尖叫。]",
            description="强烈高潮提示词（y > 71.26 时触发）",
        )
        beg: str = Field(
            default="[状态：体力耗尽。不想继续。]",
            description="求饶期提示词（n = 3 时注入）",
        )
        broken: str = Field(
            default="[状态：意识崩溃。逻辑离线，纯粹本能反应，口水横流，抽搐，言语支离破碎。]",
            description="玩坏期提示词（n > 3 时注入）",
        )

    plugin: PluginSection = Field(default_factory=PluginSection)
    scope: ScopeSection = Field(default_factory=ScopeSection)
    prompts: PromptsSection = Field(default_factory=PromptsSection)
```

---

## 10. 状态存储模块（`state.py`）

使用 `storage_api.save_json` 将每个会话的状态以 `stream_id` 为键存入 `data/json_storage/sisp/` 目录，重启后状态自动恢复。

```python
"""sisp 状态数据类与持久化封装。

每个聊天流（stream_id）维护一份独立的 SISPState，
通过 load_state / save_state 读写 JSON 存储。
"""

from __future__ import annotations

import random
from dataclasses import asdict, dataclass, field

from src.app.plugin_system.api import storage_api

_STORE_NAME = "sisp"


@dataclass
class SISPState:
    """单会话 SISP 状态。

    Attributes:
        in_sex_mode: 是否处于性爱模式
        n: 高潮计数器（初始为 1）
        x: 当前积累值
        k: 敏感系数
        U: 耐受上限
        foreplay_count: 连续高意愿轮次计数（用于触发启动）
        low_score_count: 连续低行为分轮次计数（用于触发终止）
        current_event: 一次性事件标签（高潮/求饶），由 on_prompt_build 消费后清空；
                      长期状态（氛围/玩坏）由 in_sex_mode 与 n > 3 直接控制，不走此字段。
        beg_announced: 是否已通知过求饶状态（防止每轮重复注入）
    """

    in_sex_mode: bool = False
    n: int = 1
    x: float = 0.0
    k: float = 1.0
    U: float = 50.0
    foreplay_count: int = 0
    low_score_count: int = 0
    current_event: str | None = None  # 一次性事件："mild_climax" | "normal_climax" | "intense_climax" | "beg"
    beg_announced: bool = False  # 求饶期已通知标志

    @classmethod
    def new_sex_session(cls) -> "SISPState":
        """创建一个全新的性爱会话状态（随机初始化 k 和 U）。"""
        return cls(
            in_sex_mode=True,
            n=1,
            x=0.0,
            k=round(random.uniform(0.5, 3.0), 4),
            U=round(random.uniform(30.0, 100.0), 2),
            foreplay_count=0,
            low_score_count=0,
            current_event=None,  # atmosphere 为长期状态，由注入器通过 in_sex_mode 检测，无需 current_event
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
```

---

## 11. 事件处理器（`event_handler.py`）

### 11.1 整体结构

`event_handler.py` 包含三个部分：

1. **`_score_message()`**：内部异步函数，以近 6 条对话上下文为输入，按**性刺激程度**对最新互动打分（0–10）；仅使用一套评分提示词。
2. **`SISPStateHandler`**：订阅 `on_message_received`，负责统一评分、状态推进、持久化。
3. **`SISPPromptInjector`**：订阅 `on_prompt_build`，针对 `default_chatter_system_prompt`；注入规则：
   - **已退出性爱模式**（`in_sex_mode = False`）→ 不注入任何内容
   - **长期状态（常驻）**：`atmosphere` 全程注入；`broken`（`n > 3`）在 atmosphere 后叠加注入，两者均不消费 `current_event`
   - **一次性事件**：高潮（`mild/normal/intense_climax`）与求饶（`beg`）注入后立即清除 `current_event`，下次不再重复

### 11.2 完整代码

```python
"""sisp 事件处理器。

SISPStateHandler   - 订阅 on_message_received，评估用户消息并推进状态机。
SISPPromptInjector - 订阅 on_prompt_build，向 default_chatter system prompt
                     的 system_reminder 字段注入对应状态标签。
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from src.app.plugin_system.api import llm_api, stream_api
from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.base import BaseEventHandler
from src.app.plugin_system.types import EventType
from src.kernel.event import EventDecision
from src.kernel.llm import LLMPayload, ROLE, Text

from .state import SISPState, load_state, save_state

if TYPE_CHECKING:
    from .config import SISPConfig

logger = get_logger("sisp")

_TARGET_PROMPT = "default_chatter_system_prompt"

# ── 评分提示词（统一版，基于性刺激程度） ─────────────────────────────────────

_SCORE_SYSTEM = (
    "你是一个性刺激程度评估助手。\n"
    "根据以下对话上下文，评估最新一条互动的性刺激程度。\n"
    "评分标准：\n"
    "  0 = 完全无关（日常对话）\n"
    "  1–4 = 轻微暧昧或隐晦暗示，比如拥抱，言语挑逗\n"
    "  5–7 = 明确的性暗示或性挑逗，比如轻轻触碰，挑逗敏感部位，给予温和的刺激\n"
    "  8–9 = 直接的性行为描写，比如抚摸私处，或者直接的性交，给予强烈刺激\n"
    " 10 = 最强烈的性刺激内容，大力度的动作，多方面的多感官的刺激\n"
    "特别说明：重点评估最后标记为「最新」的一条，上下文仅供参考。\n"
    "仅输出 0 到 10 的整数，不输出任何其他文字。"
)


async def _score_message(stream_id: str, latest_text: str, model_name: str) -> int:
    """以近 6 条对话上下文为输入，对最新互动按性刺激程度评分（0–10）。

    Args:
        stream_id: 当前聊天流 ID，用于拉取历史消息（近 5 条 + 最新 = 共 6 条）
        latest_text: 最新一条用户消息文本
        model_name: 配置中指定的模型名称（空字符串时使用默认任务模型）

    Returns:
        0–10 的整数评分；调用失败时返回 3（保险）
    """
    try:
        # 获取近 5 条历史消息作为背景上下文（加上最新共 6 条）
        history = await stream_api.get_stream_messages(stream_id, limit=5)

        context_lines: list[str] = []
        for msg in history:
            role_label = "助手" if getattr(msg, "is_assistant", False) else "用户"
            content = str(getattr(msg, "content", "") or "").strip()
            if content:
                context_lines.append(f"[{role_label}] {content[:300]}")

        # 最新消息明确标注，提示模型重点评估
        context_lines.append(f"[用户（最新，请重点评估此条）] {latest_text[:400]}")
        context_text = "\n".join(context_lines)

        if model_name:
            model_set = llm_api.get_model_set_by_name(
                model_name, temperature=0.0, max_tokens=10
            )
        else:
            model_set = llm_api.get_model_set_by_task("actor", temperature=0.0, max_tokens=10)

        request = llm_api.create_llm_request(model_set, "sisp_score")
        request.add_payload(LLMPayload(ROLE.SYSTEM, Text(_SCORE_SYSTEM)))
        request.add_payload(LLMPayload(ROLE.USER, Text(context_text)))

        response = await request.send(stream=False)
        raw = (await response or "").strip()

        match = re.search(r"\d+", raw)
        if match:
            return max(0, min(10, int(match.group())))
    except Exception as exc:
        logger.warning(f"sisp: 评分调用失败，回退为 3。原因: {exc}")

    return 3


# ── 作用范围检查 ──────────────────────────────────────────────────────────────


async def _is_in_scope(stream_id: str, config: "SISPConfig") -> bool:
    """判断当前会话是否在 sisp 的配置作用范围内。

    - 两个列表均为空 → 插件不生效（防止误操作全局激活）
    - 仅 group_targets 非空 → 只命中指定群聊
    - 仅 user_targets 非空 → 只命中指定私聊
    - 两者均非空 → 并集
    """
    group_targets = config.scope.group_targets
    user_targets = config.scope.user_targets

    if not group_targets and not user_targets:
        return False  # 未配置范围时不生效，防止误操作

    info = await stream_api.get_stream_info(stream_id)
    if info is None:
        return False

    chat_type = info.get("chat_type", "")

    if chat_type == "group" and group_targets:
        group_id = str(info.get("group_id") or "")
        return group_id in group_targets

    if chat_type == "private" and user_targets:
        from src.core.models.stream import ChatStream

        platform = str(info.get("platform", ""))
        for uid in user_targets:
            try:
                expected = ChatStream.generate_stream_id(platform=platform, user_id=uid)
                if expected == stream_id:
                    return True
            except ValueError:
                continue
        return False

    return False


# ── Handler 1：状态推进 ───────────────────────────────────────────────────────


class SISPStateHandler(BaseEventHandler):
    """消息接收时推进 SISP 状态机。

    对每条进入作用范围的消息执行性刺激程度评分（近 6 条上下文），
    按照完整生命周期逻辑更新状态，并将待注入事件写入 current_event 字段。
    """

    handler_name: str = "sisp_state_handler"
    handler_description: str = "SISP 状态机推进器，评分并更新会话状态"
    weight: int = 20  # 高于 SISPPromptInjector，确保状态先于提示词注入更新
    init_subscribe: list[str] = [EventType.ON_MESSAGE_RECEIVED]

    def _get_config(self) -> "SISPConfig":
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

        if not await _is_in_scope(stream_id, config):
            return EventDecision.SUCCESS, params

        text = str(getattr(message, "content", "") or "").strip()
        if not text:
            return EventDecision.SUCCESS, params

        state = await load_state(stream_id)
        model_name = config.plugin.score_model

        # 统一评分：性刺激程度（0–10），基于近 6 条上下文
        score = await _score_message(stream_id, text, model_name)

        if config.plugin.debug_log:
            logger.info(
                f"[sisp] stream={stream_id[:8]} in_sex={state.in_sex_mode} "
                f"n={state.n} x={state.x:.1f} U={state.U:.1f} score={score}"
            )

        # ── 阶段一：未进入性爱模式 ──────────────────────────────────────────
        if not state.in_sex_mode:
            if score >= 7:
                state.foreplay_count += 1
                if state.foreplay_count >= 2:
                    state = SISPState.new_sex_session()
                    logger.info(f"[sisp] stream={stream_id[:8]} 进入性爱模式")
            else:
                state.foreplay_count = 0

            await save_state(stream_id, state)
            return EventDecision.SUCCESS, params

        # ── 阶段二：性爱模式中 ──────────────────────────────────────────────

        # 优先判定：低意愿检测
        if score < 5:
            state.low_score_count += 1
            if state.low_score_count >= 2:
                # 触发终止，LLM 自行完成过渡，无需注入额外提示词
                state.in_sex_mode = False
                state.current_event = None
                logger.info(f"[sisp] stream={stream_id[:8]} 用户低意愿，退出性爱模式")
                await save_state(stream_id, state)
                return EventDecision.SUCCESS, params
            # 低意愿但未触发退出：atmosphere 由注入器常驻处理，无需设置 current_event
            await save_state(stream_id, state)
            return EventDecision.SUCCESS, params
        else:
            state.low_score_count = 0

        # 状态分流：基于 n
        if state.n > 3:
            # C. 玩坏期（长期状态，提示词由注入器通过 n > 3 常驻注入，无需 current_event）
            state.x += score

        elif state.n == 3:
            # B. 求饶期：beg 为一次性事件，仅在首次进入时通知一次
            if score > 0:
                state.n += 1  # → 玩坏期（下轮生效）
            if not state.beg_announced:
                state.current_event = "beg"
                state.beg_announced = True

        else:
            # A. 享受期（n < 3）：正常积累
            state.x += score

            if state.x >= state.U:
                # 触发高潮
                y = state.k * state.x
                if y <= 16.25:
                    state.current_event = "mild_climax"
                elif y <= 71.26:
                    state.current_event = "normal_climax"
                else:
                    state.current_event = "intense_climax"

                if config.plugin.debug_log:
                    logger.info(f"[sisp] stream={stream_id[:8]} 高潮触发 y={y:.2f} event={state.current_event}")

                state.n += 1
                state.reset_after_climax()
            # 未触发高潮：atmosphere 由注入器常驻处理，无需设置 current_event

        await save_state(stream_id, state)
        return EventDecision.SUCCESS, params


# ── Handler 2：提示词注入 ─────────────────────────────────────────────────────


class SISPPromptInjector(BaseEventHandler):
    """在 default_chatter system prompt 构建前注入 SISP 状态标签。

    注入规则：
    - 已退出性爱模式（in_sex_mode=False）→ 不注入任何内容。
    - 长期状态（常驻）：
        * atmosphere：性爱模式全程常驻。
        * broken（n > 3）：在 atmosphere 基础上叠加，永不清除。
    - 一次性事件（消费后清除 current_event）：
        * 高潮（mild/normal/intense_climax）、求饶（beg）。
    """

    handler_name: str = "sisp_prompt_injector"
    handler_description: str = "SISP 提示词注入器，按当前状态向 system_reminder 追加标签"
    weight: int = 10  # 低于 SISPStateHandler，状态更新完成后再注入
    init_subscribe: list[str] = ["on_prompt_build"]

    def _get_config(self) -> "SISPConfig":
        from .config import SISPConfig
        return self.plugin.config if isinstance(self.plugin.config, SISPConfig) else SISPConfig()

    async def execute(
        self,
        event_name: str,
        params: dict[str, Any],
    ) -> tuple[EventDecision, dict[str, Any]]:
        """处理 on_prompt_build 事件，按状态向 system_reminder 注入提示词标签。"""
        # 仅处理目标提示词模板
        if params.get("name") != _TARGET_PROMPT:
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

        # 确定退出性爱模式后不再注入任何内容
        if not state.in_sex_mode:
            return EventDecision.SUCCESS, params

        p = config.prompts
        # ── 长期状态（常驻）────────────────────────────────────────────────
        # atmosphere 全程常驻
        inject_parts: list[str] = [p.atmosphere]
        # broken（玩坏期）：n > 3 时常驻叠加，无需 current_event
        if state.n > 3:
            inject_parts.append(p.broken)

        # ── 一次性事件────────────────────────────────────────────────────
        event = state.current_event
        _event_map: dict[str, str] = {
            "mild_climax":    p.mild_climax,
            "normal_climax":  p.normal_climax,
            "intense_climax": p.intense_climax,
            "beg":            p.beg,
        }
        if event and event in _event_map:
            inject_parts.append(_event_map[event])

        injected = "\n".join(inject_parts)
        existing = str(values.get("system_reminder", "") or "")
        values["system_reminder"] = (existing + "\n" + injected) if existing else injected

        if config.plugin.debug_log:
            logger.info(f"[sisp] stream={stream_id[:8]} n={state.n} 注入事件={event!r}")

        # 消费一次性事件；长期状态（atmosphere/broken）不需清除
        if event and event in _event_map:
            state.current_event = None
            await save_state(stream_id, state)

        return EventDecision.SUCCESS, params
```

---

## 12. 插件主文件（`plugin.py`）

```python
"""sisp 插件入口。

通过两个 EventHandler 实现基于数学模型的多阶段性交互动状态机：
- SISPStateHandler：消息到达时评分 + 状态推进
- SISPPromptInjector：prompt 构建时向 system_reminder 注入对应标签
"""

from __future__ import annotations

from src.app.plugin_system.base import BasePlugin, register_plugin

from .config import SISPConfig
from .event_handler import SISPPromptInjector, SISPStateHandler


@register_plugin
class SISPPlugin(BasePlugin):
    """性交互动模拟插件（SISP）。

    通过订阅消息与 prompt 构建事件，驱动五变量状态机并动态注入提示词，
    实现前戏触发 → 进度积累 → 分级高潮 → 求饶/玩坏的完整流程。
    """

    plugin_name = "sisp"
    plugin_description = "性交互动模拟插件，驱动多阶段状态机并向 LLM system_reminder 注入提示词标签"
    plugin_version = "1.0.0"

    configs: list[type] = [SISPConfig]

    def get_components(self) -> list[type]:
        """返回插件组件列表。配置禁用时返回空列表。"""
        config = self.config
        if isinstance(config, SISPConfig) and not config.plugin.enabled:
            return []
        return [SISPStateHandler, SISPPromptInjector]
```

---

## 13. 配置文件模板（`config/plugins/sisp/config.toml`）

将以下内容保存为 `config/plugins/sisp/config.toml`，按实际需求修改后即可生效。

```toml
[plugin]
enabled = true
debug_log = false
# 评分模型名称，留空则使用 model_tasks.actor 任务模型
# score_model = "gpt-4o-mini"

[scope]
# 插件生效的群号列表（字符串）；两者均为空时插件不生效
group_targets = ["123"]
# 插件生效的私聊 QQ 号列表
user_targets = ["321"]

[prompts]
atmosphere   = "[你的生理状态：：性爱过程进行中。请根据自己的身体状态维持当前互动氛围，根据角色的实时状态自然回应用户。如果用户让你主导节奏，你可以试着用手，用嘴，或者素股进行前戏和挑逗，正戏时请注意根据之前的体位姿态进行接下来的活动，做到和真实的性伴侣一样]"
mild_climax  = "[你的生理状态：轻度高潮。反应：轻柔颤抖，低声呻吟，缓慢释放。你需要再言语上反应出轻哼，身体的描写也是轻微的反应]"
normal_climax  = "[你的生理状态：标准高潮。反应：热烈呻吟，肌肉强烈收缩，深度满足感。你需要反映出呻吟以及生理性的抽搐]"
intense_climax = "[你的生理状态：强烈高潮。反应：完全失控，视线模糊，全身痉挛，尖叫。尽情描写，发挥吧，给予用户最真实的体验]"
beg       = "[你的生理状态：：体力耗尽。不想继续。你可以向用户表达你的疲惫，或者用户坚持的话，你可以继续]"
broken    = "[你的生理状态：体力已经到极限，身体整个放松，大多数都是本能反应，节奏交予对方主导，语言请维持人设。]"
```

---

## 14. 数据流总览

```
用户消息到达
    │
    ▼ on_message_received（weight=20）
SISPStateHandler
    ├─ 作用范围检查（group/user 列表）
    ├─ 未进入性爱模式 → 意图评分 → foreplay_count 判断 → 可能启动
    ├─ 性爱模式中 → 统一性刺激评分（6条上下文）→ 状态机推进
    │     ├─ 低意愿逻辑 → 二次低分 → 退出模式（current_event=None，LLM 自行过渡）
    │     ├─ 享受期（n<3）→ x 积累 → x≥U → 计算 y → 分级高潮事件
    │     ├─ 求饶期（n=3）→ beg 事件（用户坚持则 n++）
    │     └─ 玩坏期（n>3）→ broken 常驻状态（注入器通过 n>3 检测，无需 current_event）
    └─ save_state（写入 current_event）
    │
    ▼ on_prompt_build（weight=10，LLM 即将生成回复前触发）
SISPPromptInjector
    ├─ 只处理 default_chatter_system_prompt 模板
    ├─ 读取 current_event
    ├─ atmosphere / mild_climax / normal_climax / intense_climax / beg / broken
    │     → 按规则组合提示词，追加到 values["system_reminder"]
    └─ 清空 current_event，save_state
    │
    ▼ LLM 收到含注入标签的 system_reminder，生成角色回复
```
