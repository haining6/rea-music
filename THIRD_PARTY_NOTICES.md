# THIRD_PARTY_NOTICES — 第三方组件许可声明

REA-Music 本体以 **MIT** 许可证发布（见 `LICENSE`）。本文件声明发行物引用的全部第三方组件
及其许可类型，供下游合规审查使用。

**分发边界（重要）**：REA-Music 的 wheel/sdist **只包含本项目自己的代码**（`src/rea_music/`）
与自有数据文件（`schemas/*.json`、`py.typed`）。所有第三方 Python 包均由使用者通过
`pip install` 单独获取，**不随本发行物打包、不静态链接、不修改其源码**。

尽调过程与取舍理由见 `OPEN_SOURCE_REUSE.md`；本文只承担许可声明职责。

---

## 一、直接依赖（`pyproject.toml` 的 `dependencies`）

| 组件 | 版本下限 | 许可证 | 用途 | 随 wheel 分发 |
|---|---|---|---|---|
| librosa | >=0.11 | ISC | 音频分析基座（STFT/CQT/节拍/音高） | 否 |
| soundfile | >=0.12 | BSD-3-Clause | libsndfile 绑定，核心格式直读 | 否 |
| scipy | >=1.11 | BSD-3-Clause | 信号处理（滤波/重采样/峰值） | 否 |
| scikit-learn | >=1.4 | BSD-3-Clause | Ward 层次聚类（曲式结构） | 否 |
| pyloudnorm | >=0.1.0 | MIT | ITU-R BS.1770 响度积分 | 否 |
| click | >=8.1 | BSD-3-Clause | CLI 框架 | 否 |
| mcp | >=2.3,<3 | MIT | MCP 官方 Python SDK | 否 |

> 注：`mcp` 设了上界 `<3`，因为本项目的 MCP Server 直接依赖 `mcp.server.MCPServer`
> 这一 2.x 符号；跨大版本更名会让包在 import 阶段失效，属刻意的耦合上界。

## 二、可选依赖（`optional-dependencies` extras，默认不安装）

| 组件 | extra | 版本下限 | 许可证 | 用途 |
|---|---|---|---|---|
| torch | `stems` | >=2.0 | BSD-3-Clause | demucs 分轨运行时 |
| demucs | `stems` | >=4.0 | MIT | 音源分离 |
| chordotomy | `chords` | >=0.4 | MIT | 和弦识别（DSP 档） |
| songbrain | `remote` | >=0.4 | MIT（SDK） | 远程交叉验证（API 另受其服务条款约束） |
| yt-dlp | `fetch` | >=2025.1 | Unlicense | 视频页抽音频 |
| pytest | `dev` | >=8.0 | MIT | 测试框架（仅开发） |

## 三、关键传递依赖（由上述包引入，本项目不直接声明）

| 组件 | 由谁引入 | 许可证 | 说明 |
|---|---|---|---|
| numpy | librosa / scipy / scikit-learn | BSD-3-Clause AND 0BSD AND MIT AND Zlib | 无 |
| numba | librosa | BSD-2-Clause | JIT 编译，librosa 首次调用有编译开销 |
| llvmlite | numba | BSD-2-Clause AND Apache-2.0 WITH LLVM-exception | 内嵌 LLVM 的许可例外条款 |
| **soxr** | librosa | **LGPL-2.1-or-later** | **见下方专条** |
| joblib | scikit-learn | BSD-3-Clause | 无 |
| threadpoolctl | scikit-learn | BSD-3-Clause | 无 |
| pooch | librosa | BSD-3-Clause | 示例数据下载器（本项目的分析链路不用其联网功能） |
| lazy_loader | librosa | BSD-3-Clause | 无 |
| msgpack | librosa | Apache-2.0 | 无 |
| cffi / pycparser | soundfile | MIT | libsndfile 的调用桥 |
| typing-extensions | pydantic / 多方 | PSF-2.0 | 无 |
| pydantic / pydantic-core | mcp（传递） | MIT | **本项目不直接 import pydantic**，仅作为 mcp SDK 的依赖存在 |

> 具体版本因解析时间而异，`pip freeze` 为准。上表只锁定"许可类型"这一合规事实。

## 四、关于 soxr（LGPL-2.1-or-later）专条

`soxr` 是 librosa 的重采样后端，`pip install rea-music` 会**必然**把它装进同一个环境。
LGPL-2.1 是弱著佐权许可，判定结论：

1. REA-Music **不修改** soxr 源码，**不静态链接**其代码，**不把其二进制打进 wheel**；
2. 运行时通过 Python 的 `import soxr` 动态引用，属 LGPL-2.1 §6 允许的"使用库"情形；
3. 因此 REA-Music 保持 MIT 授权，不会传染为 LGPL。

若你要把 REA-Music 与本项目**一起**重新分发（例如打成单文件 exe、Docker 镜像、系统安装包），
则 soxr 的二进制随之分发，需自行履行 LGPL-2.1 §6/§8 义务：
随附许可全文、提供替换 soxr 的方法、保留版权声明。
替代方案：设置环境变量绕过 librosa 的 soxr 后端，或改用系统级 resample（音质与行为会变）。

## 五、非 Python 的外部依赖（不随发行物分发，用户自装）

| 组件 | 许可证 | 用途 | 缺失时行为 |
|---|---|---|---|
| FFmpeg / ffprobe | LGPL-2.1+ / GPL-2+（取决于编译选项） | 非核心容器解码（m4a/aac/mp4/webm 等） | `capability_unavailable` + 安装指引 |
| YuE2 Studio | 待核（独立应用，详见 `OPEN_SOURCE_REUSE.md`） | 本地歌曲生成后端（回环 MCP） | 降级为 Suno 提示词包 |
| ACE-Step 1.5 | 待核（独立仓库，详见 `OPEN_SOURCE_REUSE.md`） | 本地歌曲生成后端（回环 REST） | `capability_unavailable` |
| basic-pitch（隔离 venv） | Apache-2.0 | 旋律转写 | `capability_unavailable` |

FFmpeg 的许可取决于其编译选项（`--enable-gpl` 编译出的二进制为 GPL）。REA-Music 只通过
子进程调用用户自装的 FFmpeg，**不链接、不分发**，故不受其许可约束；但若你随产品分发
FFmpeg 二进制，需自行遵守其 LGPL/GPL 条款。

## 六、算法思想借鉴声明

本项目参考了若干开源项目的**已发表算法路线**，但没有复制其代码：

- 曲式结构：Ward 层次聚类 + 轮廓系数，属通用方法；
- 调性判定：Krumhansl-Schmuckler 模板 + Temperley 融合，属公开算法；
- 节拍仲裁：包络自相关 + 倍频判决，独立实现。

`SongScope` 仓库无可识别 LICENSE（详见 `OPEN_SOURCE_REUSE.md`），本项目**只借鉴思想、
未复制任何代码**，故不构成衍生作品。

## 维护规则

新增任何依赖时，必须同步更新本文件的对应表格；CI 的 `tests/test_pyproject_contract.py`
会断言 `pyproject.toml` 里每个核心依赖都在本文件中有许可声明记录，防止漏登。
