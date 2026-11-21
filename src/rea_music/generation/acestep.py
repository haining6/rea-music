"""ACE-Step 1.5 REST 适配（R4-B，设计决策 A6）。

诚实边界：
- 服务由用户/Agent 按 engines/ACE-Step-1.5 仓库脚本手动拉起（REST 默认 127.0.0.1:8001）；
  本模块绝不代为启动服务进程。
- 路由以运行时 GET /openapi.json 发现为准，禁止硬编码猜测路径。服务在场时把
  发现的路由清单（方法/路径/参数）写到 workspace 的 artifacts/ 下
  （``acestep_routes.md``）供主理人接线参考；**不写仓库 docs/**（只读安装 /
  CI 只读挂载下写盘会失败，且 capabilities() 是只读探测不该产生仓库副作用）；
- 服务不在场或 openapi 不可读时，probe 只返回不可用 + 原因，不编造任何调用。
- 沙箱约束：HTTP 仅回环；远程引擎未授权不传音频（音频不出本机）。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_BASE_URL = "http://127.0.0.1:8001"
_HTTP_METHODS = ("get", "post", "put", "delete", "patch")
_ROUTES_DOC_NAME = "acestep_routes.md"


def probe(cfg: dict | None = None) -> dict:
    """探测 ACE-Step REST 服务并做 openapi 路由发现（只实测，不假定）。

    Returns:
        dict: {
            engine, url, available, routes, routes_doc, checked_at,
            可选 reason / note / hint / http_status / routes_count
        }
        - 端口不通 → available=False, reason="service_offline_or_port_closed"
        - 服务在但 openapi 不可读 → available=False, reason="openapi_unreachable"
          （无路由清单即拒绝猜测调用，如实降级）
        - openapi 可读 → available=True + routes 清单 + 路由文档落盘
    """
    cfg = cfg or {}
    base = str(cfg.get("acestep_base_url", DEFAULT_BASE_URL)).rstrip("/")
    result: dict = {
        "engine": "acestep_1.5",
        "url": base,
        "available": False,
        "routes": [],
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }

    # 1) 在场探测 + 路由发现一步完成：/openapi.json 是唯一在场判据。
    #    FastAPI 常无根路由（GET / 返回 404 属正常，不代表离线），
    #    连接拒绝/超时才判离线；HTTP 4xx 说明服务在监听。
    try:
        spec = _fetch_openapi(base)
    except urllib.error.HTTPError as exc:
        result["reason"] = "openapi_http_error"
        result["http_status"] = exc.code
        result["note"] = (f"服务在 {base} 监听但 /openapi.json 返回 "
                          f"HTTP {exc.code}；拒绝猜测调用，如实降级")
        result["hint"] = "确认服务为 API 模式（FastAPI）且 /openapi.json 可访问后重试"
        return result
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        result["reason"] = "service_offline_or_port_closed"
        result["note"] = (f"ACE-Step API 未在 {base} 监听"
                          f"（{exc.__class__.__name__}）")
        result["hint"] = ("按 engines/ACE-Step-1.5 仓库脚本启动 API 服务"
                          "（默认 127.0.0.1:8001）；REA-Music 不代为启动服务进程")
        return result
    except (json.JSONDecodeError, ValueError) as exc:
        result["reason"] = "openapi_unreachable"
        result["note"] = (f"/openapi.json 返回非 JSON（{exc.__class__.__name__}）；"
                          "无路由清单即拒绝猜测调用，如实降级")
        result["hint"] = ("确认服务为 API 模式（FastAPI）且 /openapi.json 可访问后重试；"
                          "路由语义未经实测前不接入提交调用")
        return result

    routes = _extract_routes(spec)
    if not routes:
        # 可解析但不含任何路由 → 不是可用的 openapi 描述，如实降级
        result["reason"] = "openapi_invalid"
        result["note"] = ("/openapi.json 可读但未包含任何路由（非标准 openapi 结构）；"
                          "无路由清单即拒绝猜测调用，如实降级")
        result["hint"] = ("确认服务为 API 模式且 /openapi.json 返回标准 openapi "
                          "结构后重试；路由语义未经实测前不接入提交调用")
        return result
    result["available"] = True
    result["routes"] = routes
    result["routes_count"] = len(routes)
    doc = _write_routes_doc(routes, base, cfg.get("acestep_routes_doc"))
    if doc is not None:
        result["routes_doc"] = str(doc)
    return result


def _fetch_openapi(base: str) -> dict:
    """GET {base}/openapi.json 并解析为 dict。任何失败由调用方归类为不可用。"""
    req = urllib.request.Request(base + "/openapi.json", method="GET",
                                 headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        body = resp.read().decode("utf-8", "replace")
    return json.loads(body)


def _extract_routes(spec: dict) -> list[dict]:
    """从 openapi spec 提取路由清单（方法/路径/参数/是否 JSON body）。

    只做结构化抽取，不解读语义；语义核对必须在服务实测时完成。
    """
    routes: list[dict] = []
    for path, ops in (spec.get("paths") or {}).items():
        if not isinstance(ops, dict):
            continue
        for method, op in ops.items():
            if not isinstance(op, dict) or method.lower() not in _HTTP_METHODS:
                continue
            params = [
                {"name": p.get("name"),
                 "in": p.get("in"),
                 "required": bool(p.get("required", False))}
                for p in (op.get("parameters") or [])
                if isinstance(p, dict)
            ]
            routes.append({
                "method": method.upper(),
                "path": path,
                "operation_id": op.get("operationId"),
                "summary": op.get("summary") or "",
                "params": params,
                "json_body": bool(op.get("requestBody")),
            })
    routes.sort(key=lambda r: (r["path"], r["method"]))
    return routes


def _write_routes_doc(routes: list[dict], base: str,
                      doc_path: str | None = None) -> Path | None:
    """把运行时发现的路由清单写盘，供主理人按真实路由接线（发现即记录）。

    默认写到 workspace 的 artifacts/ 下：probe() 会被 capabilities() 在只读
    路径上调用，写仓库 docs/ 会在只读安装或无写权限环境抛 OSError。任何写盘
    失败都只降级为 None，绝不影响 probe 的可用性结论。

    Args:
        routes: 已抽取的路由清单。
        base: ACE-Step 服务地址（写进文档头部便于溯源）。
        doc_path: 显式指定的落盘路径（配置项 acestep_routes_doc）。

    Returns:
        实际写入的路径；未写盘返回 None。
    """
    if not routes:
        return None
    if doc_path:
        path = Path(doc_path)
    else:
        from rea_music.core.paths import artifacts_dir
        try:
            path = artifacts_dir() / _ROUTES_DOC_NAME
        except OSError:
            return None
    lines = [
        "# ACE-Step 1.5 REST 路由清单（运行时发现，非硬编码）",
        "",
        f"- 发现时间：{time.strftime('%Y-%m-%dT%H:%M:%S%z')}",
        f"- 来源：`GET {base}/openapi.json`（服务自描述）",
        f"- 路由数：{len(routes)}",
        "- 说明：本文件由 `src/rea_music/generation/acestep.py::probe()` 自动生成，",
        "  仅供主理人接线参考；调用语义（参数/响应 schema）仍须按服务实测核对，",
        "  REA-Music 禁止硬编码猜测路径。",
        "",
        "| # | 方法 | 路径 | operationId | 摘要 | 参数 | JSON body |",
        "|---|------|------|-------------|------|------|-----------|",
    ]
    for i, r in enumerate(routes, 1):
        params = "; ".join(
            f"{p['name']}({p['in']}{'*' if p['required'] else ''})"
            for p in r["params"] if p.get("name")) or "—"
        summary = str(r["summary"]).replace("|", "\\|") or "—"
        op_id = str(r["operation_id"]).replace("|", "\\|") or "—"
        lines.append(f"| {i} | {r['method']} | `{r['path']}` | {op_id} | "
                     f"{summary} | {params} | {'yes' if r['json_body'] else 'no'} |")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        return None
    return path
