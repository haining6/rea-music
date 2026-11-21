"""REA-Music MCP Server（stdio 默认，可选 streamable HTTP 仅监听 127.0.0.1）。

14 个语义级工具，机器可读 schema 由官方 SDK 从类型注解自动生成。
CLI 与本 Server 调用同一 core 实现，无业务逻辑复制。
mcp SDK 2.x：FastMCP 已更名 MCPServer（from mcp.server import MCPServer）。
"""

from __future__ import annotations

import json
from typing import Annotated, Literal

from mcp.server import MCPServer

from rea_music import __version__
from rea_music.adapters import registry
from rea_music.analysis import inspect as inspect_mod
from rea_music.analysis import pipeline as pipeline_mod
from rea_music.comparison.compare import compare_tracks, blend_music_dna
from rea_music.core import jobs as jobs_mod
from rea_music.core import source as source_mod
from rea_music.core.config import load_config
from rea_music.core.errors import CapabilityUnavailable, REAError
from rea_music.core.resolve import (compact_dna, load_dna, load_evaluation,
                                        load_plan)
from rea_music.evaluation.evaluate import evaluate_generation
from rea_music.generation.backends import (generate_with_yue2,
                                               render_generation_request,
                                               yue2_probe)
from rea_music.generation.plan import create_composition_plan

mcp = MCPServer(
    "rea-music", version=__version__, log_level="WARNING",
    instructions=(
        "REA-Music：音乐逆向分析与创作能力层。先调 rea_music_capabilities 了解真实能力；"
        "音频入参支持本地任意常见格式、http(s) 直链媒体与视频页面链接（视频页需 yt-dlp）；"
        "长任务用 job 模式轮询；字段置信度与 missing_reason 必须如实转达给用户；"
        "禁止把低置信度启发式结果当作确定结论。"
    ),
)

_P = Annotated[str, "本地音频文件绝对路径 / http(s) 直链媒体 / 视频页面链接 / "
                    "内容哈希（支持 8 位以上前缀）"]
_T = Annotated[str, "detail=compact 剥离长数组，full 返回完整数据"]


@mcp.tool(name="rea_music_capabilities")
def capabilities() -> dict:
    """返回可用分析器、生成后端、输入类型支持与功能边界。任何任务开始前先调用本工具。"""
    from rea_music.core.config import config_status

    payload = registry.capabilities()
    # 与 CLI 保持同一口径：配置来源/错误如实暴露，避免"默认值生效"无法解释
    payload.update(config_status())
    return payload


@mcp.tool(name="rea_music_inspect_audio")
def inspect_audio(path: _P, compute_hash: bool = True) -> dict:
    """验证音频并返回时长/采样率/声道/编码/内容哈希。不启动分析。
    path 支持本地任意常见格式、http(s) 直链媒体、视频页面链接（需 yt-dlp）。"""
    return _safe(lambda: inspect_mod.inspect_audio(path, compute_hash=compute_hash))


@mcp.tool(name="rea_music_analyze_track")
def analyze_track(path: _P,
                  tier: Literal["fast", "standard", "deep"] = "standard",
                  force: bool = False,
                  wait: bool = False) -> dict:
    """启动音乐分析 Job。返回 job_id 轮询，或 wait=true 同步取结果。
    tier: fast=秒级概要, standard=含和弦/曲式/段落DNA, deep=尝试分轨级精细分析。
    path 支持本地文件、http(s) 直链媒体与视频页面链接。
    """
    def _run():
        # 先解析输入源（必要时下载）并验证，把错误立即反馈而不是在后台 Job 里失败
        meta = inspect_mod.inspect_audio(path)
        audio_path = meta["path"]
        params = {"audio_path": audio_path, "tier": tier,
                  "content_hash": meta["content_hash"],
                  "source_kind": meta["source"]["kind"],
                  "origin_url": meta["source"]["origin_url"]}
        job = jobs_mod.create_job("analyze_track", params)
        if wait:
            job = jobs_mod.run_inline(pipeline_mod.analyze_track_job, job,
                                      audio_path, tier, force)
        else:
            jobs_mod.submit(pipeline_mod.analyze_track_job, job,
                            audio_path, tier, force)
        return {
            "ok": True, "job_id": job["job_id"], "status": job["status"],
            "content_hash": meta["content_hash"],
            "song_id": f"sha256:{meta['content_hash'][:16]}",
            "source": meta["source"],
            "next": "用 rea_music_get_job_status 轮询；完成后 get_analysis 取 DNA",
        }
    return _safe(_run)


@mcp.tool(name="rea_music_get_analysis")
def get_analysis(ref: _P,
                 tier: Literal["fast", "standard", "deep"] = "standard",
                 detail: Literal["compact", "full"] = "compact") -> dict:
    """获取 Music DNA。ref 可以是内容哈希前缀、song_id 或音频路径。"""
    def _run():
        dna = load_dna(ref, tier)
        return compact_dna(dna) if detail == "compact" else dna
    return _safe(_run)


@mcp.tool(name="rea_music_get_job_status")
def get_job_status(job_id: str) -> dict:
    """查询任务状态、当前阶段、进度与错误详情。"""
    return _safe(lambda: jobs_mod.get_job(job_id))


@mcp.tool(name="rea_music_extract_music_dna")
def extract_music_dna(path: _P,
                      tier: Literal["fast", "standard", "deep"] = "standard",
                      force: bool = False) -> dict:
    """一步到位：确保分析完成并直接返回 Music DNA（同步等待）。
    path 支持本地文件、http(s) 直链媒体与视频页面链接。"""
    def _run():
        meta = inspect_mod.inspect_audio(path)
        audio_path = meta["path"]
        job = jobs_mod.create_job("extract_dna", {
            "audio_path": audio_path, "tier": tier,
            "content_hash": meta["content_hash"]})
        job = jobs_mod.run_inline(pipeline_mod.analyze_track_job, job,
                                  audio_path, tier, force)
        if job["status"] != "succeeded":
            return {"ok": False, "job": job,
                    "error_hint": "查看 job.error，修复文件或档位后重试"}
        with open(job["result_ref"], encoding="utf-8") as f:
            dna = json.load(f)
        return {"ok": True, "content_hash": meta["content_hash"],
                "source": meta["source"],
                "dna_path": job["result_ref"], "dna": dna}
    return _safe(_run)


@mcp.tool(name="rea_music_separate_stems")
def separate_stems(path: _P, model: str = "htdemucs",
                   wait: bool = False) -> dict:
    """分轨（vocals/drums/bass/other）。依赖 demucs 引擎，未安装时如实报告。
    CPU 分轨耗时约 1.5x 曲长，默认 job 化返回 job_id；wait=true 同步等待短音频。
    path 支持本地文件、http(s) 直链媒体与视频页面链接。"""
    def _run():
        from rea_music.adapters import demucs as demucs_mod
        # 先解析输入源并验证，把错误立即反馈而不是在后台 Job 里失败
        meta = inspect_mod.inspect_audio(path)
        status = registry.separation_status()
        if not status["available"]:
            raise CapabilityUnavailable(
                "分轨引擎不可用",
                details={**status, "unavailable_reason":
                         status.get("unavailable_reason")},
                hint=status["install_hint"] +
                     "；安装属大体积下载，必须先获得用户批准")
        audio_path = meta["path"]
        params = {"audio_path": audio_path, "tier": "deep",
                  "content_hash": meta["content_hash"], "model": model}
        job = jobs_mod.create_job("separate_stems", params)
        if wait:
            job = jobs_mod.run_inline(
                demucs_mod.separate_stems_job, job,
                audio_path, meta["content_hash"], model)
        else:
            jobs_mod.submit(demucs_mod.separate_stems_job, job,
                            audio_path, meta["content_hash"], model)
        manifest_hint = demucs_mod.stems_out_dir(
            meta["content_hash"], model)
        if job["status"] == "succeeded":
            return {
                "ok": True, "job_id": job["job_id"], "status": job["status"],
                "content_hash": meta["content_hash"], "model": model,
                "manifest_path": job["result_ref"],
                "stems_dir": str(manifest_hint),
                "runtime": status.get("runtime"),
                "elapsed_sec_note": "CPU 分轨耗时约 1.5x 曲长；"
                                     "重复调用同文件直接命中 manifest 缓存",
                "next": "用 rea_music_get_job_status 轮询；完成后 manifest 内含"
                        "各轨路径与 rms",
            }
        return {"ok": True, "job_id": job["job_id"], "status": job["status"],
                "content_hash": meta["content_hash"],
                "engine": status,
                "next": "用 rea_music_get_job_status 轮询进度"}
    return _safe(_run)


@mcp.tool(name="rea_music_compare_tracks")
def compare(ref_a: _P, ref_b: _P,
            tier: Literal["fast", "standard", "deep"] = "standard") -> dict:
    """两首作品的多维量化比较：节奏/调性/能量/音色/响度/曲式。"""
    def _run():
        a, b = load_dna(ref_a, tier), load_dna(ref_b, tier)
        return compare_tracks(a, b)
    return _safe(_run)


@mcp.tool(name="rea_music_blend_music_dna")
def blend(refs: list[str],
          dimension_map: dict[str, int] | None = None,
          tier: Literal["fast", "standard", "deep"] = "standard") -> dict:
    """多曲风格融合。dimension_map 指定各维度来源索引，如
    {"tempo": 0, "harmony": 1, "structure": 2}。只融合统计特征，不搬运受保护内容。"""
    def _run():
        dnas = [load_dna(r, tier) for r in refs]
        return blend_music_dna(dnas, dimension_map or {})
    return _safe(_run)


@mcp.tool(name="rea_music_create_composition_plan")
def create_plan(dna_refs: list[str],
                objective: Literal["inherit_style_new_song", "blend_styles",
                                   "modify_dimension", "lyrics_driven"],
                user_request: str | None = None,
                dimension_map: dict[str, int] | None = None,
                target_modifications: dict | None = None,
                user_lyrics: str | None = None,
                tier: Literal["fast", "standard", "deep"] = "standard") -> dict:
    """把 Music DNA 转换为原创创作方案 CompositionPlan。四种目标模式；
    歌词驱动必须传 user_lyrics（用户原创文本）。"""
    def _run():
        dnas = [load_dna(r, tier) for r in dna_refs]
        plan = create_composition_plan(
            dna_refs=dnas, objective=objective, user_request=user_request,
            dimension_map=dimension_map, target_modifications=target_modifications,
            user_lyrics=user_lyrics)
        return plan
    return _safe(_run)


@mcp.tool(name="rea_music_render_generation_request")
def render_request(plan_ref: str | dict,
                   backend: Literal["suno", "yue2", "generic"] = "suno") -> dict:
    """把 CompositionPlan 渲染为后端请求：Suno 提示词包 / YuE2 结构化请求 / 通用。"""
    def _run():
        plan = load_plan(plan_ref)
        cfg = load_config()
        return render_generation_request(plan, backend, cfg.get("generation"))
    return _safe(_run)


@mcp.tool(name="rea_music_generate_music")
def generate_music(plan_ref: str | dict,
                   backend: Literal["auto", "yue2", "suno"] = "auto") -> dict:
    """调用真实可用的生成后端。auto: YuE2 Studio 在线则真实生成并导出，
    否则探测 ACE-Step（在线则走 REST），两者离线返回 Suno 提示词包。
    不伪造任何接口，studio 须由用户上下文拉起（D18）。"""
    def _run():
        plan = load_plan(plan_ref)
        cfg = load_config().get("generation", {})
        if backend == "auto":
            from rea_music.generation.backends import generate_auto
            return generate_auto(cfg, plan)
        if backend == "yue2":
            result = generate_with_yue2(cfg, plan)
            if result.get("ok"):
                return result
            return {"ok": True, "backend": "suno_prompt_package",
                    **render_generation_request(plan, "suno", cfg),
                    "note": "YuE2 Studio 离线；产出提示词包供 Suno 人工流程",
                    "next": "用户在 Suno 创作完成后，把音频文件传回 "
                            "rea_music_evaluate_generation 闭环评测"}
        return {"ok": True, "backend": "suno_prompt_package",
                **render_generation_request(plan, "suno", cfg),
                "next": "用户在 Suno 创作完成后，把音频文件传回 "
                        "rea_music_evaluate_generation 闭环评测"}
    return _safe(_run)


@mcp.tool(name="rea_music_evaluate_generation")
def evaluate(generated_path: _P, target_ref: _P,
             tier: Literal["fast", "standard", "deep"] = "standard") -> dict:
    """重新分析生成作品并与目标 DNA 逐维比较：符合度/技术质量/原创性风险/修订建议。
    generated_path 支持本地文件、http(s) 直链媒体与视频页面链接。"""
    def _run():
        target = load_dna(target_ref, tier)
        generated = source_mod.resolve_source(generated_path)["local_path"]
        return evaluate_generation(generated_audio_path=generated,
                                   target_dna=target, tier=tier)
    return _safe(_run)


def _as_number(value) -> float | None:
    """把修订建议的 target 转成有限数值；自然语言/缺失返回 None。"""
    if isinstance(value, bool) or value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out and abs(out) != float("inf") else None


@mcp.tool(name="rea_music_revise_composition_plan")
def revise_plan(plan_ref: str | dict, evaluation_ref: str | dict) -> dict:
    """依据评测结果修订创作方案：按偏差维度调整下一轮生成参数。

    只写机器可用的结构化目标值（数值/调性标签）；建议里只有自然语言说明时
    记入 skipped 而不猜测解析 —— 早前版本把整句写进 target_modifications.bpm，
    导致后续 Suno 渲染 TypeError、YuE2 报参数缺失。
    """
    def _run():
        plan = load_plan(plan_ref)
        ev = load_evaluation(evaluation_ref)
        revisions = []
        skipped: list[dict] = []
        mods: dict = {}
        base_inherit = plan.get("inherit") or {}
        for sug in ev.get("revision_suggestions", []):
            knob = sug.get("knob")
            target = sug.get("target")
            if knob == "tempo_bpm":
                value = _as_number(target)
                if value is None or value <= 0:
                    skipped.append({"knob": knob,
                                    "reason": "missing_numeric_target",
                                    "adjustment": sug.get("adjustment")})
                    continue
                mods["bpm"] = value
                revisions.append({"knob": "tempo_bpm",
                                  "from": base_inherit.get("tempo_bpm"),
                                  "to": value})
            elif knob == "key":
                label = target.strip() if isinstance(target, str) else ""
                if not label:
                    skipped.append({"knob": knob,
                                    "reason": "missing_key_label",
                                    "adjustment": sug.get("adjustment")})
                    continue
                mods["key"] = label
                revisions.append({"knob": "key", "to": label})
            elif knob == "integrated_lufs_target":
                value = _as_number(target)
                if value is None:
                    skipped.append({"knob": knob,
                                    "reason": "missing_numeric_target",
                                    "adjustment": sug.get("adjustment")})
                    continue
                mods["loudness_target_lufs"] = value
                revisions.append({"knob": "integrated_lufs_target", "to": value})
            elif knob == "section_energy":
                mods["energy_arc"] = "align_to_target_curve"
                revisions.append({"knob": "section_energy",
                                  "to": "align_to_target_curve"})
            elif knob == "sections":
                # 段落对齐无法由参数表达，如实留给人工/Agent 决策
                skipped.append({"knob": knob,
                                "reason": "manual_alignment_required",
                                "adjustment": sug.get("adjustment")})
        if not revisions:
            # 键名与成功分支统一为 skipped_suggestions；skipped 保留为兼容别名
            return {"ok": True, "revised": False,
                    "skipped_suggestions": skipped, "skipped": skipped,
                    "note": "评测未给出可执行的结构化修订；保持原方案",
                    "plan_id": plan.get("plan_id")}
        revised = dict(plan)
        revised["target_modifications"] = {**(plan.get("target_modifications") or {}),
                                           **mods}
        revised["revision_history"] = (plan.get("revision_history") or []) + [{
            "based_on_evaluation": ev.get("evaluation_id"),
            "changes": revisions}]
        revised["plan_id"] = f"{plan.get('plan_id', 'plan')}_r{len(revised['revision_history'])}"
        from rea_music.core import cache as cache_mod
        cache_mod.save_cached(revised["plan_id"], "plan", revised,
                              {"id": revised["plan_id"]})
        # R5 OptimizationHistory：每轮 revise 记入 experiments，附停止判定
        from rea_music.evaluation import loop as loop_mod
        base_plan_id = plan.get("plan_id", "plan").rsplit("_r", 1)[0]
        history = loop_mod.record_iteration(
            base_plan_id,
            goal=(plan.get("objective") or plan.get("user_request")),
            params={"target_modifications": revised.get("target_modifications"),
                    "inherit": {k: revised.get("inherit", {}).get(k)
                                for k in ("tempo_bpm", "key")}},
            eval_summary=ev,
            revision={"changes": revisions,
                      "evaluation_id": ev.get("evaluation_id"),
                      "revised_plan_id": revised["plan_id"]})
        return {"ok": True, "revised": True, "plan": revised,
                "revisions": revisions,
                "skipped_suggestions": skipped,
                "optimization_history": history,
                "note": "修订只调整生成参数，不修改核心算法或覆盖原始结果；"
                        "history.stop=True 时建议停止迭代复盘"}
    return _safe(_run)


def _safe(fn) -> dict:
    """REAError → 结构化错误对象；其他异常原样抛给框架标记 isError。"""
    try:
        return fn()
    except REAError as exc:
        return exc.to_dict()


def serve(transport: str = "stdio", host: str = "127.0.0.1", port: int = 8912):
    """启动 Server。stdio 供宿主拉起；HTTP 只监听回环。"""
    if transport == "stdio":
        mcp.run()
    else:
        mcp.run(transport="streamable-http", host=host, port=port)


def serve_stdio() -> None:
    """pyproject console_scripts 入口。"""
    mcp.run()


if __name__ == "__main__":
    serve_stdio()
