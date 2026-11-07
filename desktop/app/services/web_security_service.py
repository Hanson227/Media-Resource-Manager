# -*- coding: utf-8 -*-
"""
Web 访问安全 —— 局域网监听时的默认 PIN。

为什么需要：便携版默认监听 `0.0.0.0`（手机/平板必须能连），但 `web_pin`
缺省是空字符串，旧行为是"空 PIN = 整站不鉴权"。同一 WiFi 下的任何设备
（包括访客、智能电视、被入侵的 IoT 设备）都能浏览、下载、甚至删除媒体库。
这不是可用性取舍，而是默认不安全的配置。

策略：
- 只监听回环（127.0.0.1 / localhost）时保持免 PIN，本机使用不受打扰；
- 监听 `0.0.0.0` / 具体网卡地址且没有 PIN 时，首次启动生成一个随机 4 位 PIN，
  落盘到 `.web_pin`（与现有持久化机制一致），并打印到控制台/日志。
桌面端用户也可以在「设置 → API 服务」里查看与修改这个 PIN。
"""

import logging
import secrets

from config import AppConfig
from app.utils.net import is_loopback_host

logger = logging.getLogger(__name__)

__all__ = ["generate_pin", "ensure_web_pin"]


def generate_pin() -> str:
    """生成 4 位数字 PIN（含前导 0，例如 0042）。"""
    return f"{secrets.randbelow(10000):04d}"


def ensure_web_pin(runtime_config: AppConfig,
                   config_path,
                   host: str) -> AppConfig:
    """局域网监听且未设置 PIN 时自动生成并落盘，返回运行时配置。

    参数:
        runtime_config: 已解析路径的运行时配置（用于启动 API）。
        config_path: config.json 路径（PIN 落到其同级 data/.web_pin，
            或由 resolve_paths 注入的数据根 data/.web_pin）。
        host: 实际监听地址。
    """
    if runtime_config.web_pin or is_loopback_host(host):
        return runtime_config
    pin = generate_pin()
    try:
        # 只写 PIN 文件，不重写 config.json —— 避免把解析后的绝对路径
        # 顺手落盘、钉死盘符。
        from config import pin_file_path
        target = pin_file_path(config_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(pin, encoding="utf-8")
    except OSError as e:  # 只读介质也要继续跑，至少本次进程受 PIN 保护
        logger.error(f"Web 访问密码落盘失败（本次仍生效）: {e}")
    logger.warning("=" * 56)
    logger.warning(f"已为局域网访问自动生成 Web 访问密码: {pin}")
    logger.warning("手机/平板首次打开会要求输入该密码；可在 Web「设置 → 访问密码」修改。")
    logger.warning("=" * 56)
    return runtime_config.with_updates(web_pin=pin)
