"""输入源解析（D22）：本地文件 / http(s) 直链媒体 / 视频网页 → 本机可分析音频路径。

统一入口 ``resolve_source(ref)``，把"用户给的东西"归一化成一份本机文件路径，
后续 inspect / decode / 分析逻辑完全不需要知道来源差异。

设计取舍（DECISIONS.md D22）：
- 直链：标准库 urllib 流式下载，零新增核心依赖（D17 红线）。约定与参考工程
  工程语义一致（UA 头 / 120s 请求超时 / 1MB 分块 / .part 原子 rename），实现独立。
- 视频页：可选 yt-dlp 抽取 bestaudio，缺失时如实报 capability_unavailable。
- 只接受 http/https；大小（512MB）与超时（请求 120s / 流式读取 300s）都有硬上限，
  避免 Agent 被超大资源或慢速服务拖死。
- 下载内容只落本机 ``data/downloads/``（gitignore），绝不二次外发。
按 URL 的 sha256 前缀分目录，重复调用同一链接直接复用已下载文件。
"""

from __future__ import annotations

import hashlib
import http.client
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit
from urllib.request import Request, urlopen

from rea_music import __version__
from rea_music.core.errors import (BackendUnavailable, CapabilityUnavailable,
                                   FileNotFound, InvalidAudio, ValidationError)
from rea_music.core.hash import file_sha256
from rea_music.core.paths import downloads_dir

# ---------- 硬上限（对 Agent 与用户都可见，见 capabilities().input） ----------
MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024      # 512MB：远超单曲母带体积
DOWNLOAD_TIMEOUT_SEC = 120.0                # urlopen 单次 socket 操作上限
READ_TIMEOUT_SEC = 300.0                    # 建连后放宽的流式读取上限（大文件友好）
YTDLP_TIMEOUT_SEC = 900.0                   # 视频页抽取总超时
_CHUNK_BYTES = 1 << 20                      # 1MB 分块：内存占用恒定
_SNIFF_BYTES = 4096

_UA = (f"Mozilla/5.0 (compatible; REA-Music/{__version__}; "
       "+https://github.com/haining6/rea-music)")
_META_NAME = "_source.json"                 # 下载目录侧车元数据
_HINT_INSTALL_YTDLP = ("pip install yt-dlp（或 pip install 'rea-music[fetch]'）"
                       "后重试；也可先下载到本机再用本地路径分析")

# 直链视频容器：内容里没音频流时由 ffmpeg 解码路径如实报错
_EXTRA_MEDIA_EXT = {".mkv", ".mov", ".avi", ".flv", ".m4v", ".ts", ".m2ts",
                    ".mpg", ".mpeg", ".weba"}

_CTYPE_EXT = {
    "audio/mpeg": ".mp3", "audio/mp3": ".mp3", "audio/wav": ".wav",
    "audio/x-wav": ".wav", "audio/wave": ".wav", "audio/flac": ".flac",
    "audio/x-flac": ".flac", "audio/ogg": ".ogg", "audio/opus": ".opus",
    "audio/mp4": ".m4a", "audio/x-m4a": ".m4a", "audio/aac": ".aac",
    "audio/x-aac": ".aac", "audio/aiff": ".aiff", "audio/x-aiff": ".aiff",
    "video/mp4": ".mp4", "video/webm": ".webm", "video/quicktime": ".mov",
    "video/x-matroska": ".mkv", "video/mpeg": ".mpg",
}
# 文件头魔数兜底：CDN 常把音频标成 application/octet-stream 且 URL 无扩展名
_MAGIC_EXT = ((b"ID3", ".mp3"), (b"fLaC", ".flac"), (b"OggS", ".ogg"),
              (b"RIFF", ".wav"), (b"FORM", ".aiff"),
              (b"\x1aE\xdf\xa3", ".webm"))


class _PageDetected(Exception):
    """直链响应其实是网页/播放列表：改走 yt-dlp（内部信号，不外泄）。"""

    def __init__(self, url: str, reason: str):
        super().__init__(reason)
        self.url = url
        self.reason = reason


# ---------- 公共 API ----------

def resolve_source(ref: str, *, redownload: bool = False) -> dict:
    """把任意输入引用归一化为本机文件。

    Args:
        ref: 本地路径、http(s) 直链媒体或视频/网页链接。
        redownload: True 时忽略已下载缓存，重新拉取远端内容。

    Returns:
        {local_path, kind, origin_url, tool, bytes, sha256}，其中
        kind ∈ {local, http_media, page}，
        tool ∈ {soundfile, ffmpeg, urllib, yt_dlp}。

    Raises:
        ValidationError: 引用为空、是目录或 scheme 非 http/https。
        FileNotFound: 本地路径不存在，或远端返回 404/410。
        InvalidAudio: 远端内容不是可分析的音视频、超大小上限。
        CapabilityUnavailable: 视频页需要 yt-dlp 但本机未安装。
        BackendUnavailable: 网络不可达或远端 5xx。
    """
    if not isinstance(ref, str):
        raise ValidationError("输入引用必须是字符串路径或 URL",
                             hint="传本地文件绝对路径或 http(s) 链接。")
    text = ref.strip()
    if not text:
        raise ValidationError("输入引用为空",
                             hint="传本地文件绝对路径或 http(s) 链接。")
    scheme = urlsplit(text).scheme.lower()
    if scheme in ("http", "https"):
        _assert_url_allowed(text)
        return _resolve_remote(text, redownload=redownload)
    if scheme == "sha256":
        raise ValidationError(
            f"{text[:24]} 是内容哈希引用，不是可解析的文件",
            hint="哈希用于 get_analysis/compare 等结果引用；分析入口需要"
                 "本地路径或 http(s) 链接。",
            details={"scheme": scheme})
    if scheme and not _is_drive_letter(text):
        raise ValidationError(
            f"不支持的 URL scheme: {scheme}://",
            hint="只接受 http/https 链接或本地文件路径；"
                 "file:// 请直接传文件路径。",
            details={"scheme": scheme})
    return _resolve_local(text)


def is_remote(ref: str) -> bool:
    """判断引用是否为 http(s) 链接（Windows 盘符不误判）。"""
    return isinstance(ref, str) and urlsplit(ref.strip()).scheme.lower() in (
        "http", "https")


def _assert_url_allowed(url: str) -> None:
    """SSRF 护栏：拒绝云元数据/链路本地/保留段，私网与回环需显式 opt-in。

    MCP 场景下链接由 Agent 提供，可能被提示注入；`resolve_source` 是唯一的
    远端入口，必须在发起请求前判定目标地址（见 ``core/net_guard.py``）。

    按 net_guard 的契约表分三类处置：
    - 地址落在禁止段（含 ``blocked``）：安全边界，直接向上抛；
    - 主机名解析不出来（``HostUnresolvable``）：放行 —— 交给网络层报
      ``backend_unavailable``，不在入口把"没网"说成"地址非法"（解析不出来的
      主机本来也连不上，放行不构成 SSRF）；
    - scheme/主机名/端口非法：naturally 向上抛（输入问题，不该被忽略）。
    """
    from rea_music.core import net_guard
    from rea_music.core.config import load_config

    try:
        allow_private = bool((load_config().get("input") or {})
                             .get("allow_private_networks", False))
    except Exception:  # noqa: BLE001 —— 配置读取失败按最严策略处理
        allow_private = False
    try:
        net_guard.assert_public_url(url, allow_private=allow_private)
    except net_guard.HostUnresolvable:
        return


def accepted_formats() -> list[str]:
    """当前可分析的媒体扩展名清单（供 capabilities 与错误提示复用）。"""
    return sorted(_media_exts())


def yt_dlp_status() -> dict:
    """探测 yt-dlp：优先 PATH 上的可执行文件，其次当前解释器的 yt_dlp 模块。"""
    path = shutil.which("yt-dlp")
    if path:
        return {"available": True, "path": path, "mode": "binary",
                "launcher": [path], "version": _probe_ytdlp_version([path]),
                "hint": _HINT_INSTALL_YTDLP}
    if _module_available("yt_dlp"):
        launcher = [sys.executable, "-m", "yt_dlp"]
        return {"available": True, "path": None, "mode": "python_module",
                "launcher": launcher,
                "version": _probe_ytdlp_version(launcher),
                "hint": _HINT_INSTALL_YTDLP}
    return {"available": False, "path": None, "mode": None, "launcher": [],
            "version": None, "hint": _HINT_INSTALL_YTDLP}


# ---------- 本地路径 ----------

def _resolve_local(ref: str) -> dict:
    path = Path(ref).expanduser()
    if not path.exists():
        raise FileNotFound(f"文件不存在: {ref}",
                           hint="本地路径请确认拼写；远程内容请传 http(s) 链接。")
    if path.is_dir():
        raise ValidationError(f"{ref} 是目录，不是音频文件",
                             hint="传具体文件路径，例如 song.wav。")
    resolved = path.resolve()
    suffix = resolved.suffix.lower()
    return {
        "local_path": str(resolved),
        "kind": "local",
        "origin_url": None,
        "tool": _tool_for_ext(suffix),
        "bytes": resolved.stat().st_size,
        "sha256": None,           # 由 inspect 按需计算，避免重复读盘
        "file_name": resolved.name,
    }


def _is_drive_letter(ref: str) -> bool:
    return len(ref) > 2 and ref[0].isalpha() and ref[1] == ":"


def _tool_for_ext(suffix: str) -> str:
    """soundfile 能直读的走主路径，其余交给 ffmpeg（含视频容器抽音频）。"""
    return "soundfile" if suffix in _soundfile_exts() else "ffmpeg"


def _soundfile_exts() -> set[str]:
    from rea_music.analysis.audio import SOUNDFILE_EXTS  # 延迟导入避免循环
    return SOUNDFILE_EXTS


def _media_exts() -> set[str]:
    from rea_music.analysis.inspect import AUDIO_EXT  # 延迟导入避免循环
    return AUDIO_EXT | _EXTRA_MEDIA_EXT


# ---------- 远端 ----------

def _resolve_remote(url: str, *, redownload: bool = False) -> dict:
    cached = _cached_download(url)
    if cached and not redownload:
        return cached
    try:
        payload = _download_http(url)
    except _PageDetected:
        payload = _download_via_ytdlp(url)
    payload["origin_url"] = url
    _write_meta(url, payload)
    return payload


def _download_http(url: str) -> dict:
    """直链媒体：流式落盘。响应是网页/播放列表时抛 _PageDetected 转 yt-dlp。

    下载约定（与参考工程语义一致，实现独立）：UA 头、请求超时 120s、
    1MB 分块、先写 <name>.part 再原子 rename，杜绝半截文件被当缓存复用。
    """
    request = Request(url, headers={"User-Agent": _UA,
                                    "Accept": "audio/*,video/*,*/*;q=0.8"})
    try:
        response = urlopen(request, timeout=DOWNLOAD_TIMEOUT_SEC)
    except HTTPError as exc:
        raise _http_error(url, exc) from None
    except URLError as exc:
        raise BackendUnavailable(
            f"无法连接 {url}: {getattr(exc, 'reason', exc)}",
            hint="确认链接可访问、本机网络可用；内网地址需先放通。") from None
    except (TimeoutError, OSError, ValueError,
            http.client.HTTPException) as exc:
        raise BackendUnavailable(
            f"建立连接失败: {exc}",
            hint="确认链接是合法 http(s) URL 且网络可达。") from None

    with closing(response):
        ctype = _content_type(response)
        total = _content_length(response)
        if total is not None and total > MAX_DOWNLOAD_BYTES:
            raise _too_large(total, url)
        _relax_read_timeout(response, READ_TIMEOUT_SEC)
        try:
            first = response.read(_SNIFF_BYTES) or b""
        except (TimeoutError, OSError) as exc:
            raise BackendUnavailable(f"读取远端数据超时/失败: {exc}",
                                     hint="链接响应过慢，先下载到本机再分析。") from None

        name = _pick_filename(url, ctype, first)
        if _needs_page_resolver(ctype, name, first):
            raise _PageDetected(url, f"content-type={ctype or 'unknown'} 需页面解析")
        if not _is_media_payload(ctype, name, first):
            raise InvalidAudio(
                f"链接内容不是音频/视频（content-type={ctype or 'unknown'}）",
                hint="直链需指向媒体文件本身；视频页/播放列表链接会自动走 "
                     "yt-dlp（需安装）。支持格式："
                     + "、".join(accepted_formats()),
                details={"content_type": ctype, "url": url})
        return _stream_to_disk(url, response, first, name, ctype, total)


def _stream_to_disk(url: str, response, first: bytes, name: str,
                    ctype: str, total: int | None = None) -> dict:
    out_dir = _target_dir(url)
    dest = out_dir / name
    tmp = out_dir / f"{name}.part"
    digest = hashlib.sha256()
    written = 0
    try:
        with open(tmp, "wb") as handle:
            if first:
                written += len(first)
                digest.update(first)
                handle.write(first)
            while True:
                chunk = response.read(_CHUNK_BYTES)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_DOWNLOAD_BYTES:
                    raise _too_large(written, url)
                digest.update(chunk)
                handle.write(chunk)
        if written == 0:
            raise InvalidAudio(f"链接返回空内容: {url}",
                               hint="确认链接有效；网页链接请改用可下载的媒体直链。")
        # 完整性校验：HTTPResponse.read(amt) 对"提前断开"只返回 b''（CPython 为
        # 兼容性不抛 IncompleteRead），不比对就写会把半截文件当成品缓存复用。
        if total is not None and written != total:
            raise BackendUnavailable(
                f"下载不完整: 期望 {total} 字节，实际 {written} 字节",
                hint="远端连接中途断开（限速/超时/CDN 断流）；稍后重试，"
                     "或先下载到本机再分析。",
                details={"url": url, "expected_bytes": total,
                         "written_bytes": written})
        tmp.replace(dest)              # os.replace：同盘原子改名，不留半截文件
    except BaseException:
        tmp.unlink(missing_ok=True)   # 半截文件绝不留存（否则会被当缓存复用）
        raise
    return {
        "local_path": str(dest),
        "kind": "http_media",
        "origin_url": url,
        "tool": "urllib",
        "bytes": written,
        "sha256": digest.hexdigest(),
        "content_type": ctype,
        "file_name": name,
    }


def _download_via_ytdlp(url: str) -> dict:
    """视频页：yt-dlp 抽 bestaudio 落盘，后续由既有 ffmpeg 路径解码。"""
    status = yt_dlp_status()
    if not status["available"]:
        raise CapabilityUnavailable(
            "该链接是视频/网页，抽取音频需要 yt-dlp（本机未安装）",
            hint=status["hint"],
            details={"engine": "yt_dlp", "url": url})
    out_dir = _target_dir(url)
    before = {p.name for p in out_dir.iterdir()}
    cmd = [
        *status["launcher"],
        "--no-playlist", "--no-progress", "--no-warnings",
        "--restrict-filenames", "--no-part",
        "-f", "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best",
        "-o", str(out_dir / "%(id)s.%(ext)s"),
        url,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=YTDLP_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        _cleanup_new(out_dir, before)
        raise BackendUnavailable(
            f"yt-dlp 抽取超时（>{YTDLP_TIMEOUT_SEC:.0f}s）",
            hint="链接可能是长视频直播流；先本地下载再分析。") from None
    except OSError as exc:
        raise CapabilityUnavailable(
            f"yt-dlp 无法执行: {exc}", hint=status["hint"]) from None

    if proc.returncode != 0:
        _cleanup_new(out_dir, before)
        stderr = (proc.stderr or "").strip()
        tail = stderr.splitlines()[-1] if stderr else f"exit {proc.returncode}"
        if _looks_like_network_failure(stderr):
            raise BackendUnavailable(f"yt-dlp 无法访问链接: {tail}",
                                     hint="确认链接公开可访问、网络可达。")
        raise InvalidAudio(
            f"未能从链接提取音频: {tail}",
            hint="确认链接是公开的音视频页面；DRM/登录保护内容无法解析。",
            details={"url": url, "yt_dlp_exit": proc.returncode})

    created = [out_dir / n for n in ({p.name for p in out_dir.iterdir()} - before)]
    media = [p for p in created
             if p.is_file() and p.suffix.lower() in _media_exts()
             and p.stat().st_size > 0]
    if not media:                       # -o 模板可能落到子目录，兜底全目录扫描
        media = _scan_media(out_dir)
    if not media:
        raise InvalidAudio("yt-dlp 未产出可识别的音频文件",
                           hint="确认链接页面内确有可下载的音轨。")
    dest = max(media, key=lambda p: p.stat().st_size)
    return {
        "local_path": str(dest),
        "kind": "page",
        "origin_url": url,
        "tool": "yt_dlp",
        "bytes": dest.stat().st_size,
        "sha256": file_sha256(dest),
        "file_name": dest.name,
    }


# ---------- 下载目录与缓存 ----------

def _url_key(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]


def _target_dir(url: str) -> Path:
    out = downloads_dir() / _url_key(url)
    out.mkdir(parents=True, exist_ok=True)
    return out


def _write_meta(url: str, payload: dict) -> None:
    meta = {k: payload.get(k) for k in
            ("kind", "tool", "sha256", "bytes", "content_type", "file_name")}
    meta["origin_url"] = url
    meta["fetched_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    try:
        (downloads_dir() / _url_key(url) / _META_NAME).write_text(
            json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass  # 侧车文件只是加速元数据，写失败不影响主流程


def _cached_download(url: str) -> dict | None:
    out_dir = downloads_dir() / _url_key(url)
    if not out_dir.is_dir():
        return None
    files = _scan_media(out_dir)
    if not files:
        return None
    meta = _read_meta(out_dir) or {}
    preferred = out_dir / str(meta.get("file_name") or "")
    dest = preferred if preferred.is_file() else max(
        files, key=lambda p: p.stat().st_size)
    # 完整性交叉校验：侧车记录的字节数与实际落盘不一致（历史半截文件、
    # 被外部改动）时视为未命中，重新下载而不是把坏文件喂给解码器。
    expected = meta.get("bytes")
    try:
        actual = dest.stat().st_size
    except OSError:
        return None
    if isinstance(expected, int) and not isinstance(expected, bool) \
            and expected != actual:
        return None
    if actual <= 0:
        return None
    return {
        "local_path": str(dest),
        "kind": meta.get("kind") or "http_media",
        "origin_url": url,
        "tool": meta.get("tool") or (
            "yt_dlp" if meta.get("kind") == "page" else "urllib"),
        "bytes": dest.stat().st_size,
        "sha256": meta.get("sha256"),
        "file_name": dest.name,
        "cached": True,
    }


def _read_meta(out_dir: Path) -> dict | None:
    path = out_dir / _META_NAME
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _scan_media(out_dir: Path) -> list[Path]:
    known = _media_exts()
    try:
        entries = list(out_dir.iterdir())
    except OSError:
        return []
    return [p for p in entries
            if p.is_file() and not p.name.startswith("_")
            and not p.name.endswith(".part")
            and p.suffix.lower() in known and p.stat().st_size > 0]


def _cleanup_new(out_dir: Path, before: set[str]) -> None:
    for path in out_dir.iterdir():
        if path.name not in before and path.is_file():
            path.unlink(missing_ok=True)


# ---------- 响应头/内容判定 ----------

def _content_type(response) -> str:
    raw = ""
    try:
        raw = response.headers.get("Content-Type") or ""
    except AttributeError:
        raw = ""
    return raw.split(";")[0].strip().lower()


def _content_length(response) -> int | None:
    try:
        raw = response.headers.get("Content-Length")
    except AttributeError:
        return None
    if not raw:
        return None
    raw = str(raw).strip()
    return int(raw) if raw.isdigit() else None


def _relax_read_timeout(response, seconds: float) -> None:
    """urllib 只有一个 timeout 参数，建连后用 socket 层把读取上限放宽。"""
    try:
        response.fp.raw._sock.settimeout(seconds)  # type: ignore[attr-defined]
    except (AttributeError, OSError, ValueError):
        return


def _needs_page_resolver(ctype: str, name: str, first: bytes) -> bool:
    """HLS/DASH 播放列表与 HTML 页面都必须交给 yt-dlp。"""
    if ctype.startswith(("audio/", "video/")):
        return False
    if ctype in ("application/x-mpegurl", "application/vnd.apple.mpegurl",
                 "application/dash+xml"):
        return True
    ext = Path(name).suffix.lower()
    if ext in (".m3u8", ".mpd"):
        return True
    return _looks_like_html(ctype, first)


def _looks_like_html(ctype: str, first: bytes) -> bool:
    if ctype in ("text/html", "application/xhtml+xml", "text/xml"):
        return True
    head = first[:512].lstrip().lower()
    if head.startswith((b"<!doctype html", b"<html")):
        return True
    return head.startswith(b"<?xml") and b"<html" in head


def _is_media_payload(ctype: str, name: str, first: bytes) -> bool:
    if ctype.startswith(("audio/", "video/")):
        return True
    if ctype in ("application/ogg", "application/mp4", "application/x-flac"):
        return True
    if Path(name).suffix.lower() in _media_exts():
        return True
    return _magic_ext(first) is not None


def _magic_ext(first: bytes) -> str | None:
    for signature, ext in _MAGIC_EXT:
        if first.startswith(signature):
            return ext
    if first[4:8] == b"ftyp":      # ISO BMFF（m4a/mp4）
        return ".m4a"
    return None


def _pick_filename(url: str, ctype: str, first: bytes = b"") -> str:
    """URL basename 优先；无扩展名时按 content-type / 文件头魔数补全。"""
    raw = unquote(urlsplit(url).path)
    name = Path(raw).name if raw else ""
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name).strip(" .")
    if not name:
        name = "download"
    if len(name) > 120:
        stem, ext = Path(name).stem[:110], Path(name).suffix
        name = f"{stem}{ext}"
    if not Path(name).suffix:
        name += _CTYPE_EXT.get(ctype) or _magic_ext(first) or ".bin"
    return name


def _too_large(size: int, url: str) -> InvalidAudio:
    limit_mb = MAX_DOWNLOAD_BYTES / (1024 * 1024)
    return InvalidAudio(
        f"远端内容 {size / (1024 * 1024):.1f}MB 超过上限 {limit_mb:.0f}MB",
        hint="先在本机裁剪到上限内，或分段下载后用本地文件路径分析。",
        details={"url": url, "max_mb": round(limit_mb), "bytes": size})


def _http_error(url: str, exc: HTTPError) -> Exception:
    code = getattr(exc, "code", None)
    if code in (404, 410):
        return FileNotFound(f"链接不存在（HTTP {code}）: {url}",
                            hint="确认链接有效未过期；必要时换公开分享链接。",
                            details={"http_status": code, "url": url})
    if code in (401, 403):
        return InvalidAudio(
            f"链接拒绝访问（HTTP {code}）: {url}",
            hint="该资源需要登录/授权；换公开链接，或先下载到本机再分析。",
            details={"http_status": code, "url": url})
    if code is not None and code >= 500:
        return BackendUnavailable(f"远端服务错误（HTTP {code}）: {url}",
                                  hint="稍后重试；或先下载到本机再分析。",
                                  details={"http_status": code, "url": url})
    return InvalidAudio(f"下载失败（HTTP {code}）: {url}",
                        hint="确认链接直接指向可下载的媒体文件。",
                        details={"http_status": code, "url": url})


def _looks_like_network_failure(stderr: str) -> bool:
    keys = ("unable to download", "failed to resolve", "timed out", "timeout",
            "connection", "getaddrinfo", "ssl", "network is unreachable")
    lowered = stderr.lower()
    return any(k in lowered for k in keys)


def _module_available(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def _probe_ytdlp_version(launcher: list[str]) -> str | None:
    try:
        proc = subprocess.run([*launcher, "--version"], capture_output=True,
                              text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return (proc.stdout or "").strip() or None
