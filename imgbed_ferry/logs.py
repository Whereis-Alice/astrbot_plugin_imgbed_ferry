"""统一日志出口。

业务模块要能在没有 AstrBot 的环境里被 import（跑单测、跑脚本），所以这里做一次
兜底：拿不到框架 logger 就退回标准库 logging，行为一致，调用方不用关心差异。
"""

from __future__ import annotations

import logging

try:  # pragma: no cover - 取决于运行环境是否有 AstrBot
    from astrbot.api import logger
except Exception:  # pragma: no cover - 单测/脚本场景
    logger = logging.getLogger("astrbot_plugin_imgbed_ferry")

__all__ = ["logger"]
