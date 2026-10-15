# -*- coding: utf-8 -*-
"""
网络辅助 —— 本机局域网地址与访问 URL。

为什么单独成模块：桌面状态栏（app/ui）与无 GUI 的便携版入口（main_web.py）
都要展示"用手机打开这个地址"，而 main_web.py 不能导入 app.ui（会拉起 PySide6）。
把这段逻辑放在这里，两边共用一份，避免地址探测逻辑分叉。

关键概念（用户最容易卡住的地方）：
    `127.0.0.1` 是**回环地址**，只代表"这台机器自己"，手机/别的设备永远连不上。
    手机要访问，服务必须监听 `0.0.0.0`（所有网卡），并且手机用本机的
    **局域网 IP**（`192.168.x.x` / `10.x.x.x`）加端口。
"""

import socket

__all__ = [
    "local_ip", "local_ipv4_addresses", "lan_urls", "portal_url",
    "is_loopback_host",
]

_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def is_loopback_host(host: str) -> bool:
    """监听地址是否只对本机可见（此时手机一定连不上）。"""
    return (host or "").strip().lower() in _LOOPBACK_HOSTS


def _is_usable(ip: str) -> bool:
    """是否是可用于局域网访问的 IPv4 地址。

    排除：回环（127.*）、链路本地/APIPA（169.254.*，网卡没拿到 DHCP 时的自分配）、
    0.0.0.0、空值。
    """
    if not ip or ip == "0.0.0.0":
        return False
    return not (ip.startswith("127.") or ip.startswith("169.254."))


def local_ip() -> str:
    """获取**首选**局域网 IPv4 地址；失败时回退 127.0.0.1。

    不真的发包（UDP connect 只做路由选择），因此离线环境也能拿到内网地址。
    注意：有 VPN/虚拟网卡（VMware、Hyper-V、Tailscale）时，这里可能选中虚拟网卡的
    地址 —— 因此展示给用户时必须配合 `local_ipv4_addresses()` 列出全部候选。
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(0.1)
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except Exception:  # noqa: BLE001 — 探测失败只影响展示
        return "127.0.0.1"
    finally:
        sock.close()


def local_ipv4_addresses() -> list[str]:
    """列出本机所有可用于局域网访问的 IPv4 地址（首选地址排最前）。

    为什么要列全部：平板/笔记本常有多张网卡（WiFi + 有线 + 虚拟网卡），
    只报一个 IP 时用户很可能拿到虚拟网卡的地址，手机当然连不上。
    """
    found: list[str] = []
    primary = local_ip()
    if _is_usable(primary):
        found.append(primary)
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        infos = []
    for info in infos:
        try:
            ip = info[4][0]
        except (IndexError, TypeError):
            continue
        if _is_usable(ip) and ip not in found:
            found.append(ip)
    return found


def portal_url(host: str, port: int) -> str:
    """把监听地址转成**本机浏览器**可直接访问的 URL（0.0.0.0 → 127.0.0.1）。"""
    display_host = "127.0.0.1" if host in ("0.0.0.0", "", "::") else host
    return f"http://{display_host}:{port}"


def lan_urls(host: str, port: int) -> list[str]:
    """可访问地址列表：第一个元素恒为本机地址，其余为局域网地址（手机用）。

    监听 127.0.0.1 时只返回本机地址 —— 局域网根本连不上，列出来只会误导用户
    （调用方应据此提示"当前只监听本机，手机访问不了"）。
    """
    urls = [f"http://127.0.0.1:{port}"]
    if is_loopback_host(host):
        return urls
    urls.extend(f"http://{ip}:{port}" for ip in local_ipv4_addresses())
    return urls
