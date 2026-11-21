"""OptimizationHistory：评测迭代历史跨会话持久化。

每次 revise（一轮迭代）追加写 data/experiments/<plan_id>.json：
goal / 生成参数 / 评测摘要 / 修订内容 / 满足维数 / 停止判定。
停止策略：默认上限 3 轮（config generation.optimization.max_iterations）；
连续 2 轮 satisfied 维数无改善即停（satisfied_repeat_stop）。
Agent 可 resume(plan_id) 跨会话续跑，不覆盖原始记录。
"""

from __future__ import annotations

import json
import time

from rea_music.core import paths
from rea_music.core.config import load_config

def _path(plan_id: str):
    # 动态解析（不 import 时绑定），保证测试 monkeypatch 与
    # REA_MUSIC_HOME 运行时切换都能生效
    return paths.experiments_dir() / f"{plan_id}.json"


def record_iteration(plan_id: str, *, goal: str | None,
                     params: dict | None, eval_summary: dict | None,
                     revision: dict | None) -> dict:
    """追加一轮迭代记录，返回最新 stop 建议与历史规模。"""
    path = _path(plan_id)
    history = load(plan_id)
    entry = {
        "iteration": len(history["iterations"]) + 1,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "goal": goal,
        "params": params or {},
        "eval_summary": eval_summary or {},
        "revision": revision or {},
        "satisfied_count": _count_satisfied(eval_summary),
    }
    history["iterations"].append(entry)
    stop = stop_verdict(plan_id, history)
    history["stop"] = stop
    path.write_text(json.dumps(history, ensure_ascii=False, indent=1),
                    encoding="utf-8")
    return {"iterations": len(history["iterations"]), "stop": stop}


def load(plan_id: str) -> dict:
    path = _path(plan_id)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass  # 历史损坏时重开新档，不阻断迭代
    return {"plan_id": plan_id, "iterations": []}


def resume(plan_id: str) -> dict:
    """跨会话续跑入口：返回历史与下一轮序号。"""
    history = load(plan_id)
    return {"plan_id": plan_id,
            "completed_iterations": len(history["iterations"]),
            "next_iteration": len(history["iterations"]) + 1,
            "stop": history.get("stop")}


def stop_verdict(plan_id: str, history: dict | None = None) -> dict:
    """停止判定：上限轮数 / 连续无改善 / 继续。"""
    cfg = load_config()["generation"]["optimization"]
    history = history or load(plan_id)
    iters = history.get("iterations", [])
    if not iters:
        return {"stop": False, "reason": "no_history"}
    if len(iters) >= int(cfg.get("max_iterations", 3)):
        return {"stop": True,
                "reason": f"max_iterations_reached ({cfg.get('max_iterations', 3)})"}
    repeat = int(cfg.get("satisfied_repeat_stop", 2))
    recent = iters[-repeat:]
    if len(recent) >= repeat:
        counts = [e.get("satisfied_count", 0) for e in recent]
        if len(set(counts)) == 1 and counts[0] >= 0 and len(recent) >= 2 \
                and iters[-1].get("satisfied_count", 0) <= iters[-2].get("satisfied_count", 0):
            return {"stop": True,
                    "reason": f"no_improvement_for_{repeat}_rounds",
                    "satisfied_counts": counts}
    return {"stop": False, "reason": "below_limits"}


def _count_satisfied(eval_summary: dict | None) -> int:
    if not eval_summary:
        return 0
    dims = eval_summary.get("dimensions") or {}
    return sum(1 for v in dims.values()
               if isinstance(v, dict) and v.get("satisfied") is True)
