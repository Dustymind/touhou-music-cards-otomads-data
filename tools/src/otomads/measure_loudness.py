"""量本仓库曲库每首的响度，写进**本源**的响度表（`loudness/<曲包>.json`，路径由源注册表声明）。

核心在 `otomads.loudness`（抓取/裁剪流程 `otomads.fetch_audio` 也会调用它；契约见主仓库
`docs/packs-audio-v1.md`）；这个脚本只是命令行入口。

用法（在数据仓库根）：
    uv run python -m otomads.measure_loudness                          # 默认量 .music/otomads
    uv run python -m otomads.measure_loudness /mnt/music/otomads --jobs 4
    uv run python -m otomads.measure_loudness --out loudness/otomads.json
"""
from __future__ import annotations

import argparse
import pathlib

from . import loudness, packformat, paths


def main() -> int:
    parser = argparse.ArgumentParser(description="量曲库响度 → 本源的 loudness 表")
    parser.add_argument("directory", nargs="?", default=str(paths.ROOT / ".music" / "otomads"),
                        help="曲库目录（默认 .music/otomads）")
    parser.add_argument("--pack", default="otomads", help="曲包 id（决定响度表路径）")
    parser.add_argument("--out", type=pathlib.Path, help="覆盖输出路径（默认读源注册表的 loudness 键）")
    parser.add_argument("--jobs", type=int, default=8)
    args = parser.parse_args()

    output = args.out or packformat.loudness_path(args.pack)
    if output is None:
        print(f"❌ 源注册表 sources/{args.pack}.toml 没声明 loudness；用 --out 指定输出路径")
        return 2
    summary = loudness.measure_library(pathlib.Path(args.directory), output=output, jobs=args.jobs)
    for line in loudness.describe(summary):
        print(line)
    return 0 if summary["measured"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
