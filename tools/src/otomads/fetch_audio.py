"""抓取并裁剪曲包音频（契约：``docs/packs-audio-v1.md``）。

    UV_CACHE_DIR=.uv/cache uv run python -m otomads.fetch_audio [选项]

选项：
    --track <子串>   只处理标题或作者含该子串的曲目
    --dry-run        只打印计划，不下载不裁剪
    --force          忽略状态，重下重裁
    --skip-update    连"检查 yt-dlp 新版"都不做（改名叫 --skip-update 之前是 --offline-ok：
                     它管的不只是离线 —— 只要不想让这一步联网/升级，就用它）
    --jobs N         并发（默认 4；1 = 串行）。**下载/裁剪**与之后的**量响度**共用它

流程：依赖检查 → yt-dlp 更新 → **并发**下载 → 裁剪（解码后精确切 + 重编码，只对带区间的曲目）
→ 顺带量响度 → 汇总。
产物：``<曲库>/<专辑>/<作者> - <标题>.mp3``（运行时唯一被读到的文件），
原件留在 ``<曲库>/.raw/``、状态在 ``<曲库>/.state/<pack>.json``（都在点目录里，
曲库助手的扫描会跳过它们 —— 见 `local_source.scan_library`）。

**为什么并发在这一层**（而不是 yt-dlp 的开关上）：yt-dlp 的 Python API 是**按实例**的
（一个 `YoutubeDL` 处理一条 URL），而 `--concurrent-fragments` 只对 HLS/DASH 的**分片**流
起作用 —— bilibili 的音频是**单个文件**直链，没有分片可并行。可测的账（本机 32 核）：

* `import yt_dlp` + `YoutubeDL()` + extractor 匹配 ≈ 0.13 秒/首；
* 4 分钟 m4a → mp3 全量重编码 ≈ 1.0 秒/首；
* 裁剪（解码后精确切 + V0 重编码）≈ 0.4–0.6 秒/首，而且**只有 16 首**带区间 ⇒ 合计不到 10 秒；
* 86 首的**本地**开销合计 ≈ 30 秒（比 `-c copy` 那会儿多约 7 秒，就是上面这 16 首裁剪的差价）
  —— 其余 **100% 是网络**（playurl 往返 + 音频本体），而那段时间 CPU 完全空闲。
  所以并行重叠的是**网络等待**，收益随 `--jobs` 增长，上限由带宽与站点风控决定
  （bilibili 对高并发有 412 风控：先用 4 试）。

并发正确性靠三处：`_STATE_LOCK`（状态文件）、`_CLAIM_LOCK` + `outputs`（同源同区间只裁一次）、
`_raw_lock()`（同一 source 只下一份原件）。
"""
from __future__ import annotations

import argparse
import collections
import concurrent.futures
import contextlib
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

from . import local_source, loudness, packformat as packs, paths as repo

PYPI_URL = "https://pypi.org/pypi/yt-dlp/json"
RAW_DIR = ".raw"
STATE_DIR = ".state"
TMP_DIR = ".state/tmp"
REEXEC_FLAG = "TMC_FETCH_REEXEC"
DEFAULT_JOBS = 4

#: 裁剪用 libmp3lame 的**最高档 VBR**（V0，约 245 kbps）：裁剪必须重编码，二次有损躲不掉，
#: 那就把这一遍的损失压到最小（实测成品体积与 `-c copy` 基本持平：1581 vs 1586 KiB / 967 vs 980 KiB）。
TRIM_ENCODER = ["-c:a", "libmp3lame", "-q:a", "0"]
#: 定位到裁剪点**之前**这么多秒再开始解码，然后在输出侧把这一段丢掉（见 `render`）。
#: 0.5 秒 ≈ 19–21 个 mp3 帧，远多于 mp3 解码器喂热比特池所需的两三帧 —— 留余量不心疼（多解 0.5 秒音频）。
TRIM_WARMUP = 0.5
#: **抓取口径的版本号**，同样是状态签名的一部分：换了"怎么把 source 变成原件"，旧原件就作废、重下。
#: 2026-09-25：`download()` 补上 `noplaylist`（原来的 `ingest_otomads.py` 有 `--no-playlist`，
#: 搬到 `fetch_audio` 时丢了 ⇒ 多 P 视频会被当成选集整套抓、各 P 互相覆盖、最后留下**最后一 P**）。
#: 不 +1 的话那 4 条多 P source 的旧原件（p2）会被 `fresh` 判为"还是目标状态"而跳过 ✗。
FETCH_VERSION = "single-v1"
#: **渲染口径的版本号**，进状态签名：改了成品是怎么产出的，旧状态就自动作废、重裁一遍。
#: 2026-09-25：裁剪从 `-c copy` 改成"解码后精确切 + 重编码"（三处实测问题见 `render`），
#: 必须 +1 —— 否则旧的 `outHash` 仍然对得上，幂等检查会直接跳过那 16 首 ✗。
RENDER_VERSION = "encode-v1"
#: 不裁剪的成品是"与原件同一 inode 的硬链接"，字节与渲染口径**无关** ⇒ 签名位留空。
#: （不留空的话，改一次裁剪实现会连带 70 首未裁剪的曲目全部重链 + 重量响度，纯属噪音。）
LINK_RENDER = ""

#: 状态文件与"同源同区间只裁一次"的登记表都要跨线程用：一个锁管前者，一个锁管后者
_STATE_LOCK = threading.Lock()
_CLAIM_LOCK = threading.Lock()
_RAW_LOCKS: dict[str, threading.Lock] = {}
_RAW_LOCKS_LOCK = threading.Lock()


@contextlib.contextmanager
def _raw_lock(source: str):
    """同一 `source` 的下载串行化（不同 source 各用各的锁）。

    原件是按 **source** 存的（`.raw/<sha1(source)[:16]>.mp3`），所以两条曲目引用同一个
    `source` 时必须**只下一份** —— 不加锁的话两个线程会同时往同一个文件写 ✗。
    """
    with _RAW_LOCKS_LOCK:
        lock = _RAW_LOCKS.setdefault(source, threading.Lock())
    with lock:
        yield


# --------------------------------------------------------------------- 依赖

def version_key(text: str) -> tuple[int, ...]:
    """版本号 → 可比较的数字元组（`2026.08.19` 与 `2026.8.19` 必须算相同）。"""
    return tuple(int(part) for part in re.findall(r"\d+", text)) or (0,)


def installed_ytdlp() -> str:
    import yt_dlp
    return yt_dlp.version.__version__


def latest_ytdlp() -> str | None:
    """PyPI 上的最新版；查不到（离线 / 网络受限）返回 None。"""
    try:
        with urllib.request.urlopen(PYPI_URL, timeout=20) as response:
            return str(json.load(response)["info"]["version"])
    except (urllib.error.URLError, OSError, KeyError, ValueError, TypeError):
        return None


def uv_env() -> dict[str, str]:
    """`uv` 子进程的环境：本机沙箱下 `$HOME/.cache` 只读，必须落在仓库内。"""
    env = dict(os.environ)
    env.setdefault("UV_CACHE_DIR", str(repo.ROOT / "tools" / ".uv" / "cache"))
    return env


def ffmpeg_problem() -> str | None:
    """ffmpeg 不可用时返回原因（None = 可用）。"""
    try:
        done = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, timeout=20,
                      stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as error:
        return str(error)
    return None if done.returncode == 0 else f"`ffmpeg -version` 退出码 {done.returncode}"


def ensure_ytdlp(*, skip_update: bool, reexec: bool) -> str | None:
    """yt-dlp 更新策略（用户口径）：无新版 → 继续；有新版且升级成功 → 继续；升级失败 → 中止。

    `skip_update`（CLI `--skip-update`，原 `--offline-ok`）**整个跳过这一步** —— 不查 PyPI、不升级；
    它的语义是"这一步别做"，不只是"我离线"（离线只是最常见的用法）。

    升级成功后必须**重启进程**：旧版已经 import 进内存，同进程里换不掉。
    """
    installed = installed_ytdlp()
    if skip_update:
        print(f"· yt-dlp {installed}（--skip-update：跳过更新检查）")
        return None
    latest = latest_ytdlp()
    if latest is None:
        return ("无法检查 yt-dlp 更新（离线或网络受限）；本次抓取可能因站点改版而失败 ✗。"
                "确认要跑就加 --skip-update")
    if version_key(latest) <= version_key(installed):
        print(f"· yt-dlp {installed} 已是最新")
        return None

    print(f"· yt-dlp 有新版本 {latest}（当前 {installed}）→ 升级")
    tools = repo.ROOT / "tools"
    lock = subprocess.run(["uv", "lock", "--upgrade-package", "yt-dlp"], cwd=tools, env=uv_env(),
                          capture_output=True, text=True, stdin=subprocess.DEVNULL)
    sync = subprocess.run(["uv", "sync"], cwd=tools, env=uv_env(), capture_output=True, text=True,
                       stdin=subprocess.DEVNULL) \
        if lock.returncode == 0 else None
    if lock.returncode != 0 or sync is None or sync.returncode != 0:
        detail = (lock.stderr or lock.stdout).strip().splitlines()[-1:] or ["?"]
        return f"yt-dlp 升级失败（uv lock={lock.returncode}）→ 中止：{detail[0]}"
    if reexec:
        return f"yt-dlp 升级后仍是旧版本（当前 {installed_ytdlp()}）→ 中止，不再重入"

    print("· 升级成功 → 重启抓取进程（让新版生效）")
    os.environ[REEXEC_FLAG] = "1"
    os.execv(sys.executable, [sys.executable, "-m", "otomads.fetch_audio", *sys.argv[1:]])
    return None                              # 只有 execv 失败才会走到这里


# --------------------------------------------------------------------- 文件

def hash_file(path: pathlib.Path) -> str:
    try:
        with open(path, "rb") as handle:
            return hashlib.file_digest(handle, "sha1").hexdigest()[:16]
    except OSError:
        return ""


def same_inode(left: pathlib.Path, right: pathlib.Path) -> bool:
    try:
        return left.stat().st_ino == right.stat().st_ino and left.stat().st_dev == right.stat().st_dev
    except OSError:
        return False


def link_or_copy(src: pathlib.Path, dst: pathlib.Path) -> str:
    """硬链接（省磁盘）；不支持时回落复制并**明确提示**。目标已存在就先删掉。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        if same_inode(src, dst):
            return "hardlink"
        dst.unlink()
    try:
        os.link(src, dst)
        return "hardlink"
    except OSError as error:
        print(f"  · 硬链接不可用（{error.strerror}）→ 改用复制：{dst.name}")
        shutil.copy2(src, dst)
        return "copy"


def download(source: str, raw: pathlib.Path) -> None:
    """yt-dlp 下最高音质音频并转成 mp3（成品必须是 mp3：见 docs/packs-audio-v1.md §5）。

    `cachedir: False`：并发时每个线程各建一个 `YoutubeDL`，关掉缓存目录就**没有共享写点**
    了（yt-dlp 的 `YoutubeDL` 没有官方线程安全承诺，这里只共享"不写"的部分）。

    **`noplaylist: True` —— 一条 `source` = 一首曲目**（契约 §7 第 7 条），别让 yt-dlp 把一个
    source 当成播放列表整套抓。对 bilibili 的多 P 视频这条是**必须的**（D143）：

    * **不带它**：多 P 且链接里没写 `?p=` 时，extractor 走 `_yes_playlist()`
      （`yt_dlp/extractor/bilibili.py` 的 `is_anthology and not part_id and ...`）⇒ 返回**整张选集**，
      每 P 一个条目；而 `outtmpl` 是**固定的文件名** ⇒ 各 P 互相覆盖，最后留下的是**最后一 P**。
      实测本包 4 条多 P source 全部中招（`月时盆` 拿到 p2「原曲只使用」而不是 p1「原曲不使用」）。
    * **带上它**：`_yes_playlist()` 返回 False ⇒ 落到 `part_id = part_id or 1` ⇒ **默认 p1**；
      链接里写了 `?p=N` 时 `part_id` 已经有值、压根不进那个分支 ⇒ **仍按链接参数解析** ✓。
      两种写法都对，正是要的行为。

    历史脚本 `ingest_otomads.py` 当年传的是 CLI 的 `--no-playlist`（等价于这个键），
    是搬到 `fetch_audio` 时丢掉的 —— 这次补回来。
    """
    import yt_dlp

    raw.parent.mkdir(parents=True, exist_ok=True)
    options = {
        "format": "bestaudio/best",
        "outtmpl": str(raw.with_suffix("")) + ".%(ext)s",
        "quiet": True, "no_warnings": True, "noprogress": True, "retries": 3,
        "cachedir": False,
        "noplaylist": True,
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3",
                            "preferredquality": "0"}],
    }
    with yt_dlp.YoutubeDL(options) as ydl:
        # 先只解析、不落地：`noplaylist` 只是"在犹豫时选单个视频"，万一某个 extractor 仍然
        # 展开成播放列表，宁可**当场报错**，也不能让各条目覆盖同一个文件名、悄悄留下最后一 P
        # （那正是这次的病；报错也比默默给错音频强）。
        info = ydl.extract_info(source, download=False)
        entries = list((info or {}).get("entries") or [])
        if len(entries) > 1:
            raise RuntimeError(
                f"这个 source 解析出 {len(entries)} 个条目（播放列表 / 多 P 选集），"
                f"而一条 source 只能对应一首曲目 —— 请把 source 指到具体那一个"
                f"（bilibili 就在链接后面加 `?p=N`）")
        ydl.process_ie_result(info, download=True)
    if not raw.exists():
        others = sorted(path.name for path in raw.parent.glob(raw.stem + ".*"))
        raise RuntimeError(f"下载完成但没得到 {raw.name}（实际文件：{others or '无'}）")


def ensure_raw(source: str, raw_path: pathlib.Path, *, force: bool, stale: bool) -> None:
    """确保原件在 `.raw/`（已在、且**来源与抓取口径都没变**就**不重下**）。

    `stale` 有两层来源：状态里的 `source` 变了，或 `fetch`（抓取口径，见 `FETCH_VERSION`）
    变了 —— 后者尤其重要：口径变了但链接没变时，旧原件**必须换掉**（多 P 视频的 p2 就是
    这么留下来的），但状态里的 `source` 与 `outHash` 全都对得上，不显式判它就会被跳过 ✗。

    "要不要下"这个判断和下载本身必须在**同一把锁**里：两条曲目共用同一个 `source` 时，
    后到的线程等前一个下完，再看一眼就发现原件已在 → 直接复用 ✓（`--force` 时确实会各下一遍，
    但那本来就是"强制重来"的语义）。
    """
    with _raw_lock(source):
        if force or not raw_path.exists() or stale:
            download(source, raw_path)


def render(raw: pathlib.Path, out: pathlib.Path, wanted: tuple[float, float | None] | None,
           tmp_dir: pathlib.Path) -> None:
    """产出成品：不裁剪 → 直接硬链接；裁剪 → **解码后精确切再重编码** 到临时文件再**原子改名**。

    原子改名是硬链接方案的前提：就地写会把共享 inode 的另一首一起改掉 ✗。

    **为什么不是 `-c copy`**（2026-09-25 之前就是它，实测三处问题，证据见 `docs/packs-audio-v1.md` §5）：

    1. **起点只能落在 mp3 帧边界上**。用户给的区间是 `2.339 / 1.388 / 1.060 / 5.168 / 3.557`
       这种毫秒值 —— 本来就是要**点在拍上**；帧长 24 ms（48 kHz）/ 26 ms（44.1 kHz），
       落不到就只能就近取整。实测：起点 `1.388s` 的成品偏 **+90 ms**、`2.339s` 偏 +1 ms、
       44.1 kHz 合成用例偏 −9.5 ms，源里有安静段（码率起伏 ⇒ 定位按字节估算）时见过 −78 ms。
    2. **容器时长与 `stop − start` 对不上**：实测 +8…+32 ms 偏长，也见过 −90 ms 偏短。
    3. **头一帧是坏的**。输入定位（`-ss` 放在 `-i` **前**）让解码器**冷启动**，而 mp3 的帧要用
       **比特池**（前几帧的主数据）—— 冷启动那一帧根本解不出内容。实测成品第 0 帧 RMS 只有真值的
       **2%**，有时整整一帧（24 ms）静音 ⇒ 听感就是"开头掉了一小块"。**这一条 `-c copy` 和
       朴素的"解码后重编码"都会中**，所以修法不是简单换个编码器。

    所以三段式：`-ss <start − 0.5s>` 粗定位 → 输出侧 `-ss 0.5s` 丢掉预热段 → `-t <时长>` → 重编码。
    输出侧那次 `-ss` 同时解决 1 和 3：起点回到**采样点级**精确，而被丢掉的预热段正好把冷冷解出来的
    头几帧一起丢掉（比特池喂热了）。实测：起点偏差 **0.000 ms**、解码时长**恰等于** `stop − start`、
    首帧 RMS 与真值一致（100%）、逐样本残差比 `-c copy` 还小。

    **不重采样**：源是 44.1 / 48 kHz，都是 mp3 原生支持的采样率 —— 再插一道 SRC 只是白添失真
    （ffmpeg 在编码器不支持某个采样率时会自动插重采样，这里用不上）。
    """
    if wanted is None:
        link_or_copy(raw, out)
        return
    start, duration = wanted
    back = min(TRIM_WARMUP, start)           # 起点本来就在 0.5 秒内 ⇒ 回退到文件头即可
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp = tmp_dir / f"{out.stem}.tmp.mp3"
    command = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
               "-nostdin",                  # 见下：别让 ffmpeg 去碰用户的终端
               "-ss", f"{start - back:.3f}", "-i", str(raw)]
    if back:
        # 输出侧的 `-ss`：丢掉预热段。它落在 `-i` **之后**，所以不解码器冷启动，只丢已解出来的样本。
        command += ["-ss", f"{back:.3f}"]
    if duration is not None:                 # None = 一直裁到文件结尾
        command += ["-t", f"{duration:.3f}"]
    command += [*TRIM_ENCODER, str(tmp)]
    # `stdin=DEVNULL`：ffmpeg 只要看到 stdin 是终端就会接管它（`-nostdin` 只管"要不要读"，
    # 不管"stdin 是不是 tty"），跑完可能让终端不回显 ✗。并发时更危险（多个 ffmpeg 同时抢）。
    subprocess.run(command, check=True, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    os.replace(tmp, out)


# --------------------------------------------------------------------- 主流程

def load_state(path: pathlib.Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            print(f"· 状态文件损坏，忽略：{path}")
    return {"version": 1, "tracks": {}}


def save_state(path: pathlib.Path, state: dict) -> None:
    """写状态文件（**串行化**：并发抓取时多个线程会同时收尾）。

    锁只包住"序列化 + 写"这一段：`state` 是各线程共享的对象，每个线程只写自己那条曲目的键，
    所以后写的线程会带上先写线程的内容 ✓（不这样就互相覆盖，表现为"跑完了但状态里少几条"）。

    读**不**在锁里 —— 状态在进线程池之前就全部读完了（见 `main`），运行期只有写。
    """
    with _STATE_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                        encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="抓取并裁剪曲包音频（docs/packs-audio-v1.md）")
    parser.add_argument("--track", default="", help="只处理标题或作者含该子串的曲目")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划")
    parser.add_argument("--force", action="store_true", help="忽略状态，重下重裁")
    parser.add_argument("--skip-update", action="store_true",
                        help="跳过 yt-dlp 更新检查（不查 PyPI、不升级；原 --offline-ok）")
    parser.add_argument("--jobs", type=int, default=DEFAULT_JOBS,
                        help=f"并发数（默认 {DEFAULT_JOBS}；1 = 串行）。下载与量响度共用")
    parser.add_argument("--config", type=pathlib.Path, default=repo.DEFAULT_CONFIG,
                        help="本地曲库配置（默认本仓库根的 local-source.toml）")
    args = parser.parse_args(argv)
    jobs = max(1, args.jobs)

    if (problem := ffmpeg_problem()) is not None:
        print(f"❌ 需要 ffmpeg 才能裁剪：{problem}\n"
              f"   Ubuntu/Debian: sudo apt install ffmpeg；macOS: brew install ffmpeg", file=sys.stderr)
        return 3

    config = local_source.load_config(args.config)
    library = pathlib.Path(config["root"])
    pack_list, _albums, tracks, _cards = packs.load_packs()
    needle = args.track.strip().lower()
    if needle:
        tracks = [track for track in tracks
                  if needle in track["title"].lower() or needle in (track.get("author") or "").lower()]
    print(f"曲库 {library} | 曲包 {', '.join(pack['id'] for pack in pack_list)} | "
          f"待处理 {len(tracks)} 条{'（--dry-run）' if args.dry_run else ''}"
          f"{f' | 并发 {jobs}' if jobs > 1 and not args.dry_run else ''}")

    if not args.dry_run:
        if (problem := ensure_ytdlp(skip_update=args.skip_update,
                                    reexec=os.environ.get(REEXEC_FLAG) == "1")) is not None:
            print(f"❌ {problem}", file=sys.stderr)
            return 3
    else:
        print("· --dry-run：跳过 yt-dlp 更新检查")

    # 状态**先全部读进来**再进线程池：`setdefault(key, load_state(...))` 那种写法会**每次都求值**
    # `load_state()`（Python 的参数先算），于是每个线程都去读一遍同一个文件 —— 正好读到别的线程
    # 写了一半的内容 ✗（实测：`--jobs 4` 会报"状态文件损坏"。修这一处比给读也加锁干净）
    states: dict[str, dict] = {}
    for pack_id in dict.fromkeys(track["pack"] for track in tracks):
        states[pack_id] = load_state(library / STATE_DIR / f"{pack_id}.json")
    outputs: dict[tuple[str, str, str], pathlib.Path] = {}
    changed: dict[str, set[str]] = collections.defaultdict(set)   # pack → 需要重量的曲目 stem
    results: list[dict] = []
    done = 0

    def run(track: dict) -> dict:
        """一条曲目（供线程池调用）。

        每个线程只动**自己那条曲目的键**（`state["tracks"][key]`），共享的 `state` 字典因此不需要
        锁；`outputs` 的写走 `_CLAIM_LOCK`（在 `process_track` 里）；状态**落盘**走 `save_state` 的锁。
        """
        pack_id = track["pack"]
        state_path = library / STATE_DIR / f"{pack_id}.json"
        state = states[pack_id]
        outcome, new_outputs = process_track(track, library=library, state=state, outputs=outputs,
                                            args=args, changed=changed[pack_id])
        if new_outputs:
            with _CLAIM_LOCK:
                outputs.update(new_outputs)
        if not args.dry_run and outcome["status"] in {"fetched", "trimmed", "linked"}:
            save_state(state_path, state)
        return outcome

    if jobs == 1 or len(tracks) <= 1:
        # 串行路径保持原样：单条时不要白起线程池（也让 `--jobs 1` 的输出顺序与过去完全一致）
        for track in tracks:
            results.append(run(track))
            done += 1
            print(f"  [{done}/{len(tracks)}] {track['title']}", flush=True)
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
            # 提交顺序 = tracks 顺序，所以 future 与 track 一一对应（zip 即可配回标题）
            futures = [pool.submit(run, track) for track in tracks]
            by_future = dict(zip(futures, (track["title"] for track in tracks)))
            for future in concurrent.futures.as_completed(futures):
                results.append(future.result())
                done += 1
                # 并发下结果顺序不确定，所以先把进度打出来（否则整段下载期间屏幕上什么都没有）
                print(f"  [{done}/{len(tracks)}] {by_future[future]}", flush=True)

    if not args.dry_run and any(changed.values()):
        for pack_id in sorted(changed):
            if not changed[pack_id]:
                continue
            output = packs.loudness_path(pack_id)
            if output is None:
                print(f"⚠️ 曲包 {pack_id} 的源注册表没声明 loudness，跳过刷新响度表")
                continue
            directories = sorted({library / track["album"] for track in tracks
                                  if track["pack"] == pack_id})
            summary = loudness.measure_library(directories, output=output, jobs=args.jobs,
                                               reset=set(changed[pack_id]))
            print("\n".join(loudness.describe(summary)))
            # 表与曲目的对应关系（只提示）—— 拿别的曲库跑时，这条会把"覆盖掉了"当场说出来
            mine = [track for track in tracks if track["pack"] == pack_id]
            report = loudness.coverage_report(mine, summary["gains"], packs.audio_stem)
            print("\n".join(loudness.describe_coverage(report)))

    counts = collections.Counter(outcome["status"] for outcome in results)
    print("\n汇总：" + "，".join(f"{status} {count}" for status, count in sorted(counts.items())))
    for outcome in results:
        status = outcome["status"]
        if status in {"fetched", "trimmed", "linked", "dry"}:
            detail = f"（{outcome['detail']}）" if outcome["detail"] else ""
            print(f"  · {status}：{outcome['title']}{detail}")
        elif status in {"missing", "failed"}:
            print(f"  ✗ {outcome['title']}：{outcome['detail']}")
    return 1 if counts["failed"] or counts["missing"] else 0


def process_track(track: dict, *, library: pathlib.Path, state: dict,
                  outputs: dict[tuple[str, str, str], pathlib.Path], args,
                  changed: set[str]) -> tuple[dict, dict[tuple[str, str, str], pathlib.Path]]:
    """一条曲目：下载 → 裁剪 → 成品。

    返回 `(outcome, new_outputs)`：`outcome` 是 `{status, title, detail}`（并发下**不保证顺序**，
    调用方按完成先后收）；`new_outputs` 是这个线程**新认领**的"同源同区间"登记项（其余线程拿到
    的是空字典，由调用方在锁里并进共享表）。

    并发下的两条纪律：

    * `state["tracks"][key]` 只由处理这条曲目的线程写 ⇒ 共享字典本身不需要锁；
    * `outputs` 的**读**（找 twin）按引用直接读（只读不写，`dict.get` 在 CPython 下是原子的），
      **写**只发生在 `_CLAIM_LOCK` 里 —— 否则两个线程会各自认领同一个 twin_key，同一份音频裁两遍。
    """
    title = track["title"]
    out_rel = pathlib.Path(track["album"]) / packs.audio_filename(track)
    out_path = library / out_rel
    key = f'{track["album"]}\u0001{title}'
    entry = state["tracks"].get(key, {})
    source = track.get("source", "")

    try:
        wanted = packs.trim_seconds(track)
    except ValueError as error:
        return {"status": "failed", "title": title, "detail": f"裁剪区间非法：{error}"}, {}

    if not source:
        if out_path.exists():
            return {"status": "skip", "title": title, "detail": "无 source（人工入库），保持原样"}, {}
        return {"status": "missing", "title": title,
                "detail": "没有 source，曲库里也没有这个文件（补 source 或手工放入曲库）"}, {}

    signature = {"source": source, "start": track.get("start_time", ""), "stop": track.get("stop_time", ""),
                 # 裁剪的成品带 **渲染口径**；不裁剪的成品是硬链接，口径与它无关（见 LINK_RENDER）
                 "render": RENDER_VERSION if wanted else LINK_RENDER,
                 # 原件的**抓取口径**（多 P 默认取哪一 P 就是它管的，见 FETCH_VERSION）
                 "fetch": FETCH_VERSION}
    raw_path = library / RAW_DIR / f"{packs.source_key(source)}.mp3"
    # 原件要不要重下：来源换了（同一条曲目改了 source）**或抓取口径换了**（旧原件可能是错的）。
    # 两条都在"状态里的 source/outHash 仍然对得上"时**照样成立** ⇒ 必须显式判，否则会被 fresh 跳过 ✗。
    # 只在**这条曲目以前抓过**（`entry` 非空）时判：状态里没有它的条目 = 第一次见它，此时原件可能是
    # 同 source 的孪生曲目刚下的那份 ⇒ 不能凭"没有 fetch 键"就重下（契约 §7 第 10 条：同源只下一份）
    stale_raw = bool(entry) and (entry.get("source") != source
                                 or entry.get("fetch") != FETCH_VERSION)
    # 状态里这些字段是**内联**存的（见下方写回），所以这里也按内联比
    fresh = (raw_path.exists() and out_path.exists()
             and entry.get("source") == signature["source"]
             and entry.get("start", "") == signature["start"]
             and entry.get("stop", "") == signature["stop"]
             and entry.get("render", "") == signature["render"]
             and entry.get("fetch", "") == signature["fetch"]
             and entry.get("outHash") == hash_file(out_path))
    if fresh and not args.force:
        return {"status": "skip", "title": title, "detail": "已是目标状态"}, {}

    if args.dry_run:
        action = "下载 + 裁剪" if wanted else "下载"
        if not raw_path.exists():
            return {"status": "dry", "title": title, "detail": f"{action} ← {source}"}, {}
        if stale_raw:
            # 原件在、但要换掉它（口径/来源变了）—— 计划里要**说出来**，不然会被读成"只重裁"
            return {"status": "dry", "title": title,
                    "detail": f"重下{' + 裁剪' if wanted else ''}（"
                              f"{'抓取口径变了' if entry.get('fetch') != FETCH_VERSION else '来源变了'}）"
                              f" ← {source}"}, {}
        return {"status": "dry", "title": title,
                "detail": f"{'裁剪' if wanted else '落成品'}（原件已在）"}, {}

    claimed: dict[tuple[str, str, str], pathlib.Path] = {}
    try:
        # 原件是按 **source** 存的：只要它还在、且这条曲目的来源与抓取口径都没变，就不重新下载
        # （同一 source 的第二条曲目本来就没有自己的状态，不能因此重下 ✗）
        ensure_raw(source, raw_path, force=args.force, stale=stale_raw)

        twin_key = (source, signature["start"], signature["stop"])
        if args.force:
            render(raw_path, out_path, wanted, library / TMP_DIR)
            action = "trimmed" if wanted else "fetched"
        else:
            # **认领与产出必须在同一把锁里**：登记表里的路径是给别的线程拿去硬链接的，
            # 认领了却还没产出的话，后到的线程会发现 `twin.exists()` 为假 → 白裁一遍 ✗
            # （实测：8 条里两条同源曲目都走成 `fetched`）。代价是"渲染"在锁里串行 ——
            # 裁剪现在是解码 + 重编码（≈ 0.4–0.6 秒/首，见模块 docstring 的账），比 `-c copy`
            # 慢一个量级，但**只有 16 首带区间**，且不同 source 用的是不同的键、互不阻塞 ✓
            with _CLAIM_LOCK:
                twin = outputs.get(twin_key)
                if twin is not None and twin != out_path and twin.exists():
                    link_or_copy(twin, out_path)          # 同一来源同一区间 → 硬链接
                    action = "linked"
                else:
                    render(raw_path, out_path, wanted, library / TMP_DIR)
                    outputs[twin_key] = out_path
                    claimed[twin_key] = out_path
                    action = "trimmed" if wanted else "fetched"
    except Exception as error:                    # yt-dlp / ffmpeg 的失败都按"这一条失败"处理
        return {"status": "failed", "title": title,
                "detail": f"{type(error).__name__}: {error}"}, claimed

    state["tracks"][key] = {**signature, "raw": str(pathlib.Path(RAW_DIR) / raw_path.name),
                            "out": str(out_rel), "outHash": hash_file(out_path),
                            "ytdlp": installed_ytdlp(), "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%S")}
    changed.add(out_path.stem)
    return {"status": action, "title": title,
            "detail": f"{track.get('start_time', '-')}–{track.get('stop_time', '-')}"
                      if wanted else "整首"}, claimed


if __name__ == "__main__":
    raise SystemExit(main())
