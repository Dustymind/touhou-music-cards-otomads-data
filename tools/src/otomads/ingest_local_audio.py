"""把 `.music/incoming/` 里的本地音频转成 320k mp3 并放进 `.music/otomads/`。

文件名规则：`<作者> - <标题>.<扩展名>`（与 yt-dlp 那批保持一致），例如
`y的自然对数 - 对了 向北邮出发吧.wav` → `y的自然对数 - 对了 向北邮出发吧.mp3` ✓

用法（在数据仓库根）：uv run python -m otomads.ingest_local_audio [--dry-run]
"""
from __future__ import annotations
import argparse, pathlib, subprocess, sys

ROOT = pathlib.Path(__file__).resolve().parents[3]
INCOMING = ROOT / ".music" / "incoming"
DEST = ROOT / ".music" / "otomads"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    DEST.mkdir(parents=True, exist_ok=True)
    sources = sorted(p for p in INCOMING.glob("*")
                     if p.suffix.lower() in {".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".mp3"})
    if not sources:
        print(f"{INCOMING} 里没有待转文件（把音频放进去，文件名用 `<作者> - <标题>.wav`）")
        return 0
    failed = 0
    for source in sources:
        stem = source.stem
        if " - " not in stem:
            print(f"✗ 文件名不符合 `<作者> - <标题>` 规则：{source.name}")
            failed += 1
            continue
        target = DEST / f"{stem}.mp3"
        print(f"{source.name} → {target.name}")
        if args.dry_run:
            continue
        result = subprocess.run([
            "ffmpeg", "-y", "-loglevel", "error", "-nostdin", "-i", str(source),
            "-vn", "-codec:a", "libmp3lame", "-b:a", "320k", str(target),
        ], stdin=subprocess.DEVNULL)          # 别让 ffmpeg 去接管用户的终端（见 D132 追加）
        if result.returncode != 0 or not target.exists():
            print(f"   ✗ 转码失败（{result.returncode}）")
            failed += 1
            continue
        print(f"   ✓ {target.stat().st_size // 1024} KB（320k）")
        source.unlink()      # 转完就删，避免下次重复转
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
