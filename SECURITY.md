# 安全模型（SECURITY.md）

REA-Music 是**单机、本地、用户自持**的音乐分析与创作能力层。本文件说明它信任什么、
不信任什么、以及哪些风险由使用方承担。范围只覆盖本仓库代码（CLI / MCP Server /
Python API / Agent Skills），不覆盖第三方引擎（demucs、YuE2 Studio、ACE-Step、
SongBrain 等）与宿主 Agent 自身。

## 威胁模型

**信任假设**

- 运行 REA-Music 的这台机器与它的登录用户是可信的。本工具不提供多用户隔离、
  权限分级、审计日志，也不做"不可信代码执行"防护。
- `data/`（`REA_MUSIC_HOME`）目录内容被视为用户自己的数据；下游分析结果只对
  本机用户负责。
- Python 环境与依赖由使用者自行安装：`pip install -e .` 会执行 `pyproject.toml`
  声明的构建后端。这与任何 Python 包一致，属于使用者的供应链决策。

**不在防护范围内**

- 恶意构造的音频文件导致解码库（libsndfile / ffmpeg / librosa）崩溃或内存耗尽。
  上限控制只覆盖时长（`analysis.max_duration_sec`，默认 900s）与下载体积
  （512MB），不是恶意样本沙箱。
- 本机已存在恶意软件、键盘记录器、被篡改的 Python 解释器。
- 用户把 `--host 0.0.0.0` 显式传给 `rea-music serve` 造成的网络暴露。

## 攻击面与边界

### 1. MCP Server 仅回环

- `rea-music serve` 默认 `--transport stdio`：由宿主进程拉起，走管道，不监听端口。
- HTTP 模式默认 `127.0.0.1:8912`，只监听回环。**没有任何鉴权机制**——这是刻意的：
  它假设"能连到 127.0.0.1 的进程就是本机用户"。若你把 `--host` 改成非回环地址，
  等于把无鉴权的工具执行接口暴露给网络，风险自负；本项目不接受这类配置的漏洞报告。
- `rea-music serve --host <非回环>` 会被 CLI 直接拒绝（`UsageError`），必须额外显式
  传 `--allow-external` 才放行。这条硬性原则由代码保证，不只写在文档里。
- stdio/HTTP 两条通道共用同一 core，因此 HTTP 暴露的能力等于本地 CLI 的全部能力
  （读写 `data/`、调用 ffmpeg、启动可选引擎子进程）。

### 2. 输入链接（SSRF 边界与限制）

`inspect` / `analyze` / `evaluate` 等入口接受 http(s) 链接（D22）。相关约定：

- **只接受 http/https**。`file://`、`ftp://`、`data:` 等 scheme 一律
  `validation_error`，不会退化成读取本机任意文件。
- **目标地址校验（deny-by-default）**：`core/net_guard.py` 的
  `assert_public_url()` 已在 `resolve_source()` 入口接入。云元数据
  （`169.254.169.254` 等链路本地/保留段）**永远拒绝**，与开关无关；私网与回环
  （`10/8`、`172.16/12`、`192.168/16`、`127/8`）默认拒绝，指向本机或局域网媒体服务
  （NAS、DLNA、本地 HTTP 服务器）时需显式设置 `input.allow_private_networks: true`。
  被拒时返回 `validation_error`，`details.blocked` 标注命中的网段类别。
  DNS 解析失败不在此拦截，交由下载层报 `backend_unavailable`。

  > **已知局限（0.1.0）**：① 重定向后的最终地址未逐跳复查（`urllib` 默认跟随 3xx）；
  > ② 未用解析结果直连 IP，理论上存在 DNS rebinding 窗口。若要把 REA-Music 包装成
  > 面向不可信输入的多租户服务，仍需在调用侧加目标白名单与出站代理。
- **资源上限**：单次下载 ≤512MB（`Content-Length` 与流式累计双检），建连超时 120s，
  流式读取 300s，1MB 分块；超限即中断并返回结构化错误。这防止被超大/慢速资源拖死，
  但**不防**大量小请求造成的带宽消耗（无速率限制）。
- **落盘位置**：远端内容只写 `data/downloads/<url-sha256前缀>/`，写入前先落
  `<name>.part` 再原子改名；失败即清理半截文件。下载内容不二次外发。
- **视频页** 走可选 `yt-dlp` 子进程（`pip install 'rea-music[fetch]'`），其自身的
  解析与网络行为由 yt-dlp 负责，不在本文件的保证范围内。

### 3. 远程上传需显式授权

本工具默认**不外发任何音频**。唯一的远程音频上传路径是 SongBrain 交叉验证：

- 代码层双条件（`adapters/songbrain.py::probe`）：存在 `remote.songbrain_api_key`
  （或环境变量 `SONGBRAIN_API_KEY`）**且**
  `remote.songbrain_upload_authorized=true`。缺任一条即返回
  `capability_unavailable`（reason 为 `songbrain_api_key_missing` /
  `songbrain_upload_not_authorized`），不会静默上传。默认配置两者皆无。
- 即使开启，`deep` 档只在时长 30s–600s 区间内才尝试上传；上传失败降级为"本地结果
  不受影响"，并如实记录错误码。
- YuE2 Studio / ACE-Step 仅通过本机回环端口（`127.0.0.1:8791` / `127.0.0.1:8001`）
  交互，不上传音频到第三方云。
- 配置文件解析失败时**不再静默回落**：会 `warnings.warn` 一次，并在 `capabilities`
  的 `config_error` 字段里暴露，避免"以为开了授权、其实配置没生效"的错觉。
  注意配置文件必须是 **JSON**（`REA_MUSIC_HOME/config.json`），模板见 `config.example.json`。

### 4. 无遥测

REA-Music 不含任何埋点、崩溃上报、使用统计或版本心跳。除以下两种情况外不发起网络请求：

1. 用户/Agent 显式提供了 http(s) 链接（下载输入）；
2. 用户显式启用了远程交叉验证（见上）且通过授权双开关。

`pip install`、可选引擎自身的联网行为发生在安装/运行时，由对应项目负责。

### 5. 本地文件与清理

- 写入范围限定在 `REA_MUSIC_HOME`（默认 `<repo>/data`，已被 `.gitignore` 排除）。
- `rea-music cache clean` 只接受内置白名单 key（`audio`/`dna`/`plan`/`evaluation`/
  `transcription`/`stems`/`downloads`/`jobs`/`experiments`/`artifacts`/`cache`/`all`），
  **不接受任意路径参数**；解析后的目录必须位于工作区内，且路径分段不得命中
  `.git`/`.venv`/`src`/`tests`/`schemas` 等代码目录。递归删除不跟随符号链接。
  默认 dry-run，`--yes` 才真正删除。
- 未实现加密存储：DNA、下载音频、Job 记录都是明文。共享机器上请自行管理磁盘权限。

## 已知限制（不视为漏洞）

- 无鉴权、无速率限制、无多租户隔离（见上）。
- URL 目标地址**已有** deny-by-default 护栏（`core/net_guard.py`，见上文「输入面」）；
  未做的是重定向逐跳复查与 IP 直连防 rebinding，两者在 0.1.0 明确为已知局限。
- 不校验输入音频的来源合法性与版权状态；`usage_rights_note` 字段只做提示。
- 启发式字段（下拍、段落功能标签）置信度封顶 0.55，可能给出不准确的音乐学结论；
  这是准确性议题，不是安全问题。
- 引擎缺失时如实降级（`capability_unavailable`），不会静默伪造结果。

## 报告漏洞

发现安全问题时请**不要**开公开 issue。请通过以下渠道私下联系维护者：

- 首选：GitHub 私密漏洞报告（Security Advisories）——
  https://github.com/haining6/rea-music/security/advisories/new
- 兜底：在仓库开 issue 仅写"需要私下沟通安全问题"，维护者会与你建立私下渠道，
  具体细节不要公开贴出

请在报告中包含：影响版本（`rea-music --version`）、复现步骤、最小复现输入、
实际影响（例如"通过链接输入读取本机文件"）。我们会在 7 个工作日内确认收到，
修复版本发布后在此文件记录致谢（如你愿意署名）。

**请勿**在报告中附带真实版权音频；用合成样本或最小化描述替代。
