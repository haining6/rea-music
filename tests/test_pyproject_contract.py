"""打包与发行契约的静态断言（B 线审计 S2/S7/S9/S19/S22 的回归护栏）。

这里只读仓库根的元数据文件，不碰任何业务模块，因此与引擎/分析链路的改动互不干扰。

覆盖的契约：
1. MCP SDK 下限必须满足 ``mcp.server.MCPServer`` 这个符号（SDK 2.0 才更名而来）；
2. ``Typing :: Typed`` 声明必须有 ``py.typed`` 且随包分发；
3. 每个核心依赖都必须在 ``THIRD_PARTY_NOTICES.md`` 里有许可声明；
4. 配置模板必须是 JSON 且键集合与 ``config._DEFAULTS`` 对齐；
5. sdist 清单（MANIFEST.in）存在且包含法律文本。
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"


def _pyproject() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def _dep_specs() -> dict[str, str]:
    """把 ``dependencies`` 列表解析成 {包名: 版本约束串}。"""
    specs: dict[str, str] = {}
    for raw in _pyproject()["project"]["dependencies"]:
        text = raw.split("#", 1)[0].strip()
        if not text:
            continue
        match = re.match(r"(?P<name>[A-Za-z0-9_.-]+)\s*(?P<rest>.*)$", text)
        if match:
            specs[match.group("name").lower()] = match.group("rest").strip()
    return specs


def test_mcp_sdk_symbol_is_importable():
    """server.py 依赖 mcp.server.MCPServer，该符号是 SDK 2.0 起的名字。

    若这里 ImportError，说明``pyproject.toml`` 的 mcp 下限被改低了，
    或环境里装的是 1.x——两种情况都会让两个 console_scripts 全部起不来。
    """
    from mcp.server import MCPServer  # noqa: F401  —— 导入本身就是断言

    assert MCPServer is not None


def test_mcp_dependency_floor_matches_sdk_symbol():
    """mcp 的版本约束必须排除 1.x（无 MCPServer），且应带上界防 3.x 再改名。"""
    spec = _dep_specs().get("mcp")
    assert spec, "pyproject.toml 缺少 mcp 依赖声明"

    assert re.search(r">=\s*2\.", spec), (
        f"mcp 下限必须 >=2.x（MCPServer 从 2.0 起才有），实际: {spec}")
    assert re.search(r"<\s*3(\.|\b)", spec), (
        f"mcp 应设上界 <3，避免 3.x 再次更名导致 import 失败，实际: {spec}")


def test_typed_classifier_requires_py_typed():
    """声明 Typing :: Typed 就必须提供 PEP 561 标记，否则下游 mypy 忽略本包。"""
    classifiers = _pyproject()["project"]["classifiers"]
    if not any("Typing :: Typed" in c for c in classifiers):
        return  # 没声明就不用有标记

    marker = ROOT / "src" / "rea_music" / "py.typed"
    assert marker.is_file(), "声明了 Typing :: Typed 却缺 src/rea_music/py.typed"

    package_data = (_pyproject().get("tool", {})
                    .get("setuptools", {})
                    .get("package-data", {})
                    .get("rea_music", []))
    assert "py.typed" in package_data, (
        f"py.typed 必须在 package-data 里才会进 wheel，实际: {package_data}")


def test_every_core_dependency_is_declared_in_notices():
    """核心依赖不许有许可声明的漏网之鱼（THIRD_PARTY_NOTICES.md 是合规出口）。"""
    notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8").lower()
    missing = [name for name in _dep_specs() if name not in notices]
    assert not missing, f"THIRD_PARTY_NOTICES.md 未声明这些核心依赖: {missing}"


def test_notices_file_is_shipped_as_license_file():
    """THIRD_PARTY_NOTICES.md 必须进 license-files，才会被打进 dist-info/licenses/。"""
    license_files = _pyproject()["project"]["license-files"]
    assert "THIRD_PARTY_NOTICES.md" in license_files, (
        f"license-files 应包含 THIRD_PARTY_NOTICES.md，实际: {license_files}")


def test_config_template_is_json_and_matches_defaults():
    """配置模板必须是 JSON（加载器只 json.loads），且键集合与默认值对齐。"""
    from rea_music.core.config import _DEFAULTS

    assert not (ROOT / "config.example.yaml").exists(), (
        "config.example.yaml 已废弃：加载器只读 JSON，YAML 模板会静默失效")

    template_path = ROOT / "config.example.json"
    assert template_path.is_file(), "缺少 config.example.json"
    template = json.loads(template_path.read_text(encoding="utf-8"))

    assert set(template) == set(_DEFAULTS), (
        f"模板顶层键与 _DEFAULTS 不一致: 模板={sorted(template)} "
        f"默认={sorted(_DEFAULTS)}")

    for section, default_values in _DEFAULTS.items():
        if not isinstance(default_values, dict):
            continue
        missing = set(default_values) - set(template.get(section) or {})
        assert not missing, f"config.example.json 缺少 {section}.{sorted(missing)}"


def test_manifest_ships_legal_and_contract_files():
    """sdist 必须带上法律文本与公开数据合同，否则下载 sdist 的人无从核对。"""
    manifest = ROOT / "MANIFEST.in"
    assert manifest.is_file(), "缺少 MANIFEST.in"

    text = manifest.read_text(encoding="utf-8")
    for name in ("LICENSE", "THIRD_PARTY_NOTICES.md", "SECURITY.md",
                 "CHANGELOG.md", "schemas/*.json", "tests"):
        assert name in text, f"MANIFEST.in 未包含 {name}"
