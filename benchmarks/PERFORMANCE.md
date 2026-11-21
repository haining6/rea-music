# benchmarks — 性能与资源占用记录

> 计量口径：全部为 CPU 路径（未启用 GPU 加速）。测试机为 8 核以上消费级 CPU +
> 32GB 内存 + 8GB 显存显卡，具体型号不记录（与结论无关）。
>
> 测试用例数会随迭代变化，表格里的"N 项"是**当时**的规模；当前规模见 CHANGELOG。

## 2026-11-20 R1 基准

| 场景 | 耗时 | 说明 |
|---|---|---|
| 80s 合成曲 standard 档全管线（首次，冷缓存） | ~20–30s | CPU；librosa numba 首次编译另加 ~10s |
| 80s 合成曲 fast 档 | ~8–12s | 不含和弦/结构 |
| 同文件同档位二次分析 | <0.1s | 内容哈希缓存命中 |
| E2E 全链（分析命中缓存 + plan + suno 包 + evaluate + revise） | 5.0s | MCP in-process 实测 |
| 测试套件全量（R1 24 项） | 84–160s | 随缓存状态波动 |

## 2026-11-20 R2 增量实测

| 场景 | 耗时 | 说明 |
|---|---|---|
| demucs CPU 分轨 76s 曲（htdemucs，two-stems 冒烟） | 44s 分轨 / 109s 含模型加载 | 主 venv torch 2.15.0+cpu；GPU 版 torch 未装（D13），runtime 如实报 cpu |
| demucs CPU 四轨分轨 80s 曲（Deep 档） | ~50–60s | 经 demucs.py API + soundfile 直读（绕沙箱 ffmpeg 写限制） |
| chordotomy 和弦引擎 vs 自研模板（benchmarks/chords_compare.json） | 三曲对比 | chordotomy 精度占优（pop 曲 4/4 truth 全覆盖），auto 档默认切换为 chordotomy |
| basic-pitch 转写（bp-venv 子进程，80s 曲 vocals 轨） | ~15–25s | ICASSP onnx 模型，MIDI 落盘 artifacts |
| Deep 档全链（分轨+转写+和弦，80s 曲） | ~90–150s | CPU 全路径实测 |
| 测试套件全量（R2 后 99 项） | ~207s | 80 引擎/回环 + 19 错误矩阵 |
| ACE-Step 模型下载（main bundle + 0.6B LM，ModelScope 回退） | ~8 分钟 | HF 镜像超时后自动切 ModelScope（31.9MB/s） |
| ACE-Step REST 服务启动（uvicorn 8001） | ~60s 到 openapi 200 | 引擎加载后 38 路由可发现 |

## 2026-11-21 生成后端实测

| 场景 | 耗时 | 说明 |
|---|---|---|
| YuE2 Studio 全链（song_create → studio_wait → song_export） | 见 `examples/yue2_live_record.json` 的 `elapsed_sec` | 真实 60s 歌曲，驱动回环 MCP `127.0.0.1:8791` |
| ACE-Step REST 生成 | 见 `examples/ace_live_record.json` | uvicorn 8001，路由经 `/openapi.json` 运行时发现 |

生成耗时强依赖 VRAM 占用与模型加载状态，单次数值不具代表性，故以实盘记录的原始数据为准，
不在此处汇总。
