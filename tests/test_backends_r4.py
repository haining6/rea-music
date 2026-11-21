"""R4 生成后端验收测试（R4-A YuE2 真实链 + R4-B ACE-Step 探测）。

studio 当前离线（127.0.0.1:8791 / 8001 端口关闭）属预期环境事实：离线路径
直测结构化降级；在线协议行为用 fake urllib 模拟官方文档 v4.1.1 的 MCP 帧验证
接线逻辑（会话头回传、工具校验、轮询、导出），不冒充真实引擎成功（能力诚实）。
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))  # rea_music 包根在 sys.path


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """工件目录隔离：REA_MUSIC_HOME 指向 tmp，避免污染真实 workspace。"""
    monkeypatch.setenv("REA_MUSIC_HOME", str(tmp_path / "data"))
    return tmp_path


# ---------- fake 基础设施 ----------

class _FakeResponse:
    """最小 HTTPResponse 替身：headers 用小写键 dict（.get 语义兼容）。"""

    def __init__(self, body, headers: dict | None = None, status: int = 200):
        self._body = body if isinstance(body, bytes) else body.encode("utf-8")
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        self.status = status

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _offline(req, timeout=1):
    """模拟端口关闭：任何请求一律 URLError（连接拒绝）。"""
    raise urllib.error.URLError("connection refused (simulated offline)")


class _FakeStudio:
    """模拟 YuE2 Studio streamable HTTP MCP 端点（按官方文档实测协议）。"""

    def __init__(self, *, tools=None, wait_payloads=None, export_payload=None,
                 session_id="sess-abc-123"):
        self.session_id = session_id
        self.requests: list[dict] = []
        self.tools = tools if tools is not None else [
            {"name": n} for n in ("studio_status", "song_create", "studio_wait",
                                  "song_export", "models_catalog",
                                  "library_songs_list")]
        self.wait_payloads = wait_payloads or [
            {"job_id": "job_1", "status": "running"},
            {"job_id": "job_1", "status": "completed",
             "songs": [{"song_id": "s1"}]},
        ]
        self.export_payload = export_payload or {
            "status": "ok", "files": ["song_main.wav", "song_instrumental.wav"]}
        self.create_args: dict | None = None
        self.wait_args: dict | None = None
        self.export_args: dict | None = None
        self._wait_i = 0

    def __call__(self, req, timeout=1):
        headers = {k.lower(): v for k, v in req.headers.items()}
        if req.get_method() == "GET":
            self.requests.append({"payload": None, "headers": headers})
            return _FakeResponse("{}", status=200)
        payload = json.loads(req.data.decode("utf-8"))
        self.requests.append({"payload": payload, "headers": headers})
        method = payload.get("method")
        if method == "initialize":
            return _FakeResponse(json.dumps({
                "jsonrpc": "2.0", "id": 1,
                "result": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "yue2-studio", "version": "4.1.1"},
                }}), headers={"mcp-session-id": self.session_id})
        if method == "notifications/initialized":
            return _FakeResponse("")
        if method == "tools/list":
            return _FakeResponse(json.dumps(
                {"jsonrpc": "2.0", "id": 1, "result": {"tools": self.tools}}))
        if method == "tools/call":
            name = payload["params"]["name"]
            if name == "song_create":
                self.create_args = payload["params"]["arguments"]
                return self._tool({"job_id": "job_1", "status": "queued"})
            if name == "studio_wait":
                self.wait_args = payload["params"]["arguments"]
                item = self.wait_payloads[min(self._wait_i,
                                              len(self.wait_payloads) - 1)]
                self._wait_i += 1
                return self._tool(item)
            if name == "song_export":
                self.export_args = payload["params"]["arguments"]
                return self._tool(self.export_payload)
            return self._tool({"error": f"unknown tool {name}"})
        raise AssertionError(f"unexpected method: {method}")

    @staticmethod
    def _tool(obj) -> _FakeResponse:
        return _FakeResponse(json.dumps({
            "jsonrpc": "2.0", "id": 1,
            "result": {"content": [{"type": "text",
                                    "text": json.dumps(obj, ensure_ascii=False)}]},
        }))


def _plan(**over) -> dict:
    """lyrics_driven 模式的最小 CompositionPlan（含用户原创歌词切分）。"""
    plan = {
        "plan_id": "plan_test1234",
        "schema": "composition_plan_v1",
        "objective": "lyrics_driven",
        "user_request": "一首温暖的歌",
        "inherit": {
            "tempo_bpm": 120.0,
            "key": "C major",
            "meter_beats_per_measure": 4,
            "harmonic_flavor": None,
            "instrumentation_hint": "balanced, acoustic guitar",
            "energy_arc": [],
            "style_tags": ["indie pop"],
        },
        "sections": [
            {"label": "intro", "target_duration_sec": 8.0, "energy": "low"},
            {"label": "verse", "target_duration_sec": 32.0, "energy": "mid"},
            {"label": "chorus", "target_duration_sec": 32.0, "energy": "high"},
        ],
        "lyrics_guidance": {
            "mode": "user_provided",
            "line_count": 6,
            "sectioning_suggestion": [
                {"label": "intro", "lines": []},
                {"label": "verse", "lines": ["霓虹落在旧街口", "晚风把心事吹皱"]},
                {"label": "pre_chorus", "lines": ["我们都在等一个答案"]},
                {"label": "chorus", "lines": ["把夜唱成一首歌", "让光住进伤口"]},
            ],
        },
    }
    plan.update(over)
    return plan


# ---------- YuE2：studio 离线 → song_create 路径结构化降级 ----------

def test_yue2_offline_structured_degrade(monkeypatch):
    from rea_music.generation import backends
    monkeypatch.setattr(urllib.request, "urlopen", _offline)
    result = backends.generate_with_yue2({}, _plan())
    assert result["ok"] is False
    err = result["error"]
    assert err["code"] == "backend_unavailable"
    assert err["message"]
    assert err["hint"]  # Agent 可执行的恢复指引
    assert "suno" in result["fallback"].lower()
    # 活动记录如实反映探测即止
    assert result["activity"][0]["step"] == "probe"


# ---------- YuE2：style 句组装规范 ----------

def test_style_sentence_conforms():
    from rea_music.generation.backends import build_yue2_style
    style = build_yue2_style(_plan(), lyrics_text="霓虹落在旧街口 晚风把心事吹皱")
    assert style.endswith("120 BPM")  # 规范：末尾 "N BPM"
    assert "Mandarin" in style  # 语言成分由用户歌词 CJK 占比推出
    low = style.lower()
    for banned in ("c major", "major", "minor", "tonality"):
        assert banned not in low, f"style 泄漏调性: {style}"
    # 无歌手/歌名：计划本身不含，且组装层不引入
    for banned in ("taylor swift", "jay chou", "周董"):
        assert banned not in low


def test_style_filters_tonality_tags():
    from rea_music.generation.backends import build_yue2_style
    plan = _plan()
    plan["inherit"]["style_tags"] = ["indie pop", "C major vibes",
                                     "a minor ballad"]
    style = build_yue2_style(plan)
    assert "indie pop" in style
    assert "major" not in style.lower()
    assert "minor" not in style.lower()


def test_style_requires_bpm():
    from rea_music.core.errors import ValidationError
    from rea_music.generation.backends import build_yue2_style
    plan = _plan()
    plan["inherit"]["tempo_bpm"] = None
    with pytest.raises(ValidationError):
        build_yue2_style(plan)


# ---------- YuE2：lyrics 标签组装（含 lyrics_driven 用户歌词模式） ----------

def test_lyrics_user_provided_tags():
    from rea_music.generation.backends import build_yue2_lyrics
    lyrics = build_yue2_lyrics(_plan())
    lines = lyrics.splitlines()
    tags = [ln for ln in lines if ln.startswith("[")]
    # 每段一标签行（intro 无词也保留标签行）
    assert tags == ["[Intro]", "[Verse 1]", "[Pre-Chorus]", "[Chorus]"]
    assert "霓虹落在旧街口" in lines
    assert "把夜唱成一首歌" in lines
    # 用户词行为原创文本，绝不出现模板占位
    assert not any("original line" in ln for ln in lines)


def test_lyrics_verse_numbering():
    from rea_music.generation.backends import build_yue2_lyrics
    plan = _plan()
    plan["lyrics_guidance"]["sectioning_suggestion"] = [
        {"label": "verse", "lines": ["a"]},
        {"label": "chorus", "lines": ["b"]},
        {"label": "verse", "lines": ["c"]},
    ]
    tags = [ln for ln in build_yue2_lyrics(plan).splitlines()
            if ln.startswith("[")]
    assert tags == ["[Verse 1]", "[Chorus]", "[Verse 2]"]


def test_lyrics_user_tags_passthrough():
    from rea_music.generation.backends import build_yue2_lyrics
    plan = _plan()
    plan["lyrics_guidance"]["sectioning_suggestion"] = [
        {"label": "verse", "lines": ["[Verse 1]", "自带的词"]},
        {"label": "chorus", "lines": ["副歌词"]},
    ]
    lines = build_yue2_lyrics(plan).splitlines()
    assert lines == ["[Verse 1]", "自带的词", "[Chorus]", "副歌词"]


def test_lyrics_generate_new_mode_requires_user_text():
    from rea_music.core.errors import ValidationError
    from rea_music.generation.backends import build_yue2_lyrics
    plan = _plan()
    plan["lyrics_guidance"] = {"mode": "generate_new"}
    with pytest.raises(ValidationError):
        build_yue2_lyrics(plan)


def test_generate_new_plan_degrades_to_validation(monkeypatch):
    """generate_new 模式走真实链时：组装步骤即结构化 validation_error，不碰 song_create。"""
    from rea_music.generation import backends
    studio = _FakeStudio()
    monkeypatch.setattr(urllib.request, "urlopen", studio)
    plan = _plan()
    plan["lyrics_guidance"] = {"mode": "generate_new"}
    result = backends.generate_with_yue2({}, plan)
    assert result["ok"] is False
    assert result["error"]["code"] == "validation_error"
    assert result["fallback"]
    assert all(r["payload"] is None or r["payload"].get("method") != "tools/call"
               for r in studio.requests)


# ---------- _mcp_call 会话头逻辑（monkeypatch urllib 验证头回传） ----------

def test_mcp_call_session_header_roundtrip(monkeypatch):
    from rea_music.generation import backends
    studio = _FakeStudio()
    monkeypatch.setattr(urllib.request, "urlopen", studio)

    session: dict = {}
    resp = backends._mcp_call("http://127.0.0.1:8791/mcp", "initialize",
                             backends._INIT_PARAMS, session=session)
    # initialize 响应头里的 mcp-session-id 被捕获
    assert resp["result"]["serverInfo"]["name"] == "yue2-studio"
    assert session["session_id"] == "sess-abc-123"

    # 后续请求必须回传会话头（streamable HTTP 要求）
    backends._mcp_call("http://127.0.0.1:8791/mcp", "tools/list", {},
                       session=session)
    sent = studio.requests[-1]
    assert sent["headers"].get("mcp-session-id") == "sess-abc-123"
    assert sent["payload"]["method"] == "tools/list"

    # 无会话时不得携带会话头
    backends._mcp_call("http://127.0.0.1:8791/mcp", "tools/list", {},
                       session={})
    assert "mcp-session-id" not in studio.requests[-1]["headers"]


# ---------- YuE2：完整在线链路（fake 协议帧，验证接线而非冒充成功） ----------

def test_full_chain_online(monkeypatch, tmp_path):
    from rea_music.generation import backends
    studio = _FakeStudio(export_payload={
        "status": "ok", "files": ["song_main.wav", "song_instrumental.wav"]})
    monkeypatch.setattr(urllib.request, "urlopen", studio)
    result = backends.generate_with_yue2(
        {"yue2_wait_timeout_sec": 30, "yue2_export_format": "wav"}, _plan())
    assert result["ok"] is True, result
    assert result["backend"] == "yue2_studio_mcp"
    assert result["job_id"] == "job_1"
    # song_create 参数符合协议：title/style/lyrics 齐备且 style 合规
    create = studio.create_args
    assert create["title"] and create["lyrics"]
    assert create["style"].endswith("120 BPM")
    assert "[Verse 1]" in create["lyrics"]
    # song_export 参数：wav + 目标文件夹 workspace/artifacts/<plan_id>/
    export = studio.export_args
    assert export["format"] == "wav"
    assert export["target_folder"].replace("\\", "/").endswith(
        "artifacts/plan_test1234")
    assert Path(result["export_dir"]).exists()
    # 返回产物路径 + 耗时 + studio 活动记录
    assert len(result["exported_files"]) == 2
    assert result["elapsed_sec"] >= 0
    steps = [a["step"] for a in result["activity"]]
    assert steps[:4] == ["probe", "initialize", "tools/list", "compose_inputs"]
    assert "song_create" in steps and "studio_wait" in steps \
        and "song_export" in steps


def test_missing_tools_structured_degrade(monkeypatch):
    """tools/list 缺 song_create → 结构化 backend_unavailable，且绝不发 tools/call。"""
    from rea_music.generation import backends
    studio = _FakeStudio(tools=[{"name": "studio_status"},
                                {"name": "models_catalog"}])
    monkeypatch.setattr(urllib.request, "urlopen", studio)
    result = backends.generate_with_yue2({}, _plan())
    assert result["ok"] is False
    assert result["error"]["code"] == "backend_unavailable"
    assert "song_create" in result["error"]["message"]
    assert all(r["payload"] is None or r["payload"].get("method") != "tools/call"
               for r in studio.requests)


def test_wait_timeout_structured(monkeypatch):
    """轮询上限（默认 600s，此处 0s）到点 → 结构化 backend_unavailable 而非裸异常。"""
    from rea_music.generation import backends
    studio = _FakeStudio(wait_payloads=[{"job_id": "job_1", "status": "running"}])
    monkeypatch.setattr(urllib.request, "urlopen", studio)
    result = backends.generate_with_yue2({"yue2_wait_timeout_sec": 0}, _plan())
    assert result["ok"] is False
    assert result["error"]["code"] == "backend_unavailable"
    assert "上限" in result["error"]["message"]
    assert result["error"]["hint"]


def test_wait_disconnect_structured(monkeypatch):
    """studio_wait 中途失联（10 分钟空闲自退）→ 结构化 backend_unavailable。"""
    from rea_music.generation import backends
    studio = _FakeStudio()

    def flaky(req, timeout=1):
        payload = json.loads(req.data.decode("utf-8")) if req.data else {}
        if payload.get("method") == "tools/call" and \
                payload.get("params", {}).get("name") == "studio_wait":
            raise urllib.error.URLError("studio exited (simulated)")
        return studio(req, timeout)

    monkeypatch.setattr(urllib.request, "urlopen", flaky)
    result = backends.generate_with_yue2(
        {"yue2_wait_timeout_sec": 60}, _plan())
    assert result["ok"] is False
    assert result["error"]["code"] == "backend_unavailable"
    assert "studio_wait" in result["error"]["message"]
    assert "library_songs_list" in result["error"]["hint"]


def test_song_failed_status_structured(monkeypatch):
    """studio 报告任务失败 → 结构化 generation_failed，附响应摘要。"""
    from rea_music.generation import backends
    studio = _FakeStudio(wait_payloads=[
        {"job_id": "job_1", "status": "failed", "error": "model download"}])
    monkeypatch.setattr(urllib.request, "urlopen", studio)
    result = backends.generate_with_yue2(
        {"yue2_wait_timeout_sec": 30}, _plan())
    assert result["ok"] is False
    assert result["error"]["code"] == "generation_failed"
    assert "failed" in result["error"]["message"]


# ---------- generate_music(auto) 顺序：yue2 → ace_step → suno 包 ----------

def test_auto_falls_through_to_suno(monkeypatch):
    from rea_music.generation import backends
    monkeypatch.setattr(urllib.request, "urlopen", _offline)
    result = backends.generate_auto({}, _plan())
    assert result["ok"] is True
    assert result["backend"] == "suno_prompt_package"
    outcomes = [(a["backend"], a["outcome"]) for a in result["attempts"]]
    assert outcomes == [("yue2_studio_mcp", "backend_unavailable"),
                        ("ace_step", "capability_unavailable")]
    assert "[CHORUS]" in result["structured_lyrics_template"]
    assert result["next"].startswith("用户在 Suno")


def test_auto_prefers_online_yue2(monkeypatch):
    from rea_music.generation import backends
    studio = _FakeStudio()
    monkeypatch.setattr(urllib.request, "urlopen", studio)
    result = backends.generate_auto({}, _plan())
    assert result["ok"] is True
    assert result["backend"] == "yue2_studio_mcp"
    assert result["attempts"] == [{"backend": "yue2_studio_mcp",
                                   "outcome": "ok"}]


# ---------- ACE-Step 探测（R4-B） ----------

def test_acestep_probe_offline(monkeypatch):
    from rea_music.generation import acestep
    monkeypatch.setattr(urllib.request, "urlopen", _offline)
    status = acestep.probe({})
    assert status["available"] is False
    assert status["reason"] == "service_offline_or_port_closed"
    assert status["hint"]
    assert status["routes"] == []


def test_acestep_probe_openapi_discovery(monkeypatch, tmp_path):
    from rea_music.generation import acestep
    spec = {
        "openapi": "3.1.0",
        "paths": {
            "/health": {"get": {"operationId": "health", "summary": "健康检查",
                                 "parameters": []}},
            "/generate": {"post": {
                "operationId": "generate", "summary": "生成",
                "parameters": [{"name": "duration", "in": "query",
                                "required": False}],
                "requestBody": {"content": {"application/json": {}}}}},
        },
    }
    doc = tmp_path / "acestep_routes.md"

    def fake(req, timeout=1):
        if req.full_url.rstrip("/").endswith("/openapi.json"):
            return _FakeResponse(json.dumps(spec))
        return _FakeResponse('{"status": "up"}')

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    status = acestep.probe({"acestep_routes_doc": str(doc)})
    assert status["available"] is True
    routes = {(r["method"], r["path"]) for r in status["routes"]}
    assert routes == {("GET", "/health"), ("POST", "/generate")}
    gen = next(r for r in status["routes"] if r["path"] == "/generate")
    assert gen["json_body"] is True
    assert gen["params"] == [{"name": "duration", "in": "query",
                              "required": False}]
    # 路由清单落盘供主理人接线参考
    assert status["routes_doc"] == str(doc)
    assert doc.exists()
    content = doc.read_text(encoding="utf-8")
    assert "POST" in content and "/generate" in content
    assert "openapi.json" in content


def test_acestep_probe_service_up_but_no_openapi(monkeypatch):
    """服务在监听但 /openapi.json 非标准结构 → 如实降级，拒绝猜测调用。"""
    from rea_music.generation import acestep

    def fake(req, timeout=1):
        return _FakeResponse('{"status": "up"}')

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    status = acestep.probe({})
    assert status["available"] is False
    assert status["reason"] == "openapi_invalid"
    assert status["routes"] == []


def test_acestep_probe_openapi_unparseable(monkeypatch):
    """openapi 端点返回非 JSON → openapi_unreachable，同样不编造调用。"""
    from rea_music.generation import acestep

    def fake(req, timeout=1):
        if req.full_url.rstrip("/").endswith("/openapi.json"):
            return _FakeResponse("<html>not an api</html>")
        return _FakeResponse('{"status": "up"}')

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    status = acestep.probe({})
    assert status["available"] is False
    assert status["reason"] == "openapi_unreachable"


# ---------- config 默认值（R4 + R5 预置） ----------

def test_config_generation_defaults():
    from rea_music.core.config import load_config
    gen = load_config()["generation"]
    assert gen["yue2_mcp_url"] == "http://127.0.0.1:8791/mcp"
    assert gen["yue2_wait_timeout_sec"] == 600
    assert gen["yue2_export_format"] == "wav"
    assert gen["acestep_base_url"] == "http://127.0.0.1:8001"
    opt = gen["optimization"]
    assert opt["max_iterations"] == 3
    assert opt["satisfied_repeat_stop"] == 2
