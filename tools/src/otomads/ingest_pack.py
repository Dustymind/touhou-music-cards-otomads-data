"""把解析好的录入行追加进**曲包的角色文件**（``packs/<曲包 id>/<角色 key>.toml``）。

布局与口径见本仓库 ``README.ai.MD`` 与主仓库契约 ``docs/packs-audio-v1.md``：
一个曲包 = 一份清单（``[pack]`` + ``[[album]]``）+ 一角色一份曲目文件。这条命令只**写数据**，
不跑主仓库的 ``tmc.build`` / ``tmc.validate``（那两个是独立命令）。

用法::

    uv run python -m otomads.parse_ingest_rows rows.txt    # → tools/ingest_rows_<日期>.json
    uv run python -m otomads.ingest_pack \\
        --pack otomads --rows tools/ingest_rows_2026-09b.json

行的形状与 ``parse_ingest_rows.py`` 的产物一致：``{bv | source, title, author, character}``；
``bv`` 会拼成 ``https://www.bilibili.com/video/<bv>/`` 当 ``source``，已经给了完整 ``source`` 就用它。
**同 (专辑, 曲名) 已在该角色文件里 ⇒ 跳过**（重复跑同一条命令不会写第二遍），
角色 key 必须存在于本仓库的 ``characters.toml`` 清单（写错一个 key 会被静默错挂，所以直接报错）。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import tomllib

from . import paths as repo

#: `bv` → B 站视频地址（曲包不写死站点：行里给了完整 `source` 就用它）
BILIBILI = "https://www.bilibili.com/video/{bv}/"

#: `[[track]]` 的键序（固定，diff 才稳定）：与 `packs.TRACK_KEYS` 一致
FIELD_ORDER = ("album", "author", "authors", "title", "extra", "source", "start_time", "stop_time")

#: 角色文件的开头注释（新文件才有）；路径按清单实际所在的根目录算
FILE_HEADER = ("# 音MAD 曲包（{pack}）：`{character}` 的曲目。\n"
               "# 清单与口径见 `{manifest}` 与 `{readme}`。\n")


def toml_str(value: str) -> str:
    """TOML 基本字符串（转义反斜杠与引号；标题里这两种字符都出现过）。"""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def track_block(track: dict) -> str:
    """一条 ``[[track]]``：键序固定，空的键不写。字符串数组（`authors`）写成 TOML 数组。"""
    lines = ["[[track]]"]
    for field in FIELD_ORDER:
        value = track.get(field)
        if value in (None, "", []):
            continue
        if isinstance(value, list):
            lines.append(f"{field} = [{', '.join(toml_str(str(item)) for item in value)}]")
        else:
            lines.append(f"{field} = {toml_str(str(value))}")
    return "\n".join(lines)


def authors_of(row: dict) -> list[str]:
    """录入行里的多作者：``authors`` 是数组就用它（校验非空、去空白）；否则返回空列表。

    行的形状见 ``parse_ingest_rows.py`` 的产物：``{source|bv, title, author, character}``；
    多作者时把那一项写成数组即可（`"author": ["甲", "乙"]`）。
    """
    many = row.get("authors")
    if many is None:
        return []
    if not isinstance(many, list) or not many:
        raise SystemExit(f"录入行里的 authors 必须是非空数组：{json.dumps(row, ensure_ascii=False)}")
    cleaned = [str(name).strip() for name in many]
    if not all(cleaned):
        raise SystemExit(f"录入行里的 authors 不能有空项：{json.dumps(row, ensure_ascii=False)}")
    return cleaned


def source_of(row: dict) -> str:
    """行的音频来源：优先完整 `source`，否则由 `bv` 拼 B 站地址，都没有返回空串。"""
    source = str(row.get("source") or "").strip()
    if source:
        return source
    bv = str(row.get("bv") or "").strip()
    return BILIBILI.format(bv=bv) if bv else ""


def character_keys() -> set[str]:
    """本仓库 `characters.toml` 清单里的角色 key（由主仓库 `pnpm data:roster` 生成）。"""
    path = repo.characters_file()
    if not path.exists():
        raise SystemExit(f"缺少角色清单：{repo.shown(path)}（先在主仓库跑 `pnpm data:roster`）")
    roster = tomllib.loads(path.read_text(encoding="utf-8"))
    return {entry["key"] for entry in roster.get("character", [])}


def existing_titles(path: pathlib.Path) -> set[tuple[str, str]]:
    """角色文件里已有的 ``(专辑, 曲名)``（幂等用）。"""
    if not path.exists():
        return set()
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return {(track["album"], track["title"]) for track in data.get("track", [])}


def pack_manifest(pack: str) -> pathlib.Path:
    """曲包清单的路径：本仓库 `packs/<pack>.toml`。"""
    path = repo.find_pack_manifest(pack)
    if path is None:
        raise SystemExit(f"找不到曲包清单：packs/{pack}.toml")
    return path


def contract_doc(manifest: pathlib.Path) -> pathlib.Path:
    """曲包契约文档：本仓库根的 ``README.ai.MD``（新文件的注释指向它；退回 ``README.md``）。"""
    for candidate in (repo.ROOT / "README.ai.MD", repo.ROOT / "README.md",
                      manifest.parent / "README.md"):
        if candidate.exists():
            return candidate
    return repo.ROOT / "README.ai.MD"


def default_album(pack: str) -> str:
    """清单里只声明了一张专辑 → 用它；否则必须显式 ``--album``。"""
    path = pack_manifest(pack)
    albums = tomllib.loads(path.read_text(encoding="utf-8")).get("album", [])
    names = [entry["name"] for entry in albums]
    if len(names) != 1:
        raise SystemExit(f"曲包 {pack} 有 {len(names)} 张专辑（{names}）：请用 --album 指定")
    return names[0]


def append_rows(pack: str, rows: list[dict], album: str | None = None,
                dry_run: bool = False) -> dict[str, dict[str, int]]:
    """按角色把行追加进 ``packs/<pack>/<角色 key>.toml``；返回每个角色的统计。

    只追加、不改写已有内容（保住人工写的注释与顺序）；重复的 ``(专辑, 曲名)`` 跳过。
    """
    known = character_keys()
    manifest = pack_manifest(pack)
    target_album = album or default_album(pack)
    directory = manifest.parent / pack
    summary: dict[str, dict[str, int]] = {}

    for row in rows:
        character = str(row.get("character") or "").strip()
        title = str(row.get("title") or "").strip()
        authors = authors_of(row)
        if not character or not title:
            raise SystemExit(f"录入行缺少 character / title：{json.dumps(row, ensure_ascii=False)}")
        if character not in known:
            raise SystemExit(f"曲包 {pack}：角色 key 不存在 → {character}（{title[:24]}）")
        source = source_of(row)
        if source and not source.lower().startswith(("http://", "https://")):
            raise SystemExit(f"{character} / {title[:24]}：source 必须是 http(s) 链接 → {source!r}")

        counts = summary.setdefault(character, {"added": 0, "skipped": 0})
        path = directory / f"{character}.toml"
        if (target_album, title) in existing_titles(path):
            counts["skipped"] += 1
            continue

        track = {"album": target_album, "title": title,
                 "extra": str(row.get("extra") or "角色曲"), "source": source}
        # 行里给了 `authors` 数组就写数组（D135），否则写老写法的整串 `author`
        if authors:
            track["authors"] = authors
        else:
            track["author"] = str(row.get("author") or "").strip()
        block = track_block(track)
        if dry_run:
            counts["added"] += 1
            continue

        directory.mkdir(parents=True, exist_ok=True)
        if path.exists():
            body = path.read_text(encoding="utf-8").rstrip("\n")
            path.write_text(f"{body}\n\n{block}\n", encoding="utf-8")
        else:
            header = FILE_HEADER.format(pack=pack, character=character,
                                        manifest=repo.shown(manifest),
                                        readme=repo.shown(contract_doc(manifest)))
            path.write_text(f"{header}\nkey = {toml_str(character)}\n\n{block}\n", encoding="utf-8")
        counts["added"] += 1
    return summary


def load_rows(path: pathlib.Path) -> list[dict]:
    """读 ``parse_ingest_rows.py`` 的产物（数组；也接受 ``{"rows": [...]}``）。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data.get("rows") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise SystemExit(f"{path}: 期望一个行数组（parse_ingest_rows.py 的产物）")
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把录入行追加进曲包的角色文件（packs/<id>/<key>.toml）")
    parser.add_argument("--pack", required=True, help="曲包 id（= 清单 <id>.toml 的文件名）")
    parser.add_argument("--rows", required=True, type=pathlib.Path, help="parse_ingest_rows.py 产出的 JSON")
    parser.add_argument("--album", help="专辑名（清单只声明一张时可不填）")
    parser.add_argument("--dry-run", action="store_true", help="只打印将要写什么")
    args = parser.parse_args(argv)

    rows = load_rows(args.rows)
    summary = append_rows(args.pack, rows, args.album, args.dry_run)
    pack_dir = pack_manifest(args.pack).parent / args.pack
    added = sum(count["added"] for count in summary.values())
    skipped = sum(count["skipped"] for count in summary.values())
    for character in sorted(summary):
        count = summary[character]
        print(f"  {character:<26} +{count['added']}  跳过 {count['skipped']}")
    verb = "将写入" if args.dry_run else "已写入"
    print(f"{verb} {added} 条 / 跳过重复 {skipped} 条 → {repo.shown(pack_dir)}/"
          f"（接着在主仓库跑 pnpm data:build 与 pnpm data:validate）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
