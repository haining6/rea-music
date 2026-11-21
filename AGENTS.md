# AGENTS.md — 给接入本仓库的 AI Agent

## 你在操作什么

REA-Music 是能力层，不是聊天产品。你的产出是：真实分析结果、可靠的创作方案、诚实的评测结论。

## 不可违反的纪律

1. **置信度如实转述**。confidence ≤ 0.55 的字段必须用"估计/推测"表述，禁止包装成确定结论。
2. **missing_reason 优先于编造**。字段为 null 时，告诉用户缺什么引擎，不要填假数据。
3. **不自动安装大依赖**。demucs/ACE-Step 等安装动作先记录并等用户批准后再装
   （内部过程文档放在 `docs/internal/`，不随公开库发布）。
4. **原创性红线**。计划与生成请求中不得出现参考曲歌词、参考曲旋律音符序列、采样指令。
5. **不无限重试**。Job 失败重试上限 2 次；生成迭代上限 3 轮，连续无改善即停下复盘。
6. **音频不出本机**。远程引擎默认 disabled；上传需用户显式授权（双开关见 SECURITY.md §3）。
7. **不写个人绝对路径**。本仓库公开发布：提交前确认 diff 里没有
   `C:/Users/<用户名>`、`D:/<个人目录>`、`/home/<用户名>` 这类路径。

## 开发约定（改代码的 Agent）

- Python 3.12 venv（`.venv/`）。未安装时在仓库根目录用 `PYTHONPATH=src`，测试 `pytest tests/`。
- 换新引擎：写 Adapter（`adapters/registry.py` 注册）+ 能力探测，不改 DNA schema。
- 改 schema：只增不改，版本号 +1，带迁移说明，旧数据必须能读。
  仓库根 `schemas/` 与包内 `src/rea_music/schemas/` 两份副本必须逐字节一致（有测试守护）。
- 加依赖：同步更新 `THIRD_PARTY_NOTICES.md` 的许可表，否则
  `tests/test_pyproject_contract.py` 会失败。
- 每完成一个任务：跑相关测试 + 证据留在 `benchmarks/` 或 `examples/`。
- 提交信息描述"为什么"，不描述"改了什么"（diff 自己会说话）。

## 常用命令

```bash
pip install -e ".[dev]"                              # 装包 + 测试依赖
pytest tests/ -x -q                                  # 全量（含引擎用例）
pytest tests/ -q --ignore=tests/test_engines_r2.py   # CI 跑的子集（无需引擎）
rea-music capabilities                               # 能力自检，应看到 "ok": true
python fixtures/make_fixtures.py                     # 生成合成夹具（*.wav 不入库）
python -m build && python -m twine check dist/*      # 发布前自检产物
```

> 镜像、代理等机器相关设置属环境专属信息，不写进本文件（公开仓库）。
> 本地需要时放在 `docs/internal/ENVIRONMENT.md`。
