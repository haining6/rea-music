"""REA-Music CLI。与 MCP Server 调用同一 core，命令与工具一一对应。"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import click

from rea_music import __version__
from rea_music.cli.cache import cache as cache_group


def _echo_json(data: dict) -> None:
    click.echo(json.dumps(data, ensure_ascii=False, indent=1))


def _err(exc) -> None:
    _echo_json(exc.to_dict() if hasattr(exc, "to_dict") else
               {"ok": False, "error": {"code": "internal_error",
                                       "message": str(exc)}})
    sys.exit(1)


@click.group()
@click.version_option(__version__)
def cli() -> None:
    """REA-Music：Agent-Native 音乐逆向分析与创作。"""


@cli.command("capabilities")
def capabilities() -> None:
    """显示真实引擎能力与硬件状态。"""
    from rea_music.adapters import registry
    from rea_music.core.config import config_status

    payload = registry.capabilities()
    # 配置来源与错误必须如实暴露：用户把格式写错时，"默认值生效"这种行为
    # 否则无法解释（配置模板是 JSON，不是 YAML）。
    payload.update(config_status())
    _echo_json(payload)


@cli.command("inspect")
@click.argument("path")
def inspect(path: str) -> None:
    """验证输入源并输出元信息与内容指纹。path 支持本地文件或 http(s) 链接。"""
    from rea_music.analysis.inspect import inspect_audio
    try:
        _echo_json(inspect_audio(path))
    except Exception as exc:  # noqa: BLE001
        _err(exc)


def _analyze_one(path: str, tier: str, force: bool) -> dict:
    """跑一次完整分析（inspect → Job → DNA）。

    Args:
        path: 本地路径或 http(s) 链接。
        tier: fast / standard / deep。
        force: True 时忽略缓存重算。

    Returns:
        ``{dna_path, dna, source, content_hash}``。

    Raises:
        REAError: 输入无法解析或 Job 失败（code 沿用 Job 记录中的错误码）。
    """
    from rea_music.analysis.inspect import inspect_audio
    from rea_music.analysis import pipeline as pipeline_mod
    from rea_music.core import jobs as jobs_mod
    from rea_music.core.errors import REAError

    meta = inspect_audio(path)          # 先解析输入源（含下载）并校验
    audio_path = meta["path"]
    job = jobs_mod.create_job("analyze_track",
                              {"audio_path": audio_path, "tier": tier,
                               "content_hash": meta["content_hash"]})
    job = jobs_mod.run_inline(pipeline_mod.analyze_track_job, job,
                              audio_path, tier, force)
    if job["status"] != "succeeded":
        error = job.get("error") or {}
        # 底层语义（reason/requires…）必须带出去：Agent 靠它区分"长度未知"与
        # "m4a 需要 FFmpeg"，只给 message 无法分流处理。
        details = {"job_id": job["job_id"], "status": job["status"]}
        details.update(error.get("details") or {})
        raise REAError(error.get("message") or f"分析任务 {job['job_id']} 未成功",
                       code=error.get("code") or "analysis_failed",
                       hint=error.get("hint")
                            or "用 rea-music jobs --job-id <id> 查看任务详情。",
                       details=details)
    with open(job["result_ref"], encoding="utf-8") as f:
        dna = json.load(f)
    return {"dna_path": job["result_ref"], "dna": dna,
            "source": meta["source"], "content_hash": meta["content_hash"]}


def _dna_scalar(dna: dict, section: str, field: str) -> float | str | None:
    """取 DNA 中 field-with-provenance 包装的标量值，缺失返回 None。"""
    wrapped = (dna.get(section) or {}).get(field) or {}
    return wrapped.get("value") if isinstance(wrapped, dict) else None


def _error_payload(exc: Exception) -> dict:
    """统一错误摘要（REAError 保留 code/hint，其他异常归为 internal_error）。"""
    if hasattr(exc, "to_dict"):
        return exc.to_dict().get("error", {"code": "internal_error",
                                          "message": str(exc)})
    return {"code": "internal_error", "message": str(exc)}


def _scan_batch(root: Path, limit: int | None) -> tuple[list[Path], int]:
    """递归扫描目录内受支持的媒体文件。

    跳过隐藏文件/目录与 ``.part`` 半截下载，按路径排序保证结果可复现。

    Args:
        root: 待扫描目录。
        limit: 只取前 N 个；None 表示不限。

    Returns:
        ``(files, matched)``，matched 为截断前的命中总数。
    """
    from rea_music.core import source as source_mod

    known = {ext.lower() for ext in source_mod.accepted_formats()}
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for name in sorted(filenames):
            if name.startswith(".") or name.endswith(".part"):
                continue
            candidate = Path(dirpath) / name
            if candidate.suffix.lower() in known:
                found.append(candidate)
    found.sort(key=lambda p: str(p).lower())
    matched = len(found)
    return (found[:limit] if limit else found), matched


def _analyze_batch(batch_dir: str, tier: str, force: bool,
                   limit: int | None) -> dict:
    """批量分析目录内的媒体文件，单文件失败不中断。

    Args:
        batch_dir: 目录路径。
        tier: fast / standard / deep。
        force: True 时忽略缓存重算。
        limit: 只处理前 N 个文件。

    Returns:
        汇总表：每文件 {file, ok, dna_path|error, bpm, key, elapsed_sec}
        + total/succeeded/failed/elapsed_sec。

    Raises:
        FileNotFound: 目录不存在。
        ValidationError: 传入的不是目录，或目录内没有受支持文件。
    """
    from rea_music.core import source as source_mod
    from rea_music.core.errors import FileNotFound, ValidationError

    root = Path(batch_dir).expanduser()
    if not root.exists():
        raise FileNotFound(f"目录不存在: {batch_dir}",
                           hint="传已存在的目录，例如 rea-music analyze --batch ./songs")
    if not root.is_dir():
        raise ValidationError(f"{batch_dir} 不是目录",
                              hint="--batch 需要目录；单个文件直接用位置参数 path。")
    files, matched = _scan_batch(root, limit)
    if not files:
        raise ValidationError(
            f"目录内没有受支持的音频文件: {root}",
            hint="支持扩展名：" + "、".join(source_mod.accepted_formats()))

    started = time.perf_counter()
    results: list[dict] = []
    for file_path in files:
        file_started = time.perf_counter()
        try:
            out = _analyze_one(str(file_path), tier, force)
            results.append({
                "file": str(file_path),
                "ok": True,
                "dna_path": out["dna_path"],
                "content_hash": out["content_hash"],
                "bpm": _dna_scalar(out["dna"], "rhythm", "bpm"),
                "key": _dna_scalar(out["dna"], "tonality", "key"),
                "elapsed_sec": round(time.perf_counter() - file_started, 2),
            })
        except Exception as exc:  # noqa: BLE001 —— 单文件失败不中断整批
            results.append({
                "file": str(file_path),
                "ok": False,
                "error": _error_payload(exc),
                "elapsed_sec": round(time.perf_counter() - file_started, 2),
            })
    succeeded = sum(1 for item in results if item["ok"])
    return {
        "ok": succeeded == len(results),
        "mode": "batch",
        "batch": str(root),
        "tier": tier,
        "limit": limit,
        "matched": matched,
        "total": len(results),
        "succeeded": succeeded,
        "failed": len(results) - succeeded,
        "elapsed_sec": round(time.perf_counter() - started, 2),
        "results": results,
        "note": ("ok 表示全部文件成功；有失败项但至少一个成功时退出码仍为 0，"
                 "便于批量跑完再逐条查看 error"),
    }


@cli.command("analyze")
@click.argument("path", required=False)
@click.option("--batch", "batch_dir", default=None, metavar="DIRECTORY",
              help="批量分析目录内受支持的音频文件（递归，单文件失败不中断）")
@click.option("--tier", type=click.Choice(["fast", "standard", "deep"]),
              default="standard")
@click.option("--force", is_flag=True, help="忽略缓存重算")
@click.option("--limit", type=int, default=None,
              help="配合 --batch：只处理排序后前 N 个文件")
def analyze(path: str | None, batch_dir: str | None, tier: str, force: bool,
            limit: int | None) -> None:
    """同步分析并打印 Music DNA（compact 视图）。path 支持本地文件或 http(s) 链接。

    批量模式：rea-music analyze --batch ./songs --tier fast --limit 10，
    输出每文件 {file, ok, dna_path|error, bpm, key, elapsed_sec} 的汇总表。
    """
    from rea_music.core.resolve import compact_dna

    if batch_dir and path:
        raise click.UsageError("--batch 与位置参数 path 不能同时使用")
    if not batch_dir and not path:
        raise click.UsageError("需要提供 path，或用 --batch <目录> 批量分析")
    if limit is not None and not batch_dir:
        raise click.UsageError("--limit 只在 --batch 模式下有效")
    if limit is not None and limit < 1:
        raise click.UsageError("--limit 必须 >= 1")
    try:
        if batch_dir:
            summary = _analyze_batch(batch_dir, tier, force, limit)
            _echo_json(summary)
            if summary["failed"] and not summary["succeeded"]:
                sys.exit(1)             # 全部失败才算整体失败
            return
        out = _analyze_one(str(path), tier, force)
        _echo_json({"ok": True, "dna_path": out["dna_path"],
                    "source": out["source"], "dna": compact_dna(out["dna"])})
    except Exception as exc:  # noqa: BLE001
        _err(exc)


@cli.command("compare")
@click.argument("ref_a")
@click.argument("ref_b")
def compare(ref_a: str, ref_b: str) -> None:
    """两首已分析作品的多维比较。参数为哈希前缀或路径。"""
    from rea_music.comparison.compare import compare_tracks
    from rea_music.core.resolve import load_dna
    try:
        _echo_json(compare_tracks(load_dna(ref_a), load_dna(ref_b)))
    except Exception as exc:  # noqa: BLE001
        _err(exc)


@cli.command("plan")
@click.argument("dna_refs", nargs=-1, required=True)
@click.option("--objective",
              type=click.Choice(["inherit_style_new_song", "blend_styles",
                                 "modify_dimension", "lyrics_driven"]),
              default="inherit_style_new_song")
@click.option("--request", "user_request", default=None)
@click.option("--lyrics", "user_lyrics", default=None)
def plan(dna_refs: tuple[str, ...], objective: str, user_request: str | None,
         user_lyrics: str | None) -> None:
    """从 Music DNA 生成原创创作方案。"""
    from rea_music.core.resolve import load_dna
    from rea_music.generation.plan import create_composition_plan
    try:
        dnas = [load_dna(r) for r in dna_refs]
        result = create_composition_plan(dna_refs=dnas, objective=objective,
                                         user_request=user_request,
                                         user_lyrics=user_lyrics)
        _echo_json(result)
    except Exception as exc:  # noqa: BLE001
        _err(exc)


@cli.command("render")
@click.argument("plan_ref")
@click.option("--backend", type=click.Choice(["suno", "yue2", "generic"]),
              default="suno")
def render(plan_ref: str, backend: str) -> None:
    """把创作方案渲染为后端请求。"""
    from rea_music.core.config import load_config
    from rea_music.core.resolve import load_plan
    from rea_music.generation.backends import render_generation_request
    try:
        result = render_generation_request(load_plan(plan_ref), backend,
                                            load_config().get("generation"))
        _echo_json(result)
    except Exception as exc:  # noqa: BLE001
        _err(exc)


@cli.command("evaluate")
@click.argument("generated_path")
@click.argument("target_ref")
def evaluate(generated_path: str, target_ref: str) -> None:
    """生成作品 vs 目标 DNA 的闭环评测。generated_path 支持本地文件或 http(s) 链接。"""
    from rea_music.core import source as source_mod
    from rea_music.core.resolve import load_dna
    from rea_music.evaluation.evaluate import evaluate_generation
    try:
        target = load_dna(target_ref)
        generated = source_mod.resolve_source(generated_path)["local_path"]
        _echo_json(evaluate_generation(generated_audio_path=generated,
                                       target_dna=target))
    except Exception as exc:  # noqa: BLE001
        _err(exc)


@cli.command("jobs")
@click.option("--job-id", default=None)
def jobs(job_id: str | None) -> None:
    """列出任务或查看单个任务状态。"""
    from rea_music.core import jobs as jobs_mod
    try:
        if job_id:
            _echo_json(jobs_mod.get_job(job_id))
        else:
            _echo_json({"ok": True, "jobs": jobs_mod.list_jobs()})
    except Exception as exc:  # noqa: BLE001
        _err(exc)


_LOOPBACK_HOSTS: tuple[str, ...] = ("127.0.0.1", "::1", "localhost")


@cli.command("serve")
@click.option("--transport", type=click.Choice(["stdio", "http"]),
              default="stdio", show_default=True)
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8912, show_default=True)
@click.option("--allow-external", is_flag=True,
              help="允许把无鉴权的 HTTP 服务绑到非回环地址（危险，见 SECURITY.md）")
def serve(transport: str, host: str, port: int, allow_external: bool) -> None:
    """启动 MCP Server（HTTP 默认只听回环 127.0.0.1）。

    HTTP 通道**没有任何鉴权**，它等价于把本机全部 CLI 能力（读写 data/、调用解码器、
    拉起可选引擎子进程）暴露给能连上这个端口的人。因此非回环绑定必须显式加
    --allow-external 二次确认——ARCHITECTURE.md 原则 7「HTTP 只听回环」由这里保证，
    而不是只写在文档里。
    """
    normalized = (host or "").strip().lower()
    if normalized not in _LOOPBACK_HOSTS and not allow_external:
        raise click.UsageError(
            f"--host {host} 不是回环地址，HTTP 通道无鉴权，拒绝启动。\n"
            f"确实需要网络暴露时，请显式加 --allow-external 并自行承担风险"
            f"（详见 SECURITY.md 的攻击面与边界一节）。")

    from rea_music.mcp.server import serve as _serve
    _serve("stdio" if transport == "stdio" else "streamable-http",
           host, port)


cli.add_command(cache_group)


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
