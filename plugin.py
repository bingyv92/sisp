"""sisp 插件入口。

通过两个 EventHandler 实现基于数学模型的多阶段性交互动状态机：
- SISPStateHandler：消息到达时评分 + 状态推进
- SISPPromptInjector：prompt 构建时向 user prompt extra 注入对应标签
"""

from __future__ import annotations

from src.core.components import BasePlugin, register_plugin

from .config import SISPConfig
from .event_handler import SISPPromptInjector, SISPStateHandler


@register_plugin
class SISPPlugin(BasePlugin):
    """性交互动模拟插件（SISP）。

    通过订阅消息与 prompt 构建事件，驱动五变量状态机并动态注入提示词，
    实现前戏触发 → 进度积累 → 分级高潮 → 求饶/玩坏的完整流程。
    """

    plugin_name = "sisp"
    plugin_description = "性交互动模拟插件，驱动多阶段状态机并向 LLM user prompt 注入提示词标签"
    plugin_version = "1.0.0"

    configs: list[type] = [SISPConfig]

    def get_components(self) -> list[type]:
        """返回插件组件列表。配置禁用时返回空列表。"""
        config = self.config
        if isinstance(config, SISPConfig) and not config.plugin.enabled:
            return []
        return [SISPStateHandler, SISPPromptInjector]
