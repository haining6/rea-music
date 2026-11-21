"""生成后端适配：YuE2 Studio MCP 真实链路、ACE-Step 探测接线、Suno 提示词包。

诚实边界（A5/A6，R4-A/B）：
- YuE2 Studio 是用户已装的原生应用，MCP 端口 127.0.0.1:8791 仅在窗口打开时可用；
  streamable HTTP 会话经 initialize 响应头捕获 mcp-session-id 并在后续请求回传。
- style 为一句英文（语言/流派时代/人声/乐器/情绪/制作/末尾 "N BPM"），
  禁写歌手名、歌名与调性；lyrics 用段落标签行组织，歌词必须来自用户原创文本，
  REA-Music 不代写歌词、不复制参考曲歌词。
- ACE-Step 由用户手动拉起（默认 127.0.0.1:8001），路由以运行时 /openapi.json
  发现为准，禁止硬编码猜测路径（见 acestep.py）。
- Suno 无官方自动化接口，绝不伪造。降级路径 = 输出结构化提示词包，
  用户创作后导入评测。
- 远程引擎未获授权不传音频（音频不出本机）。
"""

from __future__ import annotations

import json
import math
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

from rea_music.core.errors import REAError, ValidationError
from rea_music.core.paths import artifacts_dir
from rea_music.generation import acestep as _acestep

# YuE2 Studio MCP 完整链必需工具（tools/list 校验，缺任何一个即结构化降级）
_YUE2_REQUIRED_TOOLS = ("song_create", "studio_wait", "song_export")

_INIT_PARAMS: dict = {
    "protocolVersion": "2025-03-26",
    "capabilities": {},
    "clientInfo": {"name": "rea-music", "version": "0.1"},
}

_DONE_STATUS = {"completed", "complete", "done", "finished",
                "succeeded", "success"}
_FAILED_STATUS = {"failed", "failure", "error", "cancelled", "canceled"}
# 段落标签映射：协议标签 [Intro]/[Verse N]/[Pre-Chorus]/[Chorus]/[Bridge]/[Outro]
_SECTION_TAG = {
    "intro": "[Intro]",
    "pre_chorus": "[Pre-Chorus]",
    "chorus": "[Chorus]",
    "bridge": "[Bridge]",
    "outro": "[Outro]",
    "final_chorus": "[Chorus]",
    "instrumental": "[Instrumental]",
}
# style 句禁入的调性/速度词（流派标签里出现即整条丢弃，宁缺毋编）
_TONALITY_WORDS = ("major", "minor", "tonality", "key of", "bpm")
_CJK_RANGES = ((0x4E00, 0x9FFF), (0x3400, 0x4DBF), (0x3000, 0x303F),
               (0xFF00, 0xFFEF))
_AUDIO_EXTS = (".wav", ".mp3", ".flac", ".ogg", ".m4a")
_PATH_KEYS = {"file", "files", "path", "paths", "audio", "audio_url",
              "output", "output_path", "url"}


def yue2_probe(cfg: dict | None = None) -> dict:
    """探测 YuE2 Studio MCP 是否在线（只做可达性，不假定工具名）。"""
    url = (cfg or {}).get("yue2_mcp_url", "http://127.0.0.1:8791/mcp")
    try:
        req = urllib.request.Request(url, method="GET",
                                     headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            return {"available": True, "url": url, "http_status": resp.status,
                    "note": "studio 在线；工具清单经 MCP 协议发现后使用"}
    except (urllib.error.URLError, TimeoutError, OSError):
        return {
            "available": False, "url": url,
            "reason": "studio_window_not_open_or_port_closed",
            "hint": "打开 YuE2 Studio 窗口（可加 --headless）后重试；"
                    "REA-Music 不会代替用户启动桌面应用",
        }


def _mcp_call(url: str, method: str, params: dict,
              session: dict | None = None, timeout: float = 120,
              notification: bool = False) -> dict:
    """最小 JSON-RPC over streamable HTTP POST。

    session 为可变 dict：从响应头捕获 mcp-session-id（initialize 时建立）并在
    后续请求回传（streamable HTTP 会话要求）。notification=True 时不带 id，
    空响应体返回 {}（通知尽力而为，失败由调用方决定是否阻断）。
    """
    message: dict = {"jsonrpc": "2.0", "method": method, "params": params}
    if not notification:
        message["id"] = 1
    headers = {"Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"}
    if session and session.get("session_id"):
        headers["mcp-session-id"] = session["session_id"]
    req = urllib.request.Request(
        url, data=json.dumps(message).encode(), method="POST", headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        # 会话头捕获：只信响应头里的真实值，不猜测
        sid = resp.headers.get("mcp-session-id")
        if sid and session is not None:
            session["session_id"] = sid
        body = resp.read().decode("utf-8", "replace")
    if not body.strip():
        return {}
    # streamable HTTP 可能返回 SSE 帧
    if body.lstrip().startswith("event:") or "data:" in body[:200]:
        data_lines = [ln[5:].strip() for ln in body.splitlines()
                      if ln.startswith("data:")]
        for ln in reversed(data_lines):
            try:
                return json.loads(ln)
            except json.JSONDecodeError:
                continue
        return {}
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return {"_raw": body[:500]}


def _tool_call(url: str, session: dict, name: str, arguments: dict) -> dict:
    """tools/call 包装：抽取 text content 并尽量解析为 JSON。

    JSON-RPC 层错误不抛裸异常，放回 {"_rpc_error": ...} 由调用方结构化处理。
    """
    resp = _mcp_call(url, "tools/call", {"name": name, "arguments": arguments},
                     session=session)
    if resp.get("error"):
        return {"_rpc_error": resp["error"]}
    content = (resp.get("result") or {}).get("content") or []
    text = "\n".join(part.get("text", "") for part in content
                     if isinstance(part, dict) and part.get("type") == "text")
    payload = _try_json(text)
    return payload if isinstance(payload, dict) else {"text": str(payload)}


def _try_json(text: str):
    """text → JSON：直接解析失败时截取首尾大括号再试，仍失败返回原文。"""
    text = (text or "").strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                return text
        return text


def _find_first(node, keys: tuple[str, ...]):
    """深度优先找第一个命中 keys 且值为非空标量的字段。

    studio 工具响应的具体 schema 以实测为准；在此做稳健提取，找不到返回 None
    由调用方结构化报错，不编造。
    """
    if isinstance(node, dict):
        for k, v in node.items():
            if k in keys and isinstance(v, (str, int, float)) and v != "":
                return v
        for v in node.values():
            found = _find_first(v, keys)
            if found is not None:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find_first(item, keys)
            if found is not None:
                return found
    return None


def _cjk_ratio(text: str) -> float:
    """CJK 字符占比（用于从用户歌词推断演唱语言，宁缺毋猜的保守实现）。"""
    chars = [c for c in (text or "") if not c.isspace()]
    if not chars:
        return 0.0
    cjk = sum(1 for c in chars
              if any(lo <= ord(c) <= hi for lo, hi in _CJK_RANGES))
    return cjk / len(chars)


def _language_word(lyrics_text: str | None) -> str:
    """style 句的语言成分：用户歌词 CJK 占比 ≥0.2 → Mandarin，否则 English。"""
    if lyrics_text and _cjk_ratio(lyrics_text) >= 0.2:
        return "Mandarin"
    return "English"


def _mood_phrase(plan: dict) -> str:
    """从段落能量分布归纳情绪词（plan.sections.energy，数据可溯源）。"""
    energies = [s.get("energy") for s in plan.get("sections", [])
                if isinstance(s, dict)]
    if not energies:
        return "warm and melodic"
    highs = sum(1 for e in energies if e == "high")
    lows = sum(1 for e in energies if e == "low")
    half = max(2, len(energies) // 2)
    if highs >= half:
        return "uplifting and energetic"
    if lows >= half:
        return "calm and intimate"
    return "warm and melodic with a rising energy arc"


def _sanitize_style_tag(tag: str) -> str | None:
    """流派标签过滤：含调性/速度词的标签整条丢弃（style 句禁写调性与速度词）。"""
    low = tag.lower()
    if any(w in low for w in _TONALITY_WORDS):
        return None
    return tag


def _numeric(value) -> float | None:
    """把 field-with-provenance / 数值 / 数字字符串统一成 float。

    渲染层是"最后一个能兜住坏参数的地方"：plan.inherit 里的 bpm 若被写成
    自然语言句子（历史 revise_plan 缺陷），这里返回 None 触发结构化降级，
    而不是让 ``round(str)`` 抛 TypeError 变成 internal_error。
    """
    if isinstance(value, dict):
        value = value.get("value")
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _effective_bpm(plan: dict) -> float | None:
    """有效 BPM：target_modifications.bpm（修订目标）优先，其次 inherit。

    revise_plan 只把目标写进 target_modifications 而不改 inherit（inherit 是
    参考曲的继承值，语义上不该被覆盖）；渲染层必须优先读目标值，否则"修订到
    92 BPM"的方案渲染出来仍是参考曲的 120 BPM，迭代闭环等于空转。
    历史 plan 里的非法值（自然语言句子）自动回落 inherit，不因坏数据中断渲染。
    """
    mods = plan.get("target_modifications") or {}
    if isinstance(mods, dict) and "bpm" in mods:
        value = _numeric(mods["bpm"])
        if value is not None:
            return value
    return _numeric((plan.get("inherit") or {}).get("tempo_bpm"))


def _effective_key(plan: dict):
    """有效调性：target_modifications.key 优先，其次 inherit.key。"""
    mods = plan.get("target_modifications") or {}
    raw = mods.get("key") if isinstance(mods, dict) and "key" in mods \
        else (plan.get("inherit") or {}).get("key")
    return raw.get("value") if isinstance(raw, dict) else raw


def build_yue2_style(plan: dict, lyrics_text: str | None = None) -> str:
    """由 CompositionPlan 组装 YuE2 style 句（一句英文）。

    规范：语言 / 流派时代 / 人声 / 乐器 / 情绪 / 制作，末尾 "N BPM"；
    禁写歌手名、歌名、调性。缺 BPM 即无法满足规范结尾 → ValidationError。
    """
    inherit = plan.get("inherit", {})
    bpm_val = _effective_bpm(plan)
    if bpm_val is None or bpm_val <= 0:
        raise ValidationError(
            "CompositionPlan 缺少有效的 BPM（inherit.tempo_bpm 与 "
            "target_modifications.bpm 都不可用），无法组装以 \"N BPM\" "
            "结尾的合规 style 句",
            hint="补齐参考 DNA 的 BPM 分析，或在 target_modifications.bpm "
                 "中指定目标速度后重建 plan")
    tags = [t for t in (_sanitize_style_tag(str(t).strip())
                        for t in (inherit.get("style_tags") or [])) if t]
    genre = ", ".join(tags) if tags else "modern pop"
    vocal = "expressive clear vocals"  # plan 不携带声部信息，用中性人声描述
    instruments = str(inherit.get("instrumentation_hint")
                      or "balanced instrumentation")
    mood = _mood_phrase(plan)
    language = _language_word(lyrics_text)
    parts = [
        f"{language} {genre}, modern era",
        vocal,
        instruments,
        f"{mood} mood",
        "clean polished modern production",
        f"{round(float(bpm_val))} BPM",
    ]
    return ", ".join(parts)


def build_yue2_lyrics(plan: dict) -> str:
    """由 plan 组装带段落标签行的歌词文本（每段一标签行）。

    - lyrics_guidance.mode 必须为 "user_provided"（用户原创歌词），
      否则 ValidationError（REA-Music 不代写歌词，也不用模板占位行冒充）；
    - 标签行用 [Intro]/[Verse N]/[Pre-Chorus]/[Chorus]/[Bridge]/[Outro]；
    - 用户词行若已自带 [xx] 标签则透传该段，不重复加标签。
    """
    lg = plan.get("lyrics_guidance") or {}
    if lg.get("mode") != "user_provided":
        raise ValidationError(
            f"YuE2 真实生成需要用户原创歌词，当前 plan 未携带"
            f"（lyrics_guidance.mode={lg.get('mode')!r}）",
            hint="用 create_composition_plan(objective='lyrics_driven', "
                 "user_lyrics=<用户原创歌词>) 创建带歌词的 plan 后再走真实生成；"
                 "REA-Music 不代写歌词")
    sections = lg.get("sectioning_suggestion") or []
    if not sections:
        raise ValidationError("lyrics_guidance.sectioning_suggestion 为空，"
                               "无法切分段落标签",
                               hint="重新创建 lyrics_driven plan 以生成歌词切分建议")
    out: list[str] = []
    verse_no = 0
    for sec in sections:
        if not isinstance(sec, dict):
            continue
        label = str(sec.get("label", "verse")).lower()
        lines = [str(ln) for ln in (sec.get("lines") or [])
                 if str(ln).strip()]
        # 用户已自带标签行则透传，不重复加标签
        if lines and lines[0].strip().startswith("[") \
                and lines[0].strip().endswith("]"):
            out.extend(lines)
            continue
        if label == "verse":
            verse_no += 1
            out.append(f"[Verse {verse_no}]")
        else:
            out.append(_SECTION_TAG.get(label, f"[Verse {verse_no + 1}]"))
        out.extend(lines)
    if not out:
        raise ValidationError("用户歌词切分后为空，无法组装歌词文本")
    return "\n".join(out)


def _derive_title(plan: dict) -> str:
    """song_create 标题：优先用户请求文本（截断净化），否则 plan_id。"""
    req = re.sub(r"[\r\n\t]+", " ", str(plan.get("user_request") or "").strip())
    if req:
        return req[:60]
    return f"REA {str(plan.get('plan_id') or 'plan')[:16]}"


def _collect_paths(payload) -> list[str]:
    """从 song_export 响应里稳健收集音频产物路径（键名或扩展名二选一命中）。"""
    out: list[str] = []

    def _walk(node, key: str | None) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                _walk(v, str(k))
        elif isinstance(node, list):
            for item in node:
                _walk(item, key)
        elif isinstance(node, str) and node:
            hit = (key is not None and key.lower() in _PATH_KEYS) \
                or node.lower().endswith(_AUDIO_EXTS)
            if hit:
                out.append(node)

    _walk(payload, None)
    return out


def generate_with_yue2(cfg: dict, plan: dict) -> dict:
    """YuE2 Studio 真实生成链（同步函数；job 化由 MCP 层负责）。

    链路：probe → initialize（捕获 mcp-session-id）→ tools/list（校验必需工具，
    缺工具即结构化降级）→ 组装 style/lyrics → song_create → studio_wait 轮询
    （上限 generation.yue2_wait_timeout_sec，默认 600s）→ song_export 音频落
    workspace/artifacts/<plan_id>/ → 返回产物路径 + 耗时 + studio 活动记录。

    任何一步不可达/失联/超时都返回结构化 backend_unavailable / generation_failed
    而非抛裸异常；studio 10 分钟空闲自动退出 → 中途失联即结构化降级。
    """
    cfg = cfg or {}
    url = cfg.get("yue2_mcp_url", "http://127.0.0.1:8791/mcp")
    raw_wait_timeout = cfg.get("yue2_wait_timeout_sec", 600)
    wait_timeout = (float(raw_wait_timeout)
                     if isinstance(raw_wait_timeout, (int, float))
                     else 600.0)
    export_format = str(cfg.get("yue2_export_format", "wav"))
    t0 = time.monotonic()
    activity: list[dict] = []

    def _elapsed() -> float:
        return round(time.monotonic() - t0, 1)

    def _log(step: str, detail: str) -> None:
        activity.append({"step": step, "detail": detail,
                         "elapsed_sec": _elapsed()})

    def _fail(code: str, message: str, hint: str | None = None) -> dict:
        error = {"code": code, "message": message}
        if hint:
            error["hint"] = hint
        return {"ok": False, "backend": "yue2_studio_mcp", "error": error,
                "fallback": "调用方可改用 render_generation_request(plan, 'suno') "
                            "输出提示词包降级",
                "activity": activity, "elapsed_sec": _elapsed()}

    # 1) 探测（只做可达性）
    probe = yue2_probe(cfg)
    _log("probe", probe.get("note") or probe.get("reason") or "")
    if not probe.get("available"):
        return _fail("backend_unavailable",
                     f"YuE2 Studio 不可达: {probe.get('reason')}",
                     probe.get("hint"))

    session: dict = {}

    # 2) initialize：握手并捕获会话头
    try:
        init = _mcp_call(url, "initialize", _INIT_PARAMS, session=session)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return _fail("backend_unavailable",
                     f"initialize 失败，studio 可能刚空闲退出或窗口关闭: {exc}",
                     "重新打开 YuE2 Studio 窗口后重试；10 分钟空闲会自动退出")
    if init.get("error"):
        return _fail("backend_unavailable",
                     f"initialize 被拒: {init['error']}")
    try:
        _mcp_call(url, "notifications/initialized", {}, session=session,
                  notification=True)
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        pass  # 通知尽力而为：个别服务无响应体，失败不阻断主链
    _log("initialize",
         f"session_id={'captured' if session.get('session_id') else 'none'}")

    # 3) tools/list：校验必需工具，缺任何一个即结构化降级
    try:
        tools = _mcp_call(url, "tools/list", {}, session=session)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return _fail("backend_unavailable", f"tools/list 失联: {exc}",
                     "studio 可能在握手后退出；重开窗口后重试")
    names = [t.get("name") for t in (tools.get("result") or {}).get("tools", [])
             if isinstance(t, dict)]
    missing = [t for t in _YUE2_REQUIRED_TOOLS if t not in names]
    _log("tools/list", f"{len(names)} 个工具" +
         (f"，缺失: {missing}" if missing else "，必需工具齐全"))
    if missing:
        return _fail("backend_unavailable",
                     f"YuE2 Studio 工具清单缺少必需工具: {missing}",
                     "studio 版本可能不匹配；确认 YuE2 Studio ≥ v4.1.1 且 "
                     "MCP 服务正常后重试")

    # 4) 组装 style 与 lyrics（歌词必须为用户原创文本）
    try:
        lyrics = build_yue2_lyrics(plan)
        style = build_yue2_style(plan, lyrics_text=lyrics)
        title = _derive_title(plan)
    except REAError as exc:
        out = exc.to_dict()
        out["backend"] = "yue2_studio_mcp"
        out["fallback"] = ("补齐用户原创歌词后重走真实生成；或先用 "
                           "render_generation_request(plan, 'suno') 输出提示词包")
        out["activity"] = activity
        out["elapsed_sec"] = _elapsed()
        return out
    _log("compose_inputs",
         f"title={title!r}, style {len(style)} 字符, lyrics {len(lyrics)} 字符")

    # 5) song_create：起生成任务
    create = _tool_call(url, session, "song_create", {
        "title": title,
        "style": style,
        "lyrics": lyrics,
        "cover_prompt": f"minimal abstract cover art for {title}",
    })
    if create.get("_rpc_error"):
        return _fail("generation_failed",
                     f"song_create 调用失败: {create['_rpc_error']}")
    job_id = _find_first(create, ("job_id", "jobId", "job", "id"))
    if not job_id:
        return _fail("generation_failed",
                    "song_create 返回中未识别到 job_id（studio 响应结构可能更新）",
                    f"响应摘要: {json.dumps(create, ensure_ascii=False)[:400]}")
    job_id = str(job_id)
    _log("song_create", f"job_id={job_id}")

    # 6) studio_wait 轮询：每次调用默认 ~30s 返回，反复调用直到完成或超时
    deadline = time.monotonic() + wait_timeout
    waits = 0
    while True:
        if time.monotonic() >= deadline:
            return _fail(
                "backend_unavailable",
                f"studio_wait 超过 {wait_timeout:.0f}s 上限仍未完成",
                "job 仍在 studio 侧运行；可在 studio 窗口查看进度，或调大 config "
                "generation.yue2_wait_timeout_sec 后重试")
        try:
            wait = _tool_call(url, session, "studio_wait", {"job_id": job_id})
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return _fail("backend_unavailable",
                         f"studio_wait 失联: {exc}",
                         "studio 10 分钟空闲会自动退出；重开窗口后可用 "
                         "library_songs_list 查询既有 job 结果，或重跑生成")
        if wait.get("_rpc_error"):
            return _fail("generation_failed",
                         f"studio_wait 调用失败: {wait['_rpc_error']}")
        waits += 1
        status = str(_find_first(wait, ("status", "state", "job_status"))
                      or "").lower()
        _log("studio_wait", f"#{waits} status={status or 'unknown'}")
        if status in _FAILED_STATUS:
            return _fail("generation_failed",
                         f"studio 报告任务失败: {status or 'unknown'}",
                         f"响应摘要: {json.dumps(wait, ensure_ascii=False)[:400]}")
        done = status in _DONE_STATUS or (
            not status and any(k in json.dumps(wait) for k in
                               ("songs", "audio", "files", "output")))
        if done:
            break

    # 7) song_export：音频落 workspace/artifacts/<plan_id>/
    dest = artifacts_dir() / str(plan.get("plan_id") or "plan_unknown")
    dest.mkdir(parents=True, exist_ok=True)
    export = _tool_call(url, session, "song_export", {
        "job_id": job_id,
        "format": export_format,
        "target_folder": str(dest),
    })
    if export.get("_rpc_error"):
        return _fail("generation_failed",
                     f"song_export 调用失败: {export['_rpc_error']}")
    files = _collect_paths(export)
    exported = [str(Path(f)) for f in files] or [f"(studio 应已写入 {dest})"]
    _log("song_export", f"{len(files)} 个产物 → {dest}")

    return {
        "ok": True,
        "backend": "yue2_studio_mcp",
        "plan_id": plan.get("plan_id"),
        "job_id": job_id,
        "title": title,
        "style": style,
        "lyrics": lyrics,
        "export_dir": str(dest),
        "exported_files": exported,
        "wait_rounds": waits,
        "elapsed_sec": _elapsed(),
        "activity": activity,
        "rights_note": "歌词由用户提供，权利归用户；REA-Music 不复制参考曲歌词与旋律",
    }


def generate_auto(cfg: dict, plan: dict) -> dict:
    """generate_music(backend="auto") 的真实顺序：yue2 → ace_step → suno 包。

    - yue2 在线即真实生成（song_create 全链）；
    - ace_step 探测可用才考虑；提交接线须按 openapi 实测路由语义接入，
      未实测前如实记 capability_unavailable 并降级，不编造调用；
    - 最终降级 Suno 提示词包（不伪造接口）。每个 backend 的尝试/跳过原因
      记入 attempts，供调用方如实转达。
    """
    cfg = cfg or {}
    attempts: list[dict] = []

    # 1) YuE2 Studio：在线即真实生成
    yue2 = generate_with_yue2(cfg, plan)
    if yue2.get("ok"):
        yue2["attempts"] = [{"backend": "yue2_studio_mcp", "outcome": "ok"}]
        return yue2
    attempts.append({
        "backend": "yue2_studio_mcp",
        "outcome": (yue2.get("error") or {}).get("code", "backend_unavailable"),
        "message": (yue2.get("error") or {}).get("message", ""),
    })

    # 2) ACE-Step：探测可用才走（离线如实跳过；在线未接线同样如实降级）
    ace = _acestep.probe(cfg)
    if ace.get("available"):
        attempts.append({
            "backend": "ace_step",
            "outcome": "capability_unavailable",
            "message": "服务在线且路由已发现"
                       + (f"（清单: {ace.get('routes_doc')}）"
                          if ace.get("routes_doc") else "")
                       + "；提交调用待按真实路由实测接线，当前不编造调用",
        })
    else:
        attempts.append({
            "backend": "ace_step",
            "outcome": "capability_unavailable",
            "message": ace.get("note") or ace.get("reason") or "探测不可用",
        })

    # 3) Suno 提示词包：最终降级，产出结构化包供人工流程
    pkg = render_generation_request(plan, "suno", cfg)
    pkg["attempts"] = attempts
    pkg["next"] = ("用户在 Suno 创作完成后，把音频文件传回 "
                   "rea_music_evaluate_generation 闭环评测")
    return pkg


def render_generation_request(plan: dict, backend: str = "suno",
                             cfg: dict | None = None) -> dict:
    """把 CompositionPlan 渲染为后端请求。backend ∈ suno | yue2 | generic。"""
    if backend == "suno":
        return {"ok": True, "backend": "suno_prompt_package", **_suno_package(plan)}
    if backend == "yue2":
        return {"ok": True, "backend": "yue2_request", **_yue2_request(plan)}
    return {"ok": True, "backend": "generic", "plan": plan,
            "note": "通用计划直接透传，由宿主 Agent 自行适配未知后端"}


def _suno_package(plan: dict) -> dict:
    inherit = plan.get("inherit", {})
    bpm_val = _effective_bpm(plan)
    key_val = _effective_key(plan)
    tags = inherit.get("style_tags") or []
    inst_hint = inherit.get("instrumentation_hint") or ""

    style_parts = []
    if tags:
        style_parts.append(", ".join(tags))
    if inst_hint:
        style_parts.append(inst_hint)
    if bpm_val:
        style_parts.append(f"{round(bpm_val)} bpm")
    if key_val:
        style_parts.append(f"{key_val} tonality")
    style_prompt = ", ".join(style_parts)

    sections = plan.get("sections", [])
    lyric_lines = []
    for i, sec in enumerate(sections):
        label = sec["label"].upper().replace("_", " ")
        lyric_lines.append(f"[{label}]")
        emotion = sec.get("emotion")
        energy = sec.get("energy_target")
        if emotion or energy is not None:
            parts = []
            if emotion:
                parts.append(f"mood:{emotion}")
            if energy is not None:
                parts.append(f"energy:{round(float(energy), 2)}")
            lyric_lines.append(f"({' '.join(parts)})")
        n_lines = 4 if sec["label"] in ("chorus", "verse") else 2
        for _ in range(n_lines):
            lyric_lines.append("(original line — 由用户/Agent 填入)")
    lyric_lines.append("[End]")

    return {
        "style_prompt": style_prompt,
        "structured_lyrics_template": "\n".join(lyric_lines),
        "lyrics_rule": "模板行必须整体替换为原创歌词；禁止填入参考曲歌词",
        "exclude_styles": "copy of reference track, direct cover",
        "title_hint": "由用户拟定",
    }


def _yue2_request(plan: dict) -> dict:
    inherit = plan.get("inherit", {})
    sections = plan.get("sections", [])
    emotions = [s.get("emotion") for s in sections if s.get("emotion")]
    mood = emotions[-1] if emotions else None  # 取主导情绪（末段副歌通常定调）
    return {
        "request": {
            "style": {
                "genre": ", ".join(inherit.get("style_tags") or []),
                "mood": mood,
                "voice": None,
                "tempo_bpm": _effective_bpm(plan),
                "instrumentation_hint": inherit.get("instrumentation_hint"),
            },
            "lyrics_sections": [
                {"tag": s["label"],
                 "target_duration_sec": s["target_duration_sec"],
                 "emotion": s.get("emotion"),
                 "energy_target": s.get("energy_target"),
                 "strategy": s.get("strategy")}
                for s in sections],
            "score_mode": "model_composed",
            "note": "YuE2 可先作曲谱后演唱；参数最终以其 MCP 工具 schema 为准",
        }
    }
