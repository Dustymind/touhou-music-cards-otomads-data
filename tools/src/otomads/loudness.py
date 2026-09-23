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
from collections.abc import Callable, Iterable

#: 只衰减不放大 ✓；下限 0.6（约 −4.4 dB）—— 用"最轻的一首"当目标会把绝大多数曲目压到地板 ✗
MIN_GAIN, MAX_GAIN = 0.6, 1.0

#: ffmpeg `volumedetect` 的输出里那一行
MEAN_RE = re.compile(r"mean_volume: ([-\d.]+) dB")


def mean_volume_db(path: pathlib.Path) -> float | None:
    """量一首的平均电平（dB）；量不到返回 `None`。"""
    out = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(path), "-af", "volumedetect",
                          "-f", "null", "-"], capture_output=True, text=True).stderr
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
    files = sorted(path for root in roots for path in pathlib.Path(root).glob("*.mp3")
                   if path.is_file())
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
