"""出站链接的目标地址护栏（SSRF 边界）。

**为什么需要这个模块**

D22 的裁定原本是「不做内网黑名单」——局域网 NAS/DLNA 是正当的音乐文件来源，不能一刀切
封死。但 `core/source.py` 只校验了 scheme ∈ {http, https}，连
`http://169.254.169.254/latest/meta-data/iam/security-credentials/` 这样的云元数据地址
也照单全收：响应体会被写进 `data/downloads/<hash>/`，再经 `inspect` 的 details 回显。

这在 CLI 手动使用场景下问题不大（用户自己知道自己在传什么链接），但 REA-Music 的主要入口
是 **MCP Server**——链接由 Agent 提供，而 Agent 可能被提示注入。此时「用户主动给链接」
这个前提就不成立了。

**策略：deny-by-default + 显式 opt-in**

- 默认（`allow_private=False`）：只放行全球可路由的公网地址；
- `allow_private=True`：额外放行 RFC1918 私网与回环（保留 NAS / 局域网 DLNA 用例）；
- 云元数据地址**无论如何都拒绝**。它没有任何正当的音乐文件用途，只有凭据窃取用途，
  因此不提供开关。

**当前接线状态（已接入）**

`core/source.py::_assert_url_allowed()` 在 `resolve_source()` 判定 http(s) 之后、发起下载
之前调用本模块，`allow_private` 取自配置 `input.allow_private_networks`（默认 False）。

**异常契约（两件事必须分开）**

| 情形 | 抛什么 | 调用方应做什么 |
|---|---|---|
| 地址解析成功但落在禁止段 | ``ValidationError``（details 含 ``blocked``） | 直接向上抛，安全边界 |
| 主机名根本解析不出来 | :class:`HostUnresolvable`（``ValidationError`` 子类，code 为 ``host_unresolvable``） | 放行，让网络层报真实错误 |
| scheme 不支持 / 缺主机名 / 端口非法 | ``ValidationError``（details 不含 ``blocked``） | 向上抛，输入非法 |

判别方式**只用异常类型**：``except HostUnresolvable`` 写在 ``except ValidationError``
之前。``details["blocked"]`` 只作诊断信息（哪几个地址、哪种类别），**不再承担判别职责**——
``source.py`` 已按上表显式分派，不要再引入基于该键的分支。

**已知局限（务必知悉）**

1. **DNS rebinding 未完全防住**：本模块解析一次 DNS 做判断，`urllib` 随后会再解析一次，
   两次之间域名可被重新指向内网。彻底修法是在下载层用解析结果直接连 IP
   （连接固定 + 保留 Host 头）。
2. **重定向未逐跳校验**：`urllib` 默认跟随 3xx，跳转目标不会再过一遍本函数，
   需要自定义 `HTTPRedirectHandler` 在 `redirect_request` 里回调本模块。

> 上述两条都需要改 `source.py` 的下载主流程（含测试 monkeypatch 接缝），
> 归属下载层负责人，不在本模块内实现；本模块只提供判定能力。
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

from rea_music.core.errors import ValidationError


class HostUnresolvable(ValidationError):
    """主机名解析失败（DNS 不可用、离线、域名拼错）。

    与"地址非法"共享错误码需要区分，但**必须继承 ValidationError**——这是刻意的兼容
    选择，理由有两条：

    1. `core/source.py::_assert_url_allowed()` 的既有语义是「解析不出来就放行，交给
       网络层报真实错误」，它靠 `except ValidationError` 实现。若本异常继承 REAError，
       那条放行分支会失效，把"没网"变成硬错误。
    2. 在 API 边界上，两者的处置方式同样都是"这个输入现在用不了"，归在同一个 except
       分支里是合理的。

    ``code`` 已改写为 ``host_unresolvable``，所以机器可读层面的区分是完整的。
    调用方若想显式区分，把 ``except HostUnresolvable`` 写在 ``except ValidationError``
    之前即可。真要彻底分离成 ``REAError`` 子类，需同步改调用点——届时一起改。
    """

    code = "host_unresolvable"

    def __init__(self, message: str, *, hint: str | None = None,
                 details: dict | None = None):
        super().__init__(message, hint=hint, details=details)


# ---------- 地址分类表 ----------
# 用显式网段而不是 ipaddress 的 is_private/is_global：后者的判定随 CPython 版本演进，
# 且 100.64/10（CGNAT，阿里云元数据 100.100.100.200 就在这里）在 3.12 里的归类有歧义。
# 合规与安全判定必须版本稳定，所以自己列表。

_NETWORK_KINDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    # 云元数据必须排在最前：169.254.169.254 同时属于 link_local，
    # 但它的处置策略比 link_local 更严（永远拒绝），需要先命中。
    ("metadata", ("169.254.169.254/32", "fd00:ec2::254/128")),
    ("cgnat", ("100.64.0.0/10",)),
    ("loopback", ("127.0.0.0/8", "::1/128")),
    ("private", ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")),
    ("link_local", ("169.254.0.0/16", "fe80::/10")),
    ("unspecified", ("0.0.0.0/32", "::/128")),
    ("multicast", ("224.0.0.0/4", "ff00::/8")),
    ("reserved", (
        "240.0.0.0/4", "192.0.0.0/24", "198.18.0.0/15", "192.0.2.0/24",
        "198.51.100.0/24", "203.0.113.0/24", "255.255.255.255/32",
        "100::/64", "2001:db8::/32",
    )),
)

_COMPILED: tuple[tuple[str, tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]], ...] = tuple(
    (kind, tuple(ipaddress.ip_network(cidr, strict=False) for cidr in cidrs))
    for kind, cidrs in _NETWORK_KINDS
)

#: 无论 allow_private 取值如何都拒绝的类别
_ALWAYS_BLOCKED: frozenset[str] = frozenset({
    "metadata", "cgnat", "link_local", "unspecified", "multicast", "reserved",
})

#: allow_private=True 时才放行的类别
_PRIVATE_OK: frozenset[str] = frozenset({"loopback", "private"})

_ALLOWED_SCHEMES: tuple[str, ...] = ("http", "https")


def classify_ip(raw: str) -> str:
    """把一个 IP 字面量归类。

    Args:
        raw: IPv4 或 IPv6 的字符串形式（IPv6 可带方括号，会先剥掉）。

    Returns:
        类别名之一：``public`` / ``metadata`` / ``cgnat`` / ``loopback`` /
        ``private`` / ``link_local`` / ``unspecified`` / ``multicast`` / ``reserved``。

    Raises:
        ValidationError: ``raw`` 不是合法 IP 字面量。
    """
    text = raw.strip().strip("[]")
    try:
        addr = ipaddress.ip_address(text)
    except ValueError as exc:
        raise ValidationError(
            f"不是合法的 IP 地址: {raw}",
            hint="传入点分十进制 IPv4 或标准 IPv6 写法",
            details={"value": raw},
        ) from exc

    for kind, networks in _COMPILED:
        # IPv4 地址不能和 IPv6 网段比，ipaddress 会直接报版本不一致，故先对齐版本
        for network in networks:
            if network.version != addr.version:
                continue
            if addr in network:
                return kind
    return "public"


def verdict_for(kind: str, allow_private: bool = False) -> bool:
    """根据地址类别与开关判定是否放行。

    Args:
        kind: :func:`classify_ip` 返回的类别名。
        allow_private: 是否显式允许私网/回环（NAS、DLNA 用例）。

    Returns:
        True 表示放行，False 表示拒绝。
    """
    if kind in _ALWAYS_BLOCKED:
        return False
    if kind in _PRIVATE_OK:
        return allow_private
    return True


def _resolve_addresses(host: str, port: int | None) -> tuple[str, ...]:
    """解析主机名为若干 IP 字面量。

    Args:
        host: 主机名或 IP 字面量。
        port: 端口，None 时按 80/443 猜测；只影响 getaddrinfo 的查询，不影响判定。

    Returns:
        去重后的 IP 字符串元组；解析失败返回空元组（由调用方决定如何处置）。

    Note:
        IP 字面量会直接返回，不触发 DNS 查询。
    """
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        pass
    else:
        return (host.strip("[]"),)

    try:
        infos = socket.getaddrinfo(host, port or 443, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, OSError):
        return ()
    seen: list[str] = []
    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            continue
        addr = str(sockaddr[0])
        if addr not in seen:
            seen.append(addr)
    return tuple(seen)


def assert_public_url(url: str, allow_private: bool = False,
                      resolve_dns: bool = True) -> dict:
    """校验一个 http(s) 链接的目标地址是否可放行；不放行则抛错。

    Args:
        url: 待校验的完整链接。
        allow_private: True 时放行 RFC1918 私网与回环（云元数据仍然拒绝）。
        resolve_dns: False 时跳过 DNS 解析，只校验 IP 字面量。用于离线单测；
            生产路径必须保持 True，否则 ``http://内网域名/`` 这类绕过会生效。

    Returns:
        ``{"url", "scheme", "host", "port", "addresses", "kind", "allow_private"}``；
        ``kind`` 是**最严**的那个地址类别（多 A 记录里只要有一个被拒，整体就拒）。

        **返回契约（下游依赖，改动前必须先沟通）**：``addresses`` 是
        **判定时那一次解析**得到的 IP 列表，调用方应当直接拿它做连接固定
        （pin），不要再单独解析一次——再解析等于把 DNS rebinding 窗口重新打开。
        注意 ``resolve_dns=False`` 时该字段可能是主机名字面量而非 IP，
        不能用于连接固定；生产路径用默认的 ``resolve_dns=True``。

    Raises:
        HostUnresolvable: 主机名解析失败（``ValidationError`` 子类，code 为
            ``host_unresolvable``；调用方应放行，交给网络层报真实错误）。
        ValidationError: scheme 不支持、缺主机名、端口非法，或目标地址落在禁止范围。
    """
    text = (url or "").strip()
    if not text:
        raise ValidationError("链接为空", hint="传入 http(s) 链接")

    parts = urlsplit(text)
    scheme = parts.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise ValidationError(
            f"不支持的 URL scheme: {scheme or '(缺失)'}",
            hint="只接受 http/https 链接",
            details={"scheme": scheme},
        )

    host = parts.hostname or ""
    if not host:
        raise ValidationError(
            "链接缺少主机名",
            hint="检查链接是否写完整，例如 https://cdn.example.com/song.flac",
            details={"url": text},
        )
    try:
        port = parts.port
    except ValueError as exc:
        raise ValidationError(
            "链接端口非法",
            hint="端口必须是 0-65535 的整数",
            details={"url": text},
        ) from exc

    if host.lower() == "localhost":
        addresses: tuple[str, ...] = ("127.0.0.1",)
    elif resolve_dns:
        try:
            addresses = _resolve_addresses(host, port)
        except OSError as exc:              # 含 socket.gaierror：DNS 解析失败
            raise HostUnresolvable(
                f"无法解析主机名: {host}",
                hint="确认域名拼写正确且本机 DNS 可用；离线环境请改用本地文件路径",
                details={"url": text, "host": host,
                         "error": type(exc).__name__},
            ) from exc
        if not addresses:
            raise HostUnresolvable(
                f"无法解析主机名: {host}",
                hint="确认域名拼写正确且本机 DNS 可用；离线环境请改用本地文件路径",
                details={"url": text, "host": host},
            )
    else:
        addresses = (host.strip("[]"),)

    strictest_kind = "public"
    blocked: list[str] = []
    for addr in addresses:
        kind = classify_ip(addr)
        if not verdict_for(kind, allow_private):
            blocked.append(f"{addr}({kind})")
        # 记录最严类别，供返回值与错误详情使用
        if strictest_kind == "public" and kind != "public":
            strictest_kind = kind

    if blocked:
        raise ValidationError(
            f"目标地址不允许访问: {', '.join(blocked)}",
            hint=("该地址属云元数据/链路本地/保留段，任何情况下都不放行；"
                  "若确需访问局域网 NAS，请在配置中显式开启 "
                  "input.allow_private_networks=true"),
            details={
                "url": text,
                "host": host,
                "blocked": blocked,
                "allow_private": allow_private,
            },
        )

    return {
        "url": text,
        "scheme": scheme,
        "host": host,
        "port": port,
        "addresses": list(addresses),
        "kind": strictest_kind,
        "allow_private": allow_private,
    }
