"""缓存维护 CLI：`rea-music cache stats` 与 `rea-music cache clean`。

运行时数据（解码后的 wav、DNA、分轨、下载、Job 记录、实验记录、工件）都落在
``REA_MUSIC_HOME``（默认 ``<repo>/data``），长期使用会累积到数 GB。本模块提供
只读统计与按目录清理两条运维命令。

安全边界（硬约束，见 SECURITY.md）：
- 命令**只接受内置白名单 key**（audio/dna/plan/evaluation/transcription/stems/
  downloads/jobs/experiments/artifacts/cache/all），不接受任何自由路径参数，
  因此不存在"误传 ``data`` 以外路径"的入口。
- 解析后的目录必须位于 ``workspace_root()`` 之内，且路径分段不得命中代码/配置
  目录名（``.git``/``.venv``/``src``/``tests``/``schemas`` 等）。即使白名单被
  程序化篡改，第二道校验仍会拒绝。
- 递归遍历不跟随符号链接，符号链接文件本身也不删除（避免越界删除）。
- 默认 dry-run：只列清单与可回收空间；``--yes`` 才真正删除，并逐条打印删除项。
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path

import click

from rea_music.core.errors import ValidationError
from rea_music.core.paths import (artifacts_dir, cache_dir, downloads_dir,
                                  experiments_dir, jobs_dir, stems_dir,
                                  workspace_root)

# ---------- 白名单：key -> 目录解析器（延迟调用，兼容 REA_MUSIC_HOME 运行时切换）----------

WHITELIST: dict[str, Callable[[], Path]] = {
    "audio": lambda: cache_dir() / "audio",
    "dna": lambda: cache_dir() / "dna",
    "plan": lambda: cache_dir() / "plan",
    "evaluation": lambda: cache_dir() / "evaluation",
    "transcription": lambda: cache_dir() / "bp_transcription",
    "stems": stems_dir,
    "downloads": downloads_dir,
    "jobs": jobs_dir,
    "experiments": experiments_dir,
    "artifacts": artifacts_dir,
    "cache": cache_dir,
}

# stats 逐项展开的叶子目录；"cache" 用合计（subtotals）表示，避免重复计数
STATS_TARGETS: tuple[str, ...] = ("audio", "dna", "plan", "evaluation",
                                  "transcription", "stems", "downloads",
                                  "jobs", "experiments", "artifacts")
_CACHE_LEAVES: tuple[str, ...] = ("audio", "dna", "plan", "evaluation",
                                  "transcription")
ALL_TARGETS: tuple[str, ...] = STATS_TARGETS

# 绝不允许出现在待清理路径里的分段名（代码/配置/依赖目录）
_FORBIDDEN_PARTS = frozenset({
    ".git", ".github", ".venv", "venv", "src", "tests", "schemas", "engines",
    "scripts", "docs", "fixtures", "skills", "benchmarks", "examples",
    "dist", "build", "node_modules", "__pycache__",
})


def human_bytes(size: int) -> str:
    """人类可读体积（1024 进制）。

    Args:
        size: 字节数（负数按 0 处理）。

    Returns:
        形如 ``512 B`` / ``1.5 KB`` / ``2.3 GB`` 的字符串。
    """
    value = float(max(int(size), 0))
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024.0:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} TB"


def resolve_target(name: str) -> Path:
    """把白名单 key 解析为绝对目录，并做越界/敏感路径校验。

    Args:
        name: 白名单 key。

    Returns:
        校验通过的绝对目录。

    Raises:
        ValidationError: key 不在白名单，或目标目录越出工作区、命中敏感目录名。
    """
    resolver = WHITELIST.get(name)
    if resolver is None:
        raise ValidationError(
            f"不支持的目标 {name!r}",
            hint="只允许：" + "、".join(sorted([*WHITELIST, "all"])),
            details={"allowed": sorted([*WHITELIST, "all"])})
    root = workspace_root().resolve()
    target = resolver().resolve()
    if target == root:
        raise ValidationError(
            f"拒绝操作工作区根目录: {target}",
            hint="清理命令只作用于 data/ 下的子目录。")
    try:
        relative = target.relative_to(root)
    except ValueError:
        raise ValidationError(
            f"{target} 不在工作区 {root} 内，拒绝操作",
            hint="设置 REA_MUSIC_HOME 后重试；清理命令不会越过工作区边界。",
            details={"target": str(target), "root": str(root)}) from None
    for part in relative.parts:
        if part in _FORBIDDEN_PARTS:
            raise ValidationError(
                f"{target} 命中受保护目录名 {part!r}，拒绝操作",
                hint="清理只针对运行时数据目录，源码/配置/依赖目录一律不碰。",
                details={"target": str(target), "blocked_part": part})
    return target


def _expand(what: str) -> tuple[str, ...]:
    """把 ``all`` 展开成叶子目标列表，其余原样返回单元素元组。"""
    if what == "all":
        return ALL_TARGETS
    return (what,)


# ---------- 遍历 ----------

def _scan_tree(root: Path) -> dict:
    """统计目录内的文件数/子目录数/总字节（不跟随符号链接）。"""
    files = 0
    dirs = 0
    total = 0
    for dirpath, dirnames, filenames in os.walk(root, topdown=True,
                                                followlinks=False):
        dirs += len(dirnames)
        for name in filenames:
            path = Path(dirpath) / name
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                total += path.stat().st_size
            except OSError:
                continue
            files += 1
    return {"files": files, "subdirs": dirs, "bytes": total}


def _collect_files(root: Path, cutoff: float | None) -> list[tuple[Path, int]]:
    """收集待删除文件（按 mtime 过滤，跳过符号链接与不可读项）。

    Args:
        root: 目标目录。
        cutoff: 时间戳下限；仅当 mtime 更早时才收集，None 表示不按时间过滤。

    Returns:
        ``[(path, size_bytes)]``，按路径深度倒序（深的先删，便于随后清空目录）。
    """
    found: list[tuple[Path, int]] = []
    for dirpath, _dirnames, filenames in os.walk(root, topdown=True,
                                                 followlinks=False):
        for name in filenames:
            path = Path(dirpath) / name
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                info = path.stat()
            except OSError:
                continue
            if cutoff is not None and info.st_mtime >= cutoff:
                continue
            found.append((path, info.st_size))
    found.sort(key=lambda item: len(item[0].parts), reverse=True)
    return found


def _prune_empty_dirs(root: Path) -> None:
    """自底向上删除空白子目录（保留 root 本身）。"""
    for dirpath, _dirnames, _filenames in os.walk(root, topdown=False,
                                                  followlinks=False):
        current = Path(dirpath)
        if current.resolve() == root:
            continue
        try:
            current.rmdir()          # 非空目录会抛 OSError，属预期
        except OSError:
            continue


# ---------- 业务实现 ----------

def collect_stats() -> dict:
    """汇总各白名单目录的用量。

    目录由 ``core.paths`` 按需创建，因此不报告 exists 字段。

    Returns:
        ``{ok, root, dirs[], subtotals[], total}``；每个条目含
        name/path/files/subdirs/bytes/human。
    """
    entries: list[dict] = []
    total_files = 0
    total_bytes = 0
    for name in STATS_TARGETS:
        target = resolve_target(name)
        scanned = _scan_tree(target) if target.is_dir() else {
            "files": 0, "subdirs": 0, "bytes": 0}
        entries.append({
            "name": name,
            "path": str(target),
            "files": scanned["files"],
            "subdirs": scanned["subdirs"],
            "bytes": scanned["bytes"],
            "human": human_bytes(scanned["bytes"]),
        })
        total_files += scanned["files"]
        total_bytes += scanned["bytes"]

    cache_bytes = sum(e["bytes"] for e in entries if e["name"] in _CACHE_LEAVES)
    cache_files = sum(e["files"] for e in entries if e["name"] in _CACHE_LEAVES)
    subtotals = [{
        "name": "cache",
        "path": str(resolve_target("cache")),
        "files": cache_files,
        "bytes": cache_bytes,
        "human": human_bytes(cache_bytes),
    }]
    return {
        "ok": True,
        "root": str(workspace_root()),
        "dirs": entries,
        "subtotals": subtotals,
        "total": {"files": total_files, "bytes": total_bytes,
                  "human": human_bytes(total_bytes)},
        "note": "bytes/human 为实际占盘字节；jobs 目录按 Job JSON 计数。",
    }


def clean(what: str = "all", *, older_than_days: int = 0,
          apply: bool = False) -> dict:
    """按白名单清理运行时文件。

    Args:
        what: 白名单 key 或 ``all``。
        older_than_days: 只删除 mtime 早于 N 天的文件；0 表示不限时间。
        apply: True 才真正删除；False（默认）只预览。

    Returns:
        ``{ok, dry_run, what, older_than_days, candidates, reclaim_bytes,
        reclaim_human, deleted[], failed[], by_dir[]}``。

    Raises:
        ValidationError: 目标不在白名单或越出工作区（见 resolve_target）。
    """
    if older_than_days < 0:
        raise ValidationError("--older-than-days 不能为负数",
                             hint="传 0 表示不按时间过滤，例如 30 表示 30 天前。")
    targets = _expand(what)
    cutoff = (time.time() - older_than_days * 86400
              if older_than_days > 0 else None)

    planned: list[tuple[Path, int]] = []
    by_dir: list[dict] = []
    for name in targets:
        target = resolve_target(name)
        files = _collect_files(target, cutoff) if target.is_dir() else []
        size = sum(item[1] for item in files)
        by_dir.append({"name": name, "path": str(target),
                       "files": len(files), "bytes": size,
                       "human": human_bytes(size)})
        planned.extend(files)

    reclaim = sum(size for _path, size in planned)
    deleted: list[str] = []
    failed: list[dict] = []
    if apply:
        for path, _size in planned:
            try:
                path.unlink()
            except FileNotFoundError:
                pass                      # 并发清理：视为已达成目标
            except OSError as exc:
                failed.append({"path": str(path), "error": str(exc)})
                continue
            deleted.append(str(path))
            click.echo(f"deleted  {path}")
        for name in targets:
            target = resolve_target(name)
            if target.is_dir():
                _prune_empty_dirs(target)
    else:
        for path, _size in planned:
            click.echo(f"would delete  {path}")

    return {
        "ok": not failed,
        "dry_run": not apply,
        "what": what,
        "older_than_days": older_than_days,
        "candidates": len(planned),
        "reclaim_bytes": reclaim,
        "reclaim_human": human_bytes(reclaim),
        "deleted": deleted,
        "failed": failed,
        "by_dir": by_dir,
        "root": str(workspace_root()),
        "note": ("dry-run：只预览清单与可回收空间，确认后加 --yes 才真删"
                 if not apply else "已按白名单删除匹配项"),
    }


# ---------- Click 命令 ----------

@click.group("cache")
def cache() -> None:
    """缓存维护：统计用量（stats）与按白名单清理（clean）。"""


@cache.command("stats")
def stats_command() -> None:
    """统计 data/ 各缓存目录的文件数、占用与合计。"""
    from rea_music.cli.main import _echo_json, _err
    try:
        _echo_json(collect_stats())
    except Exception as exc:  # noqa: BLE001
        _err(exc)


@cache.command("clean")
@click.option("--what",
              type=click.Choice(["all", *sorted(WHITELIST)], case_sensitive=False),
              default="all", show_default=True,
              help="清理目标；cache 表示整个分析缓存目录")
@click.option("--older-than-days", type=click.IntRange(min=0), default=0,
              show_default=True,
              help="只清理 mtime 早于 N 天的文件；0 表示不限")
@click.option("--yes", "apply", is_flag=True,
              help="真正执行删除（默认 dry-run，只列清单）")
@click.option("--dry-run", "explicit_dry", is_flag=True,
              help="显式只预览（与默认行为一致；与 --yes 互斥，供 CI/脚本显式书写）")
def clean_command(what: str, older_than_days: int, apply: bool,
                  explicit_dry: bool) -> None:
    """按白名单清理运行时数据；默认 dry-run，--yes 才删除。"""
    from rea_music.cli.main import _echo_json, _err
    if explicit_dry and apply:
        raise click.UsageError("--dry-run 与 --yes 互斥，二者只能居其一")
    try:
        _echo_json(clean(what, older_than_days=older_than_days,
                         apply=apply and not explicit_dry))
    except Exception as exc:  # noqa: BLE001
        _err(exc)
