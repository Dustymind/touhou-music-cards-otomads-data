"""逐曲响度均衡的系数表（D102）→ ``本源的 loudness/<曲包>.json``。

为什么只衰减不放大：``HTMLMediaElement.volume`` 上限是 1 ✗，放大要么削波要么得挂 Web Audio 节点；
以"最轻的一首"为目标、其余按比例**往下压**，就能在不改音色的前提下让每首听感一样响 ✓。

核心抽到这里是为了让**抓取/裁剪流程（``otomads.fetch_audio``）能直接调用**：
裁过的曲目必须先失效缓存再重量，否则会沿用裁剪前的 dB ✗（契约见 ``docs/packs-audio-v1.md``）。
命令行入口仍是 ``tools/measure_loudness.py``（薄封装，行为不变）。
"""
from __future__ import annotations

import concurrent.futures
import json
import pathlib
import re
import statistics
import subprocess
from collections.abc import Callable, Iterable, Mapping

from . import paths as repo

#: 只衰减不放大 ✓；下限 0.6（约 −4.4 dB）—— 用"最轻的一首"当目标会把绝大多数曲目压到地板 ✗
MIN_GAIN, MAX_GAIN = 0.6, 1.0

#: ffmpeg `volumedetect` 的输出里那一行
MEAN_RE = re.compile(r"mean_volume: ([-\d.]+) dB")


def mean_volume_db(path: pathlib.Path) -> float | None:
    """量一首的平均电平（dB）；量不到返回 `None`。

    `stdin=DEVNULL` **不能省**：ffmpeg 只要看到 stdin 是终端就会去接管它（`-nostdin` 也拦不住
    "stdin 是 tty" 这件事），跑完可能把终端留在不回显的状态 ✗ —— 86 首就是 86 次机会。
    实测（WSL2 上用户报的"抓取跑完终端不回显"，见 D132 追加）：Linux 沙箱里复现不出来，
    所以这条按规范写死，不去赌平台。
    """
    out = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(path), "-af", "volumedetect",
                          "-f", "null", "-"], capture_output=True, text=True,
                         stdin=subprocess.DEVNULL).stderr
    matched = MEAN_RE.search(out)
    return float(matched.group(1)) if matched else None


def measure_library(
    directories: "pathlib.Path | str | Iterable[pathlib.Path | str]",
    *,
    output: pathlib.Path,
    jobs: int = 8,
    reset: Iterable[str] = (),
    measure: Callable[[pathlib.Path], float | None] = mean_volume_db,
) -> dict:
    """量若干目录下的 ``*.mp3`` → 写 ``output``，返回摘要（也被 ``fetch_audio`` 调用）。

    - `directories` 传一个路径或一组路径（曲包可以有多个专辑目录；所有曲子共用一个目标电平）；
    - ``reset``：这些**文件名 stem** 的缓存先删掉（裁剪/换源后必须重量的曲目）；
    - 顺带清掉"文件已不在"的旧键 —— 老实现只增不减，删过的曲子会一直留在表里 ✗；
    - 目标 = **中位数**（抗离群 ✓），只衰减不放大，系数夹在 ``[MIN_GAIN, MAX_GAIN]``。
    """
    roots = ([directories] if isinstance(directories, (str, pathlib.Path))
             else list(directories))
    # 与 `local_source.scan_library` / `stage_media.audio_files` 同一套口径：
    # 只看 `paths.AUDIO_EXTENSIONS`、**跳过点开头的文件**（`glob("*.mp3")` 会收 `.hidden.mp3`，
    # 那是 pathlib 与 shell 的著名差异）。
    files = sorted(
        path for root in roots if pathlib.Path(root).is_dir()
        for path in pathlib.Path(root).iterdir()
        if path.is_file() and not path.name.startswith(".")
        and path.name.lower().endswith(repo.AUDIO_EXTENSIONS)
    )
    cache: dict[str, float] = {}
    if output.exists():
        cache = json.loads(output.read_text(encoding="utf-8")).get("measuredDb", {})

    reset_keys = set(reset)
    present = {path.stem for path in files}
    # 该丢的键 = 本次要求重置的 + 文件已经不在的（老实现只增不减 ✗）
    drop = reset_keys | (set(cache) - present)
    dropped = sorted(drop)
    for key in drop:
        cache.pop(key, None)

    todo = [path for path in files if path.stem not in cache]
    if todo:
        with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
            for path, db in pool.map(lambda item: (item, measure(item)), todo):
                if db is not None:
                    cache[path.stem] = db

    measured = [(path, cache[path.stem]) for path in files if path.stem in cache]
    if not measured:
        return {"measured": 0, "target": 0.0, "gains": {}, "dropped": dropped, "output": output}

    target = statistics.median(db for _path, db in measured)
    gains = {
        path.stem: round(min(MAX_GAIN, max(MIN_GAIN, 10 ** ((target - db) / 20))), 4)
        for path, db in measured
    }
    payload = {"schema": 1, "targetDb": round(target, 2), "measuredDb": dict(sorted(cache.items())),
               "gains": dict(sorted(gains.items()))}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return {"measured": len(measured), "target": target, "gains": gains, "dropped": dropped,
            "output": output}


def describe(summary: dict) -> list[str]:
    """摘要 → 几行给人看的输出（脚本与抓取命令共用同一套措辞）。"""
    if not summary["measured"]:
        return ["没找到 mp3"]
    gains = summary["gains"]
    values = sorted(gains.values())
    lines = [
        f"量了 {summary['measured']} 首 | 目标（中位）= {summary['target']:.1f} dB | "
        f"系数 {values[0]:.3f}–{values[-1]:.3f}（中位 {statistics.median(values):.3f}）",
        "压缩最多的 3 首:",
    ]
    for name in sorted(gains, key=lambda key: gains[key])[:3]:
        lines.append(f"  {gains[name]:.3f}  {name[:52]}")
    lines.append(f"→ {summary['output']}")
    return lines


# ------------------------------------------------------------------ 表与曲目的对应关系（D135 追加）

#: 对应关系只提示、不报错 —— "音频还没抓"（曲目写了 source 但没跑 fetch_audio）
#: 与"曲库只放了部分"都是**合法**状态，不该让命令失败。
COVERAGE_SAMPLE = 5


def coverage_report(tracks: "list[dict]", gains: "Mapping[str, float]",
                    stem_of: "Callable[[dict], str]") -> dict:
    """曲目 × 响度表的对应关系 → 两类问题（都只是提示）。

    - ``missing``：曲目里**本该有增益、表里却没有**的 stem（音频不在曲库 / 还没抓）；
    - ``stale``：表里有、**任何曲目都算不出来**的键（改了作者/曲名后没重跑，或拿别的曲库量过）。

    为什么要有这条：表是**提交进仓库**的（生成它需要音频，而音频不分发），
    所以"表与曲目对不上"只能靠代码发现。实测踩过一次 —— 拿别的曲库跑 `fetch_audio`
    把表覆盖掉了，增益全错，而且是**悄无声息**的（听感差别，不是报错）。
    """
    expected = [stem_of(track) for track in tracks]
    known = set(expected)
    return {
        "tracks": len(expected),
        "covered": sum(1 for stem in expected if stem in gains),
        "missing": sorted({stem for stem in expected if stem not in gains}),
        "stale": sorted(set(gains) - known),
    }


def describe_coverage(report: dict) -> list[str]:
    """对应关系 → 几行警告（没问题就返回空列表，不占输出）。"""
    if not report:
        return []
    lines: list[str] = []
    missing = report.get("missing") or []
    if missing:
        lines.append(f"⚠️ 响度表缺 {len(missing)}/{report['tracks']} 首"
                     f"（音频不在曲库，或这几首还没抓）：")
        for stem in missing[:COVERAGE_SAMPLE]:
            lines.append(f"  · {stem[:60]}")
        if len(missing) > COVERAGE_SAMPLE:
            lines.append(f"  · …还有 {len(missing) - COVERAGE_SAMPLE} 首")
    stale = report.get("stale") or []
    if stale:
        lines.append(f"⚠️ 响度表里有 {len(stale)} 个键对不上任何曲目"
                     f"（改了作者/曲名？或拿别的曲库量过）：")
        for stem in stale[:COVERAGE_SAMPLE]:
            lines.append(f"  · {stem[:60]}")
        if len(stale) > COVERAGE_SAMPLE:
            lines.append(f"  · …还有 {len(stale) - COVERAGE_SAMPLE} 个")
    return lines
