"""抓取并裁剪曲包音频（契约：``docs/packs-audio-v1.md``）。

    UV_CACHE_DIR=.uv/cache uv run python -m otomads.fetch_audio [选项]

选项：
    --track <子串>   只处理标题或作者含该子串的曲目
    --dry-run        只打印计划，不下载不裁剪
    --force          忽略状态，重下重裁
    --offline-ok     检查不到 yt-dlp 新版时也继续（默认：检查失败即中止）
    --jobs N         量响度的并发（默认 8）

流程：依赖检查 → yt-dlp 更新 → 逐条下载 → 裁剪（`-c copy`）→ 顺带量响度 → 汇总。
产物：``<曲库>/<专辑>/<作者> - <标题>.mp3``（运行时唯一被读到的文件），
原件留在 ``<曲库>/.raw/``、状态在 ``<曲库>/.state/<pack>.json``（都在点目录里，
曲库助手的扫描会跳过它们 —— 见 `local_source.scan_library`）。
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

from . import local_source, loudness, packformat as packs, paths as repo

PYPI_URL = "https://pypi.org/pypi/yt-dlp/json"
RAW_DIR = ".raw"
STATE_DIR = ".state"
TMP_DIR = ".state/tmp"
REEXEC_FLAG = "TMC_FETCH_REEXEC"


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
        done = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as error:
        return str(error)
    return None if done.returncode == 0 else f"`ffmpeg -version` 退出码 {done.returncode}"


def ensure_ytdlp(*, offline_ok: bool, reexec: bool) -> str | None:
    """yt-dlp 更新策略（用户口径）：无新版 → 继续；有新版且升级成功 → 继续；升级失败 → 中止。

    升级成功后必须**重启进程**：旧版已经 import 进内存，同进程里换不掉。
    """
    installed = installed_ytdlp()
    if offline_ok:
        print(f"· yt-dlp {installed}（--offline-ok：跳过更新检查）")
        return None
    latest = latest_ytdlp()
    if latest is None:
        return ("无法检查 yt-dlp 更新（离线或网络受限）；本次抓取可能因站点改版而失败 ✗。"
                "确认要跑就加 --offline-ok")
    if version_key(latest) <= version_key(installed):
        print(f"· yt-dlp {installed} 已是最新")
        return None

    print(f"· yt-dlp 有新版本 {latest}（当前 {installed}）→ 升级")
    tools = repo.ROOT / "tools"
    lock = subprocess.run(["uv", "lock", "--upgrade-package", "yt-dlp"], cwd=tools, env=uv_env(),
                          capture_output=True, text=True)
    sync = subprocess.run(["uv", "sync"], cwd=tools, env=uv_env(), capture_output=True, text=True) \
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
    """yt-dlp 下最高音质音频并转成 mp3（成品必须是 mp3：见 docs/packs-audio-v1.md §5）。"""
    import yt_dlp

    raw.parent.mkdir(parents=True, exist_ok=True)
    options = {
        "format": "bestaudio/best",
        "outtmpl": str(raw.with_suffix("")) + ".%(ext)s",
        "quiet": True, "no_warnings": True, "noprogress": True, "retries": 3,
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3",
                            "preferredquality": "0"}],
    }
    with yt_dlp.YoutubeDL(options) as ydl:
        ydl.download([source])
    if not raw.exists():
        others = sorted(path.name for path in raw.parent.glob(raw.stem + ".*"))
        raise RuntimeError(f"下载完成但没得到 {raw.name}（实际文件：{others or '无'}）")


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
               "-ss", f"{start:.3f}", "-i", str(raw)]
    if duration is not None:                 # None = 一直裁到文件结尾
        command += ["-t", f"{duration:.3f}"]
    command += ["-c", "copy", str(tmp)]
    subprocess.run(command, check=True, capture_output=True, text=True)
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
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                    encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="抓取并裁剪曲包音频（docs/packs-audio-v1.md）")
    parser.add_argument("--track", default="", help="只处理标题或作者含该子串的曲目")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划")
    parser.add_argument("--force", action="store_true", help="忽略状态，重下重裁")
    parser.add_argument("--offline-ok", action="store_true", help="检查不到 yt-dlp 新版时也继续")
    parser.add_argument("--jobs", type=int, default=8, help="量响度的并发数")
    parser.add_argument("--config", type=pathlib.Path, default=repo.DEFAULT_CONFIG,
                        help="本地曲库配置（默认本仓库根的 local-source.toml）")
    args = parser.parse_args(argv)

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
          f"待处理 {len(tracks)} 条{'（--dry-run）' if args.dry_run else ''}")

    if not args.dry_run:
        if (problem := ensure_ytdlp(offline_ok=args.offline_ok,
                                    reexec=os.environ.get(REEXEC_FLAG) == "1")) is not None:
            print(f"❌ {problem}", file=sys.stderr)
            return 3
    else:
        print("· --dry-run：跳过 yt-dlp 更新检查")

    states: dict[str, dict] = {}
    outputs: dict[tuple[str, str, str], pathlib.Path] = {}
    changed: dict[str, set[str]] = collections.defaultdict(set)   # pack → 需要重量的曲目 stem
    results: list[dict] = []

    for track in tracks:
        pack_id = track["pack"]
        state_path = library / STATE_DIR / f"{pack_id}.json"
        state = states.setdefault(pack_id, load_state(state_path))
        outcome = process_track(track, library=library, state=state, outputs=outputs, args=args,
                                changed=changed[pack_id])
        results.append(outcome)
        if not args.dry_run and outcome["status"] in {"fetched", "trimmed", "linked"}:
            save_state(state_path, state)

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
                  outputs: dict[tuple[str, str, str], pathlib.Path], args, changed: set[str]) -> dict:
    """一条曲目：下载 → 裁剪 → 成品。返回 `{status, title, detail}`。"""
    title = track["title"]
    out_rel = pathlib.Path(track["album"]) / packs.audio_filename(track)
    out_path = library / out_rel
    key = f'{track["album"]}\u0001{title}'
    entry = state["tracks"].get(key, {})
    source = track.get("source", "")

    try:
        wanted = packs.trim_seconds(track)
    except ValueError as error:
        return {"status": "failed", "title": title, "detail": f"裁剪区间非法：{error}"}

    if not source:
        if out_path.exists():
            return {"status": "skip", "title": title, "detail": "无 source（人工入库），保持原样"}
        return {"status": "missing", "title": title,
                "detail": "没有 source，曲库里也没有这个文件（补 source 或手工放入曲库）"}

    signature = {"source": source, "start": track.get("start_time", ""), "stop": track.get("stop_time", "")}
    raw_path = library / RAW_DIR / f"{packs.source_key(source)}.mp3"
    # 状态里这些字段是**内联**存的（见下方写回），所以这里也按内联比
    fresh = (raw_path.exists() and out_path.exists()
             and entry.get("source") == signature["source"]
             and entry.get("start", "") == signature["start"]
             and entry.get("stop", "") == signature["stop"]
             and entry.get("outHash") == hash_file(out_path))
    if fresh and not args.force:
        return {"status": "skip", "title": title, "detail": "已是目标状态"}

    if args.dry_run:
        action = "下载 + 裁剪" if wanted else "下载"
        if not raw_path.exists():
            return {"status": "dry", "title": title, "detail": f"{action} ← {source}"}
        return {"status": "dry", "title": title, "detail": f"{'裁剪' if wanted else '落成品'}（原件已在）"}

    try:
        # 原件是按 **source** 存的：只要它还在、且这条曲目没换来源，就不重新下载
        # （同一 source 的第二条曲目本来就没有自己的状态，不能因此重下 ✗）
        stale_source = bool(entry.get("source")) and entry["source"] != source
        if args.force or not raw_path.exists() or stale_source:
            download(source, raw_path)
        twin_key = (source, signature["start"], signature["stop"])
        twin = outputs.get(twin_key)
        if twin is not None and twin != out_path and twin.exists() and not args.force:
            link_or_copy(twin, out_path)          # 同一来源同一区间 → 硬链接
            action = "linked"
        else:
            render(raw_path, out_path, wanted, library / TMP_DIR)
            outputs[twin_key] = out_path
            action = "trimmed" if wanted else "fetched"
    except Exception as error:                    # yt-dlp / ffmpeg 的失败都按"这一条失败"处理
        return {"status": "failed", "title": title, "detail": f"{type(error).__name__}: {error}"}

    state["tracks"][key] = {**signature, "raw": str(pathlib.Path(RAW_DIR) / raw_path.name),
                            "out": str(out_rel), "outHash": hash_file(out_path),
                            "ytdlp": installed_ytdlp(), "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%S")}
    changed.add(out_path.stem)
    return {"status": action, "title": title,
            "detail": f"{track.get('start_time', '-')}–{track.get('stop_time', '-')}"
                      if wanted else "整首"}


if __name__ == "__main__":
    raise SystemExit(main())
