"""抓取并裁剪曲包音频（契约：``docs/packs-audio-v1.md``）。

    UV_CACHE_DIR=.uv/cache uv run python -m otomads.fetch_audio [选项]

选项：
    --track <子串>   只处理标题或作者含该子串的曲目
    --dry-run        只打印计划，不下载不裁剪
    --force          忽略状态，重下重裁
    --skip-update    连"检查 yt-dlp 新版"都不做（改名叫 --skip-update 之前是 --offline-ok：
                     它管的不只是离线 —— 只要不想让这一步联网/升级，就用它）
    --jobs N         并发（默认 4；1 = 串行）。**下载/裁剪**与之后的**量响度**共用它

流程：依赖检查 → yt-dlp 更新 → **并发**下载 → 裁剪（`-c copy`）→ 顺带量响度 → 汇总。
产物：``<曲库>/<专辑>/<作者> - <标题>.mp3``（运行时唯一被读到的文件），
原件留在 ``<曲库>/.raw/``、状态在 ``<曲库>/.state/<pack>.json``（都在点目录里，
曲库助手的扫描会跳过它们 —— 见 `local_source.scan_library`）。

**为什么并发在这一层**（而不是 yt-dlp 的开关上）：yt-dlp 的 Python API 是**按实例**的
（一个 `YoutubeDL` 处理一条 URL），而 `--concurrent-fragments` 只对 HLS/DASH 的**分片**流
起作用 —— bilibili 的音频是**单个文件**直链，没有分片可并行。可测的账（本机 32 核）：

* `import yt_dlp` + `YoutubeDL()` + extractor 匹配 ≈ 0.13 秒/首；
* 4 分钟 m4a → mp3 全量重编码 ≈ 1.0 秒/首，`-c copy` 裁剪 ≈ 0.08 秒/首；
* 86 首的**本地**开销合计 ≈ 23 秒 —— 其余 **100% 是网络**（playurl 往返 + 音频本体），
  而那段时间 CPU 完全空闲。所以并行重叠的是**网络等待**，收益随 `--jobs` 增长，
  上限由带宽与站点风控决定（bilibili 对高并发有 412 风控：先用 4 试）。

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
    """
    import yt_dlp

    raw.parent.mkdir(parents=True, exist_ok=True)
    options = {
        "format": "bestaudio/best",
        "outtmpl": str(raw.with_suffix("")) + ".%(ext)s",
        "quiet": True, "no_warnings": True, "noprogress": True, "retries": 3,
        "cachedir": False,
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3",
                            "preferredquality": "0"}],
    }
    with yt_dlp.YoutubeDL(options) as ydl:
        ydl.download([source])
    if not raw.exists():
        others = sorted(path.name for path in raw.parent.glob(raw.stem + ".*"))
        raise RuntimeError(f"下载完成但没得到 {raw.name}（实际文件：{others or '无'}）")


def ensure_raw(source: str, raw_path: pathlib.Path, *, force: bool, stale_source: bool) -> None:
    """确保原件在 `.raw/`（已在且来源没变就**不重下**）。

    "要不要下"这个判断和下载本身必须在**同一把锁**里：两条曲目共用同一个 `source` 时，
    后到的线程等前一个下完，再看一眼就发现原件已在 → 直接复用 ✓（`--force` 时确实会各下一遍，
    但那本来就是"强制重来"的语义）。
    """
    with _raw_lock(source):
        if force or not raw_path.exists() or stale_source:
            download(source, raw_path)


def render(raw: pathlib.Path, out: pathlib.Path, wanted: tuple[float, float | None] | None,
           tmp_dir: pathlib.Path) -> None:
    """产出成品：不裁剪 → 直接硬链接；裁剪 → ffmpeg `-c copy` 到临时文件再**原子改名**。

    原子改名是硬链接方案的前提：就地写会把共享 inode 的另一首一起改掉 ✗。
    """
    if wanted is None:
        link_or_copy(raw, out)
        return
    start, duration = wanted
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp = tmp_dir / f"{out.stem}.tmp.mp3"
    command = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
               "-nostdin",                  # 见下：别让 ffmpeg 去碰用户的终端
               "-ss", f"{start:.3f}", "-i", str(raw)]
    if duration is not None:                 # None = 一直裁到文件结尾
        command += ["-t", f"{duration:.3f}"]
    command += ["-c", "copy", str(tmp)]
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

    signature = {"source": source, "start": track.get("start_time", ""), "stop": track.get("stop_time", "")}
    raw_path = library / RAW_DIR / f"{packs.source_key(source)}.mp3"
    # 状态里这些字段是**内联**存的（见下方写回），所以这里也按内联比
    fresh = (raw_path.exists() and out_path.exists()
             and entry.get("source") == signature["source"]
             and entry.get("start", "") == signature["start"]
             and entry.get("stop", "") == signature["stop"]
             and entry.get("outHash") == hash_file(out_path))
    if fresh and not args.force:
        return {"status": "skip", "title": title, "detail": "已是目标状态"}, {}

    if args.dry_run:
        action = "下载 + 裁剪" if wanted else "下载"
        if not raw_path.exists():
            return {"status": "dry", "title": title, "detail": f"{action} ← {source}"}, {}
        return {"status": "dry", "title": title,
                "detail": f"{'裁剪' if wanted else '落成品'}（原件已在）"}, {}

    claimed: dict[tuple[str, str, str], pathlib.Path] = {}
    try:
        # 原件是按 **source** 存的：只要它还在、且这条曲目没换来源，就不重新下载
        # （同一 source 的第二条曲目本来就没有自己的状态，不能因此重下 ✗）
        stale_source = bool(entry.get("source")) and entry["source"] != source
        ensure_raw(source, raw_path, force=args.force, stale_source=stale_source)

        twin_key = (source, signature["start"], signature["stop"])
        if args.force:
            render(raw_path, out_path, wanted, library / TMP_DIR)
            action = "trimmed" if wanted else "fetched"
        else:
            # **认领与产出必须在同一把锁里**：登记表里的路径是给别的线程拿去硬链接的，
            # 认领了却还没产出的话，后到的线程会发现 `twin.exists()` 为假 → 白裁一遍 ✗
            # （实测：8 条里两条同源曲目都走成 `fetched`）。代价是"渲染"在锁里串行 ——
            # 但 `-c copy` 只要 ~0.08 秒，而不同 source 用的是不同的键，互不阻塞 ✓
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
