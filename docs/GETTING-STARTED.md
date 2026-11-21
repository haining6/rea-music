# REA-Music 新手入门指南

这份指南写给第一次用的人。不需要懂乐理，不需要会编程，照着做就能跑起来。

---

## 一、它能帮你做什么

把一段音频（歌曲、伴奏、视频里的声音）变成一份**写得明明白白的参数清单**，然后你可以拿这份清单去做新歌。

具体是四件事：

| 你想做的事 | 你会得到 |
|---|---|
| 分析一首歌 | 速度（BPM）、调性、段落结构、和弦进行、响度，全部带"可信度"标注 |
| 对比两首歌 | 哪首更快、更亮、更响，差多少，逐项列出来 |
| 生成新歌 | 按参考曲的风格自动写配置、发给出品引擎、拿到音频文件 |
| 检查生成结果 | 成品和原目标差在哪，下一版该改什么 |

结果保存在本机的 JSON 文件里，可以反复查看、互相比较。

---

## 二、装好它（Windows，5 分钟）

### 1. 确认 Python 版本

打开「终端」（Win 键搜索 Terminal 或 PowerShell），输入：

```bash
python --version
```

看到 `Python 3.12.x` 就对了。没有 Python 的话，去 python.org 下载 3.12 安装包，安装时**务必勾选 Add Python to PATH**。

### 2. 拿到代码

```bash
git clone https://github.com/haining6/rea-music.git
cd rea-music
```

没装 git 的话，也可以在网页上点绿色 `Code` 按钮 → `Download ZIP`，解压后进入文件夹。

### 3. 建一个独立环境（推荐，避免污染系统）

```bash
python -m venv .venv
.venv\Scripts\activate
```

命令行前面出现 `(.venv)` 就成功了。

### 4. 安装

```bash
pip install -e .
```

国内网络慢的话可以换镜像源（把地址替换成你信任的镜像，例如公司内网源）：

```bash
pip install -e . -i <你的镜像源地址>
```

### 5. 验证安装

```bash
rea-music capabilities
```

会输出一段 JSON，看到 `"ok": true` 和 `"version": "0.1.0"` 就是装好了。里面 `input.accepted_formats` 列出了它认识的 30 多种音频格式。

> **音频格式说明**：wav / flac / ogg / mp3 / opus / aiff 这几种核心格式**不需要安装任何额外程序**，`pip install -e .` 装完就能直接分析。
>
> 只有 m4a / aac / mp4 / webm 这类容器和视频页链接才需要 FFmpeg（可选项）。要装的话：Windows 在终端执行 `winget install Gyan.FFmpeg`（或去 ffmpeg.org 下载解压后把 `bin` 目录加进系统 PATH），macOS 用 `brew install ffmpeg`，Linux 用 `sudo apt install ffmpeg`。装完重开终端，`rea-music capabilities` 里 `input.ffmpeg.available` 会变成 `true`。

---

## 三、分析你的第一首歌

```bash
rea-music analyze "D:\音乐\某首歌.mp3" --tier standard
```

等 20 到 40 秒，会打印一份摘要，并给出结果文件的路径（源码目录里在 `data\cache\dna\` 下；用 `pip install` 装到别处时在用户数据目录，见「常见问题」）。

打开那个 JSON，你会看到这些内容：

| 字段 | 通俗解释 |
|---|---|
| `rhythm.bpm.value` | 每分钟多少拍。`92.3` 就是中速抒情 |
| `rhythm.bpm.confidence` | 这个数字有多可信，0 到 1，越高越可信 |
| `tonality.key.value` | 调性。`C major` 是大调，通常偏明亮；`A minor` 是小调，偏忧郁 |
| `form.segments` | 段落：前奏、主歌、副歌……各在第几秒到第几秒 |
| `harmony.chord_segments` | 和弦进行：每个时间段用了哪几个和弦 |
| `production.integrated_lufs` | 整体响度。-14 左右是常见商业水平，-20 算偏安静 |
| `vocals.presence_band` | 有没有人声：`likely` 有人声 / `ambiguous` 不确定 / `absent` 纯器乐 |

### 三种分析档位，按需要选

| 档位 | 用时 | 内容 |
|---|---|---|
| `--tier fast` | 10 秒内 | 速度、调性、响度 |
| `--tier standard` | 30 秒左右 | 加上和弦、段落、人声判断（日常用这个） |
| `--tier deep` | 2 分钟左右 | 加上真实分轨（人声/鼓/贝斯分开）与旋律提取 |

---

## 四、更多用法

**一次分析一整个文件夹：**

```bash
rea-music analyze --batch "D:\音乐" --limit 20
```

每个文件单独出结果，某个文件坏了不影响其他文件。

**直接给它一个链接：**

```bash
rea-music analyze "https://example.com/song.m4a"
```

**给视频页链接**（比如视频网站的播放页），需要额外装一个组件：

```bash
pip install "rea-music[fetch]"
rea-music analyze "https://www.example.com/watch?v=xxxx"
```

它会自动把视频里的声音抽出来分析。下载的文件存在本机 `data\downloads\`，单次不超过 512MB。

**看磁盘占用、清理旧文件：**

```bash
rea-music cache stats
rea-music cache clean --what audio --older-than-days 30
```

清理命令默认只列出会删什么、占多少空间，确认后加 `--yes` 才真的删。

---

## 五、在 WorkBuddy 里用（不用敲命令）

把 REA-Music 注册成工具后（`python scripts/register_workbuddy_mcp.py`），直接说话就行：

- 「分析 D:\音乐\某首歌.mp3」
- 「这两首歌对比一下」
- 「参考这首的风格，做一首 90 秒的新歌」
- 「检查一下生成结果，告诉我哪里要改」

---

## 六、生成新歌（需要额外装出品引擎）

分析功能装完就能用。**生成**需要本机另外装一个出品引擎（YuE2 Studio 或 ACE-Step），REA-Music 只负责指挥它。

简单流程：

1. 启动 YuE2 Studio（双击它的启动器，等窗口出现）
2. 在 WorkBuddy 里说：「分析 D:\音乐\参考曲.mp3，参考它的风格生成一首新歌」
3. 等一两分钟，成品音频出现在 `data\artifacts\` 目录

没装引擎也没关系，它会明确告诉你"这个能力不可用"以及怎么装，不会假装成功。

---

## 七、结果怎么读（三个规则）

1. **看 value，也看 confidence。** 同样的 92 BPM，confidence 0.79 比 0.45 可信得多。低于 0.5 就当"约是多少"来说。
2. **null 就是"测不到"。** 例如纯器乐的人声振音字段是 null，这不是出错，是如实说明。
3. **`octave_ambiguity: true` 表示速度可能有整倍歧义**（比如 92 还是 184），这时按低的那一档理解，或者用 `--tier deep` 再确认。

---

## 八、常见问题

| 现象 | 原因与处理 |
|---|---|
| `'rea-music' 不是内部或外部命令` | 没激活虚拟环境。先 `.venv\Scripts\activate`，再 `pip install -e .` |
| `No module named rea_music` | 同上；或临时设 `set PYTHONPATH=src` |
| 报 `capability_unavailable` | 某个可选引擎没装。报错里会写装什么，照着装即可 |
| 报 `invalid_audio` | 文件不是有效音频，或者链接需要登录/有版权保护 |
| 提示「该容器需要 FFmpeg」 | 你传的是 m4a / aac / mp4 / webm 这类只有 FFmpeg 能解的容器，而本机没装 FFmpeg。两种办法：① 装 FFmpeg 后重试（Windows `winget install Gyan.FFmpeg`，macOS `brew install ffmpeg`，Linux `sudo apt install ffmpeg`，装完重开终端）；② 不装也行——用播放器/转换工具把它转成 wav、flac、ogg、mp3、opus 或 aiff，这 6 种格式零依赖直读。**wav/mp3/flac 报这个错，说明文件损坏或扩展名与实际内容不符**，换个文件试试 |
| 报 `file_not_found` | 路径写错了。路径含空格、中文时用引号包住整个路径 |
| 报 `backend_unavailable` | 网络不通，或远端服务器出错，稍后重试 |
| 报 `host_unresolvable` | 域名解析失败：检查网络，或域名是否拼错 |
| 报 `validation_error`，说地址被拒绝 | 传的是本机或局域网地址（`127.0.0.1`、`192.168.x`、`10.x`）。默认只放行公网链接；要访问自己的 NAS / 本地媒体服务器，在配置里设 `input.allow_private_networks: true`。云元数据地址（`169.254.169.254`）任何情况下都拒绝 |
| 分析很慢 | 第一次运行较慢（要预热），之后会快很多；`deep` 档本身就要两分钟 |
| 想换个目录存结果 | 设环境变量 `REA_MUSIC_HOME` 指向新目录。默认位置：在源码目录里用是 `./data`；用 `pip install` 装到别处时落在用户数据目录（Windows `%LOCALAPPDATA%\REA-Music`，macOS/Linux `~/.local/share/rea-music`） |

---

## 九、名词小抄

- **BPM**：每分钟拍数。60 慢、120 中速、150 以上偏快。
- **调性（Key）**：全曲围绕哪个音。大调明亮，小调忧郁。
- **和弦进行**：几个和弦按顺序循环，是"副歌为什么好听"的主因之一。
- **曲式 / 段落**：前奏、主歌、副歌、桥段、尾奏的排列。
- **LUFS**：整体响度的单位，数值越大越响，流媒体平台通常要求 -14 左右。
- **Music DNA**：本项目对一首歌的完整参数打包，是一个 JSON 文件。
- **MCP**：一种让 AI 助手调用外部工具的标准协议。REA-Music 支持它，所以能在 WorkBuddy 等助手直接用。

---

## 十、隐私与版权

- 音频只在你本机处理，**默认不联网**；只有你主动给出链接时才会下载。
- 下载内容只存在本机 `data\downloads\`，不会上传到任何地方。
- 唯一会外发的情况：你手动启用了远程交叉验证引擎（需要 API key + 明确授权开关）。
- 下载或分析的音频，请自行确认使用权。本项目只做技术特征分析，不传播、不分发内容。

---

遇到文档没覆盖的问题，去 GitHub 的 Issues 提一条：https://github.com/haining6/rea-music/issues
