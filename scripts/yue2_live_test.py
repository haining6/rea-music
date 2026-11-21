"""YuE2 Studio 真实生成联调脚本（用户开窗模式，v4.0.0 实测契约）。

用法：保持 YuE2 Studio 开启（一键启动.cmd），然后运行：
    PYTHONPATH=src .venv/Scripts/python.exe scripts/yue2_live_test.py [--wait-min 90]

流程：等端口 → MCP 握手 → tools/list 落盘 → song_create（真实生成）→
studio_wait 轮询（done 判据）→ 从返回态提取 song 列表 → library_song_files
取磁盘路径 → 复制进 data/artifacts/yue2_live/ → 记录 examples/yue2_live_record.json。

实测要点（v4.0.0）：
- song_create 返回 {"id": "<job_id>", "status": "queued", ...}（字段是 id，非 job_id）
- studio_wait 返回 {"done": bool, "state": {...}}，job 列表在 state.song_jobs
- 无 song_export 工具（v4.1.1 才有），导出走 library_song_files 拿磁盘路径
- studio 空闲 10 分钟自退；运行中的 job 视为在用，不会中途退出
"""

from __future__ import annotations

import argparse
import json
import shutil
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
URL = "http://127.0.0.1:8791/mcp"
RECORD_PATH = REPO / "examples" / "yue2_live_record.json"
ARTIFACT_DIR = REPO / "data" / "artifacts" / "yue2_live"
TITLE = "REA-Music live test"


def post(payload, timeout=300):
    headers = {"Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"}
    req = urllib.request.Request(URL, data=json.dumps(payload).encode(),
                                 headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", "replace")
    if body.lstrip().startswith(("event:", "data:")):
        for ln in reversed(body.splitlines()):
            if ln.startswith("data:"):
                try:
                    return json.loads(ln[5:].strip())
                except json.JSONDecodeError:
                    continue
        return {}
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return {"raw": body[:400]}


def wait_for_port(minutes: float) -> bool:
    deadline = time.time() + minutes * 60
    while time.time() < deadline:
        try:
            urllib.request.urlopen(urllib.request.Request(URL, method="GET"),
                                   timeout=3)
            return True
        except urllib.error.HTTPError:
            return True  # 405 等：端口在监听
        except (urllib.error.URLError, TimeoutError, OSError):
            time.sleep(4)
    return False


def call(name, arguments):
    r = post({"jsonrpc": "2.0", "id": int(time.time() * 1000) % 10**6,
              "method": "tools/call",
              "params": {"name": name, "arguments": arguments}})
    res = (r or {}).get("result", {})
    text = (res.get("content") or [{}])[0].get("text", "")
    try:
        return json.loads(text), text
    except (json.JSONDecodeError, TypeError):
        return None, text


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wait-min", type=float, default=90)
    ap.add_argument("--style", type=str, default=(
        "Mandarin pop ballad from the 2020s, warm female vocal with gentle "
        "delivery, soft piano and light strings with a subtle drum groove, "
        "hopeful and tender mood, clean modern production, 92 BPM"))
    ap.add_argument("--lyrics", type=str, default="\n".join([
        "[Intro]", "(piano only)", "",
        "[Verse 1]", "夜色刚刚落下 城市亮起灯火", "我在街角听见 一首熟悉的歌", "",
        "[Chorus]", "把心事交给风 交给远方", "明天的路会在 晨光里发亮",
    ]))
    args = ap.parse_args()
    record: dict = {"started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}

    if not wait_for_port(args.wait_min):
        record["error"] = f"studio 未在 {args.wait_min} 分钟内上线（127.0.0.1:8791）"
        RECORD_PATH.write_text(json.dumps(record, ensure_ascii=False, indent=1),
                               encoding="utf-8")
        print(json.dumps(record, ensure_ascii=False))
        raise SystemExit(1)
    print("[0] studio online")
    record["studio_online"] = True

    init = post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"protocolVersion": "2025-06-18",
                            "capabilities": {},
                            "clientInfo": {"name": "rea-music",
                                           "version": "0.1"}}})
    assert init and init.get("result"), f"initialize 失败: {init}"
    post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    st, _ = call("studio_status", {})
    print("[1] studio_status:", json.dumps(st, ensure_ascii=False)[:160])
    record["studio_status"] = st

    tools = post({"jsonrpc": "2.0", "id": 90, "method": "tools/list",
                  "params": {}})
    schemas = {t["name"]: t.get("inputSchema", {})
               for t in tools.get("result", {}).get("tools", [])}
    record["tool_schemas_captured"] = sorted(schemas)
    (REPO / "examples" / "yue2_tool_schemas.json").write_text(
        json.dumps(schemas, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[2] tools captured: {len(schemas)}")

    t0 = time.time()
    created, _raw = call("song_create", {
        "title": TITLE, "style": args.style, "lyrics": args.lyrics})
    job_id = (created or {}).get("id")
    record["song_create"] = created
    print("[3] song_create job:", job_id, "| status:", (created or {}).get("status"))

    final = {}
    deadline = time.time() + 1500  # 25 分钟生成窗口
    while time.time() < deadline:
        final, _ = call("studio_wait",
                        {"job_id": job_id} if job_id else {"until": "song_jobs"})
        done = bool((final or {}).get("done"))
        print(f"[4] studio_wait done={done} elapsed={round(time.time()-t0)}s")
        if done:
            break
        time.sleep(5)
    record["studio_wait_final"] = final

    # 从完成后状态提取 library song id
    song_ids = _collect_song_ids(final)
    if not song_ids:
        lst, _ = call("library_songs_list", {"query": TITLE, "since": "today"})
        song_ids = _collect_song_ids(lst)
    print("[5] song ids:", song_ids)

    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for sid in song_ids[:2]:
        files_resp, _ = call("library_song_files", {"song_id": sid})
        for path in _collect_paths(files_resp):
            src = Path(path)
            if src.exists() and src.suffix.lower() in (".wav", ".flac",
                                                       ".mp3", ".ogg"):
                dst = ARTIFACT_DIR / f"{sid}{src.suffix.lower()}"
                shutil.copy2(src, dst)
                copied.append(str(dst))
    record["exported_files"] = copied
    record["wall_seconds"] = round(time.time() - t0, 1)
    record["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    RECORD_PATH.write_text(json.dumps(record, ensure_ascii=False, indent=1),
                           encoding="utf-8")
    print(json.dumps({"job": job_id, "song_ids": song_ids,
                      "exported": copied,
                      "wall_sec": record["wall_seconds"]},
                     ensure_ascii=False, indent=1))
    print("RECORD ->", RECORD_PATH)


def _collect_song_ids(node) -> list[str]:
    """收集 library song id：优先 songs[] 列表里的 id（v4.0.0 实测结构），
    其次兼容带 status/audio_files 的节点。"""
    ids: list[str] = []

    def walk(n):
        if isinstance(n, dict):
            for k, v in n.items():
                if k == "songs" and isinstance(v, list):
                    for s in v:
                        if isinstance(s, dict) and isinstance(s.get("id"), str):
                            ids.append(s["id"])
                walk(v)
        elif isinstance(n, list):
            for item in n:
                walk(item)

    walk(node)
    if ids:
        return list(dict.fromkeys(ids))
    # 兜底：老逻辑
    def walk2(n):
        if isinstance(n, dict):
            if n.get("status") in ("completed", "done") or "audio_files" in n:
                sid = n.get("id") or n.get("song_id")
                if isinstance(sid, str):
                    ids.append(sid)
            for v in n.values():
                walk2(v)
        elif isinstance(n, list):
            for item in n:
                walk2(item)

    walk2(node)
    return list(dict.fromkeys(ids))


def _collect_paths(node) -> list[str]:
    out: list[str] = []

    def walk(n):
        if isinstance(n, dict):
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for item in n:
                walk(item)
        elif isinstance(n, str) and len(n) > 8 and n.lower().endswith(
                (".wav", ".flac", ".mp3", ".ogg")):
            out.append(n)

    walk(node)
    return out


if __name__ == "__main__":
    main()
