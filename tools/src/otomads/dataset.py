"""本仓库自己的数据集：把曲包真源与源注册表编译成 ``dataset/`` 下的 JSON（REFACTOR-PLAN §7.2）。

以前音MAD 那份数据集由主仓库的 ``tmc.build`` 从本仓库的曲包生成；现在**本仓库构建自己的数据集**，
主仓库只负责取用（env ``OTOMADS_DATA_DIR`` / Release 快照，见 §7.2「三仓库独立构建」）。

``python -m otomads.dataset [--out DIR]`` 写出（默认 ``DIR = <仓库根>/dataset``）::

    albums.json       曲包自带专辑（记录 / 键 / 顺序同 tmc.build.build_albums("otomads", …)）
    characters.json   身份无关的角色表：{"key", "music": [曲目 id], "card"?, "covers"?}
    tracks.json       曲目 id → {"album", "title", "extra", "author"?, "authors"?}
    pack-audio.json   音频指纹输入：[专辑, 曲名, start_time, stop_time, source]
    sources.json      本源的音乐源注册表（形状同 tmc.build.build_sources("otomads")）
    index.json        {"schema", "mode", "source": {"repo", "commit", "dirty"}, "counts"}
    loudness/*.json   注册表声明的响度表（源文件不存在就静默跳过；表本身仍提交在本仓库里）

**身份字段不在这份数据里**（契约 ``docs/otomads-separation-v1.md`` §5 S1）：角色记录只有
``key`` + 本模式的曲目（+ 可选 ``card`` / ``covers``）。``name`` / ``order`` / ``searchNames``
的真源仍在主仓库，烘焙时按 ``key`` 合并。曲目 id 的规则 ``<角色 key>_otomad_<NNN>``（三位、
从 001 起、按曲包文件顺序）与 ``tmc.build.build_characters`` 一字不差，主仓库据此把身份拼回去。

**``contentHash`` 也故意不写**：主仓库的口径是
``sha256(json.dumps([characters, albums, pack_audio]))``，而那份 ``characters`` 是**合并身份之后**的
（上面那条 S1）—— 只有消费方手里才有算 hash 的完整输入，所以 hash 由消费方算；这里不写一个
口径不同的假 hash，也不往 ``index.json`` 里塞。

所有 JSON 的字节格式一致：``json.dumps(payload, ensure_ascii=False, indent=1) + "\n"``。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import tomllib

from . import packformat
from . import paths as repo

SCHEMA_VERSION = 2

#: 本模式的 id（index.json 的 ``mode``）。
MODE = "otomads"

#: index.json 的 ``source.repo``：构建它的仓库名（主仓库据此报"这份产物来自哪"）。
REPO_NAME = "touhou-music-cards-otomads-data"


def _dumps(payload) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=1) + "\n"


def load_registry(mode: str = MODE) -> list[dict]:
    """读本模式的音源注册表（``sources/<mode>.toml`` 的 ``[[source]]``）→ 条目列表。"""
    path = repo.find_source_registry(mode)
    if path is None:
        searched = "、".join(repo.shown(root) for root in repo.source_roots())
        raise SystemExit(f"找不到音源注册表 {mode}.toml（找过：{searched}）")
    with open(path, "rb") as fh:
        return tomllib.load(fh)["source"]


def table_url_problem(value: object) -> str | None:
    """源表的地址是否可用？可用返回 ``None``，否则返回一句人话。

    **与主仓库 ``tmc.build.table_url_problem`` 同一口径**（两份实现零 import 依赖，靠这条注释
    与测试盯着）：只有两种合法形态 —— **绝对 http(s) URL**，或**相对路径**（不带前导 ``/``）。
    根绝对路径在"站点挂在子目录"的部署里会打到域名根上 → 404；其它 scheme / ``//host/x`` 前端取不到。
    构建期在这里当场炸，而不是等用户在某个部署形态上发现"一首歌都放不出来"（D131）。
    """
    if not isinstance(value, str) or value.strip() == "":
        return "不能为空"
    if value.startswith("/"):
        return ("不能是根绝对路径（前导 ``/``）：站点部署在子目录时会打到域名根上 → 404。"
                "请写成相对路径（如 ``manifest.json``）或完整 URL（``https://…``）")
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:", value):
        if not re.match(r"^https?://", value, re.IGNORECASE):
            return "只支持 http(s) 绝对 URL，或相对路径（其它 scheme 前端取不到）"
    return None


def build_sources(entries: list[dict]) -> dict:
    """音源注册表条目 → ``sources.json``（形状同 ``tmc.build.build_sources("otomads")``）。

    地址形态在这里就把关（:func:`table_url_problem`）：坏形态构建期直接失败。
    注册表里的 ``loudness``（可选）只是**声明响度表在哪**，不进这个文件；文件由
    :func:`build_dataset` 按它拷贝。
    """
    sources = []
    for entry in entries:
        problem = table_url_problem(entry["table_url"])
        if problem is not None:
            raise SystemExit(
                f"❌ [{MODE}] 音源 {entry['id']} 的 table_url 不合法：{entry['table_url']}\n   {problem}")
        record = {
            "id": entry["id"],
            "label": {"en": entry["label_en"], "zh": entry["label_zh"]},
            "tableUrl": entry["table_url"],
            "kind": entry["kind"],
            "order": entry["order"],
            "enabled": entry["enabled"],
            "proxyable": entry.get("proxyable", False),
            "description": {"en": entry.get("description_en", ""),
                            "zh": entry.get("description_zh", "")},
        }
        # 每个源自己的响度表：路径相对数据集目录，前端按解析到的 sourceId 取表（D130）
        if entry.get("loudness"):
            record["loudnessUrl"] = entry["loudness"]
        sources.append(record)
    sources.sort(key=lambda source: source["order"])
    return {"schema": SCHEMA_VERSION, "sources": sources}


def build_records(snapshot: dict) -> list[dict]:
    """曲包快照（:func:`packformat.pack_snapshot`）→ 本仓库的角色记录（**带曲目 id**）。

    每条：``{"key", "music": [{"id", "album", "title", "extra", "author"?, "authors"?}],
    "card"?, "covers"?}``。角色顺序 = 快照顺序 = 曲包文件顺序（packformat 给的，保持稳定）；
    曲目 id = ``<角色 key>_otomad_<NNN>``（三位、从 001 起、按曲包文件顺序）—— 与主仓库
    ``tmc.build.build_characters`` 同一条规则（§11.1）。

    ``music`` 条目的形状来自 ``packformat.music_entry``（与主仓库 ``tmc.build.merge_characters``
    合并出来的运行时曲目逐字一致；D175 起由两侧 ``test_vectors.py`` 的共享向量交叉守）：
    第 4 位是作者整串、第 5 位是多作者数组，**写了才有那一位**。
    ``card`` / ``covers`` 是"有才覆盖"（D137 / D153）：没写的角色记录里就没有那个键。
    """
    records: list[dict] = []
    for char in snapshot["characters"]:
        key = char["key"]
        music = []
        for index, entry in enumerate(char["music"], start=1):
            item = {"id": f"{key}_otomad_{index:03d}", "album": entry[0],
                    "title": entry[1], "extra": entry[2]}
            if len(entry) > 3:
                item["author"] = entry[3]
            if len(entry) > 4:
                item["authors"] = entry[4]
            music.append(item)
        record = {"key": key, "music": music}
        if char.get("card"):
            record["card"] = list(char["card"])
        if char.get("covers"):
            record["covers"] = list(char["covers"])
        records.append(record)
    return records


def build_characters(records: list[dict]) -> dict:
    """角色记录 → ``characters.json``：``music`` 只留**曲目 id**（曲目信息在 tracks.json）。

    **身份无关是刻意的**：不写 ``name`` / ``order`` / ``searchNames``（契约 §5 S1），
    主仓库烘焙时按 ``key`` 合并。
    """
    return {
        "schema": SCHEMA_VERSION,
        "characters": [
            {"key": record["key"], "music": [entry["id"] for entry in record["music"]],
             **({"card": list(record["card"])} if record.get("card") else {}),
             **({"covers": list(record["covers"])} if record.get("covers") else {})}
            for record in records
        ],
    }


def build_tracks(records: list[dict]) -> dict:
    """角色记录 → ``tracks.json``（``{曲目 id: {album, title, extra, author?, authors?}}``）。

    键的顺序与 ``tmc.build.build_outputs`` 里那份逐字一致：先 album / title / extra，
    再可选 author / authors（packformat 写 ``authors`` 时两样都会置）。
    """
    tracks = {entry["id"]: {k: entry[k] for k in ("album", "title", "extra") if k in entry}
              | ({k: entry[k] for k in ("author", "authors") if k in entry})
              for record in records for entry in record["music"]}
    return {"schema": SCHEMA_VERSION, "tracks": tracks}


def build_albums(pack_albums: list[dict]) -> dict:
    """曲包自带的专辑 → ``albums.json``（记录 / 键 / 顺序同 ``tmc.build.build_albums("otomads", …)``）。"""
    albums: list[dict] = []
    for entry in pack_albums:
        albums.append({k: entry[k] for k in ("key", "name", "kind", "pack", "order") if k in entry}
                      | ({"showAlbumName": entry["showAlbumName"]} if "showAlbumName" in entry else {}))
    albums.sort(key=lambda album: album["order"])
    return {"schema": SCHEMA_VERSION, "albums": albums}


def pack_audio_entries(pack_tracks: list[dict]) -> list[list[str]]:
    """曲包音频的**指纹行**：``[专辑, 曲名, start_time, stop_time, source]``（按值排序，稳定）。

    **必须与主仓库 ``tmc.packs.audio_descriptors`` 逐字一致**：主仓库把它并进 ``contentHash``
    （``sha256(json.dumps([characters, albums, pack_audio]))``，契约 ``docs/packs-audio-v1.md`` §6），
    于是"两端音频来源 / 裁剪不同"在联机**握手期**就被拒。两个仓库零 import 依赖，口径靠两边
    各自盯着 —— 改动这里必须同步改主仓库那个同名函数。
    """
    rows = [[track["album"], track["title"], track.get("start_time", ""), track.get("stop_time", ""),
             track.get("source", "")] for track in pack_tracks]
    return sorted(rows)


def build_pack_audio(pack_tracks: list[dict]) -> dict:
    """``tmc.packs.audio_descriptors(pack_tracks)`` 的落盘形状：``{"schema": 2, "entries": […]}``。"""
    return {"schema": SCHEMA_VERSION, "entries": pack_audio_entries(pack_tracks)}


def git_source(root: pathlib.Path | None = None) -> dict:
    """本仓库现在的版本：``{"repo", "commit", "dirty"}``（index.json 的 ``source``）。

    ``commit`` = ``git rev-parse HEAD``；``dirty`` = 工作树里有没有未提交的改动
    （``git status --porcelain`` 非空，含未跟踪但**未被 gitignore** 的文件）。
    **不写时间戳**：每次生成字节都不同会破坏可复现性（REFACTOR-PLAN §7.2）。
    ``dataset/`` 已 gitignore ⇒ 产物本身不会把工作树弄脏。
    """
    root = repo.ROOT if root is None else root

    def run(*args: str) -> str:
        try:
            done = subprocess.run(["git", "-C", str(root), *args],
                                  capture_output=True, text=True, check=True)
        except (OSError, subprocess.CalledProcessError) as error:
            raise SystemExit(f"[FAIL] 取不到本仓库的 git 版本：{error}") from None
        return done.stdout

    return {
        "repo": REPO_NAME,
        "commit": run("rev-parse", "HEAD").strip(),
        "dirty": bool(run("status", "--porcelain").strip()),
    }


def counts_of(characters: dict, albums: dict, tracks: dict) -> dict:
    """``index.json`` 的计数（口径同主仓库 ``tmc.build.build_index``）。"""
    chars = characters["characters"]
    return {
        "characters": len(chars),
        "albums": len(albums["albums"]),
        "trackEntries": sum(len(char["music"]) for char in chars),
        "distinctTracks": len(tracks["tracks"]),
    }


def build_index(counts: dict, source: dict) -> dict:
    """``index.json``：``{schema, mode, source, counts}``。

    **没有 ``contentHash``**：它算的是"合并身份之后"的 characters（见模块 docstring），
    输入只有消费方手里才有 —— 由消费方算，不在这里写一个口径不同的假 hash。
    """
    return {"schema": SCHEMA_VERSION, "mode": MODE, "source": dict(source), "counts": dict(counts)}


def build_dataset(out_dir: pathlib.Path) -> dict[str, dict]:
    """构建全部文件 → 返回 ``{文件名: 载荷}``（含 index.json 那份），并落盘到 ``out_dir``。

    读的是**本仓库**的曲包真源（``packformat.load_packs``）与源注册表；响度表按注册表声明的
    路径**按字节拷贝**（源文件不存在就静默跳过 —— 它不是构建的必需输入）。
    """
    _packs, pack_albums, pack_tracks, pack_cards, pack_covers = packformat.load_packs()
    snapshot = packformat.pack_snapshot(pack_albums, pack_tracks, pack_cards, pack_covers)
    records = build_records(snapshot)

    registry = load_registry()
    characters = build_characters(records)
    tracks = build_tracks(records)
    albums = build_albums(snapshot["albums"])
    payloads: dict[str, dict] = {
        "albums.json": albums,
        "characters.json": characters,
        "tracks.json": tracks,
        "pack-audio.json": build_pack_audio(pack_tracks),
        "sources.json": build_sources(registry),
    }
    payloads["index.json"] = build_index(counts_of(characters, albums, tracks), git_source())

    out_dir = pathlib.Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, payload in payloads.items():
        (out_dir / name).write_text(_dumps(payload), encoding="utf-8")
    _copy_loudness(out_dir, registry)
    return payloads


def _copy_loudness(out_dir: pathlib.Path, entries: list[dict]) -> None:
    """注册表声明的响度表 → ``out_dir/<声明的相对路径>``（字节拷贝；源文件不在就跳过）。

    路径口径同主仓库 ``tmc.build.loudness_tables``：注册表里的 ``loudness`` 相对**仓库根**。
    """
    for entry in entries:
        rel = entry.get("loudness")
        if not rel:
            continue
        origin = repo.DATA / rel
        if not origin.exists():
            continue
        target = out_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(origin.read_bytes())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m otomads.dataset",
        description="构建本仓库自己的音MAD 数据集（曲包 + 源注册表 → dataset/*.json）")
    parser.add_argument("--out", type=pathlib.Path, default=repo.ROOT / "dataset",
                        help="输出目录（默认 <仓库根>/dataset）")
    args = parser.parse_args(argv)

    payloads = build_dataset(args.out)
    counts = payloads["index.json"]["counts"]
    print(f"写出 {len(payloads)} 个 JSON + 响度表 → {repo.shown(pathlib.Path(args.out))}："
          f"{counts['characters']} 角色 / {counts['albums']} 专辑 / {counts['distinctTracks']} 曲")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
