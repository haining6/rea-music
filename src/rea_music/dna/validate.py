"""Music DNA v1 结构校验。

对 rea_music/schemas/music_dna_v1.schema.json 的受支持子集做程序化校验：
type / required / properties / items / enum / minimum。
DNA 输出 100% 过校验是 R1 验收标准之一。

schema 是**包内数据资源**（随 wheel/sdist 分发，见 pyproject package-data），
通过 ``importlib.resources`` 读取，不依赖仓库根目录布局：editable 安装与
``pip install <wheel>`` 行为一致。仓库根的 ``schemas/`` 只是对外公开契约的
同内容副本（``tests/test_standalone.py`` 断言两份字节一致，防漂移）。
"""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path

from rea_music.core.errors import ValidationError

SCHEMA_PACKAGE = "rea_music"
SCHEMA_RELATIVE = "schemas/music_dna_v1.schema.json"


def schema_path() -> Path:
    """schema 的本机文件路径（调试/日志用；zip 安装下可能不可寻址）。"""
    return Path(str(resources.files(SCHEMA_PACKAGE).joinpath(SCHEMA_RELATIVE)))


def load_schema() -> dict:
    """读取并解析 DNA schema。

    Raises:
        ValidationError: schema 资源缺失（属于打包缺陷，不是数据问题）。
    """
    try:
        text = resources.files(SCHEMA_PACKAGE)\
            .joinpath(SCHEMA_RELATIVE).read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError, OSError) as exc:
        raise ValidationError(
            f"内置 Music DNA schema 缺失：{SCHEMA_RELATIVE}（安装包不完整）",
            hint="重新安装本包（pip install --force-reinstall）；自行打包时请检查 "
                 "pyproject.toml 的 package-data 是否包含 schemas/*.json。",
            details={"resource": f"{SCHEMA_PACKAGE}/{SCHEMA_RELATIVE}"}) from exc
    return json.loads(text)


def validate(instance: dict, schema: dict | None = None) -> list[str]:
    """返回错误列表；空列表 = 通过。不抛异常，便于调用方聚合报告。"""
    schema = schema or load_schema()
    errors: list[str] = []
    _walk(instance, schema, "$", errors)
    return errors


def validate_or_raise(instance: dict) -> None:
    errors = validate(instance)
    if errors:
        raise ValidationError(
            f"Music DNA 结构校验失败: {errors[:5]}",
            hint="分析器输出与 schema 不符，属于代码缺陷，应修复引擎而非数据。")


def _walk(inst, sch, path: str, errors: list[str]) -> None:
    t = sch.get("type")
    types = t if isinstance(t, list) else ([t] if t else [])
    if types and not any(_check_type(inst, single) for single in types):
        errors.append(f"{path}: 期望 {t}, 实际 {_pytype(inst)}")
        return
    if "enum" in sch and inst not in sch["enum"]:
        errors.append(f"{path}: {inst!r} 不在 enum {sch['enum']}")
    if "minimum" in sch and isinstance(inst, (int, float)) and inst < sch["minimum"]:
        errors.append(f"{path}: {inst} < minimum {sch['minimum']}")
    if "object" in types:
        for req in sch.get("required", []):
            if req not in inst:
                errors.append(f"{path}: 缺少必填字段 {req}")
        for key, sub in (sch.get("properties") or {}).items():
            if key in inst:
                _walk(inst[key], sub, f"{path}.{key}", errors)
    if "array" in types and "items" in sch and isinstance(inst, list):
        for i, item in enumerate(inst):
            _walk(item, sch["items"], f"{path}[{i}]", errors)


def _check_type(inst, t) -> bool:
    mapping = {"object": dict, "array": list, "string": str,
               "number": (int, float), "integer": int, "boolean": bool,
               "null": type(None)}
    py = mapping.get(t)
    if py is None:
        return True
    if t == "integer" and isinstance(inst, bool):
        return False
    if t == "number" and isinstance(inst, bool):
        return False
    return isinstance(inst, py)


def _pytype(inst) -> str:
    return type(inst).__name__
