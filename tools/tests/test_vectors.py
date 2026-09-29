"""跨仓库共享测试向量（REFACTOR-PLAN v2 §18 Q1）：**写入侧**（本仓库）那一份。

Q1 的落地口径是**共享向量为主**：每一个跨仓库的产物形状，两侧各存一份**字面量**（零 import
依赖），漂移时两侧测试同时报红。读取侧那份在主仓库的 ``tools/tests/test_vectors.py``；这里存的是
**写入侧**那一份 —— 本仓库真正写出去、由主仓库 / 前端消费的东西：

    本仓库写 → 主仓库读   ``dataset/*.json``（``otomads.dataset.build_dataset``）
    本仓库写 → 前端读     本机助手清单 ``manifest.json``（``otomads.local_source.build_manifest``）

字面量必须与主仓库那份**逐字一致**：改形状要同时改两边（数据集那份还多一份在同级的自定义数据
仓库里）。向量只写**键集合**（必填 / 可选）与**行的元数**，不写值域：值域由 ``dataset`` /
``local_source`` 各自的单测守。

断言用的是**本仓库真实产出的载荷**（真曲包 ``packs/`` + 真源注册表 ``sources/otomads.toml``），
不是另造一份与实现无关的假数据 —— 实现悄悄改了形状，这套向量才会当场红。
"""
from __future__ import annotations

import pytest

from otomads import dataset, local_source, packformat

# ---------------------------------------------------------------- 共享字面量（与主仓库逐字一致）

DATASET_INDEX = {"required": ["schema", "mode", "source", "counts"],
                 "types": {"schema": int, "mode": str, "source": dict, "counts": dict}}
DATASET_SOURCE = {"required": ["repo", "commit"], "optional": ["dirty"]}
DATASET_COUNTS = {"required": ["characters", "albums", "trackEntries", "distinctTracks"],
                   "types": {"characters": int, "albums": int, "trackEntries": int,
                             "distinctTracks": int}}
DATASET_CHARACTERS = {"required": ["schema", "characters"],
                       "types": {"schema": int, "characters": list}}
DATASET_CHARACTER = {"required": ["key", "music"], "optional": ["card", "covers"],
                      "types": {"key": str, "music": list, "card": list, "covers": list}}
DATASET_ALBUMS = {"required": ["schema", "albums"], "types": {"schema": int, "albums": list}}
DATASET_ALBUM = {"required": ["key", "name", "kind", "pack", "order"],
                 "optional": ["showAlbumName", "work"],
                 "types": {"key": str, "name": str, "kind": str, "pack": str, "order": int,
                           "showAlbumName": bool, "work": str}}
DATASET_TRACKS = {"required": ["schema", "tracks"], "types": {"schema": int, "tracks": dict}}
DATASET_TRACK = {"required": ["album", "title", "extra"], "optional": ["author", "authors"],
                 "types": {"album": str, "title": str, "extra": str, "author": str,
                           "authors": list}}
DATASET_SOURCES = {"required": ["schema", "sources"], "types": {"schema": int, "sources": list}}
DATASET_SOURCE_RECORD = {"required": ["id", "label", "tableUrl", "kind", "order", "enabled",
                                       "proxyable", "description"], "optional": ["loudnessUrl"],
                          "types": {"id": str, "label": dict, "tableUrl": str, "kind": str,
                                    "order": int, "enabled": bool, "proxyable": bool,
                                    "description": dict, "loudnessUrl": str}}
DATASET_PACK_AUDIO = {"required": ["schema", "entries"],
                       "types": {"schema": int, "entries": list}}
DATASET_PACK_AUDIO_ROW = 5        # [专辑, 曲名, start_time, stop_time, source]（packs-audio-v1 §6）
ASSISTANT_MANIFEST = {"required": ["schema", "pack", "revision", "tracks"]}
ASSISTANT_ROW = 4                 # [专辑, 曲名, 地址, 版本]；版本一律内容哈希（§2.6）


def spec_problem(where: str, payload, spec: dict) -> list[str]:
    """payload 对不上向量 ⇒ 说清缺了谁、多出谁、谁的类型不对。

    多出来的键也必须登记 —— 悄悄加字段同样是形状漂移，两侧测试要一起红。
    """
    got = set(payload)
    problems = []
    missing = [key for key in spec["required"] if key not in got]
    if missing:
        problems.append(f"{where} 缺键 {missing}")
    extra = sorted(got - set(spec["required"]) - set(spec.get("optional", [])))
    if extra:
        problems.append(f"{where} 多出没登记的键 {extra}（形状变了就把向量一起改）")
    for key, want in (spec.get("types") or {}).items():
        if key in payload and not isinstance(payload[key], want):
            problems.append(f"{where}.{key} 的类型不是 {want.__name__}：{type(payload[key]).__name__}")
    return problems


def rows_problem(where: str, rows, width: int) -> list[str]:
    problems = []
    for index, row in enumerate(rows):
        if not isinstance(row, list) or len(row) != width:
            problems.append(f"{where} 第 {index + 1} 行的元数不是 {width}：{row!r}")
        elif not all(isinstance(cell, str) for cell in row):
            problems.append(f"{where} 第 {index + 1} 行有非字符串：{row!r}")
    return problems


# ---------------------------------------------------------------- 数据集（本仓库写、主仓库读）

@pytest.fixture()
def built(tmp_path):
    """本仓库真源（真曲包 + 真源注册表）→ ``build_dataset`` 的 ``(输出目录, 载荷)``。

    **不是**另造的假数据：形状断言要盯的就是本仓库真正写出去的那一份。
    """
    out = tmp_path / "dataset"
    return out, dataset.build_dataset(out)


def test_built_dataset_follows_the_shared_vectors(built):
    """``build_dataset`` 的六件逐键对向量 —— 主仓库读到的东西在这里被钉住。"""
    _out, payloads = built
    problems: list[str] = []

    index = payloads["index.json"]
    problems += spec_problem("dataset/index.json", index, DATASET_INDEX)
    problems += spec_problem("dataset/index.json source", index["source"], DATASET_SOURCE)
    problems += spec_problem("dataset/index.json counts", index["counts"], DATASET_COUNTS)

    characters = payloads["characters.json"]
    problems += spec_problem("dataset/characters.json", characters, DATASET_CHARACTERS)
    for entry in characters["characters"]:
        problems += spec_problem("dataset/characters.json 角色", entry, DATASET_CHARACTER)

    albums = payloads["albums.json"]
    problems += spec_problem("dataset/albums.json", albums, DATASET_ALBUMS)
    for entry in albums["albums"]:
        problems += spec_problem("dataset/albums.json 专辑", entry, DATASET_ALBUM)

    tracks = payloads["tracks.json"]
    problems += spec_problem("dataset/tracks.json", tracks, DATASET_TRACKS)
    for track_id, entry in tracks["tracks"].items():
        problems += spec_problem(f"dataset/tracks.json {track_id}", entry, DATASET_TRACK)

    sources = payloads["sources.json"]
    problems += spec_problem("dataset/sources.json", sources, DATASET_SOURCES)
    for entry in sources["sources"]:
        problems += spec_problem("dataset/sources.json 源", entry, DATASET_SOURCE_RECORD)

    pack_audio = payloads["pack-audio.json"]
    problems += spec_problem("dataset/pack-audio.json", pack_audio, DATASET_PACK_AUDIO)
    problems += rows_problem("dataset/pack-audio.json", pack_audio["entries"], DATASET_PACK_AUDIO_ROW)

    assert problems == [], problems


def test_the_dataset_always_carries_pack_audio(built):
    """音MAD 模式**有音频口径** ⇒ ``pack-audio.json`` 必写（自定义模式没有它，那份才可选）。"""
    out, payloads = built

    assert sorted(payloads) == ["albums.json", "characters.json", "index.json",
                                "pack-audio.json", "sources.json", "tracks.json"]
    assert (out / "pack-audio.json").is_file()
    assert payloads["pack-audio.json"]["entries"]          # 真曲目 ⇒ 不是空表


def test_index_counts_match_the_built_payloads(built):
    """``index.counts`` 与实产内容一致 —— 这条本身就是断言（真源在长，**不写死 80/191**）。"""
    _out, payloads = built
    characters = payloads["characters.json"]["characters"]
    counts = payloads["index.json"]["counts"]

    assert counts["characters"] >= 1 and counts["trackEntries"] >= 1     # 真源在场
    assert counts == {
        "characters": len(characters),
        "albums": len(payloads["albums.json"]["albums"]),
        "trackEntries": sum(len(entry["music"]) for entry in characters),
        "distinctTracks": len(payloads["tracks.json"]["tracks"]),
    }


# ---------------------------------------------------------------- 本机助手清单（本仓库写、前端读）

@pytest.fixture()
def library(tmp_path):
    """最小曲库：一首 ASCII、一首非 ASCII（与 ``test_local_source.py`` 同一份夹具形状）。"""
    (tmp_path / "otomads").mkdir()
    (tmp_path / "otomads" / "thwy - 岁月.mp3").write_bytes(b"ID3" + bytes(range(256)) * 4)
    (tmp_path / "测试专辑").mkdir()
    (tmp_path / "测试专辑" / "01. 曲目.mp3").write_bytes(b"ID3" + b"\x00" * 999)
    (tmp_path / "readme.txt").write_text("not audio", encoding="utf-8")
    return tmp_path


def test_local_assistant_manifest_follows_the_shared_vectors(library):
    """助手清单的**基形状**（``build_manifest`` 不传 ``snapshot``）对向量。

    顶层键集合对 ``ASSISTANT_MANIFEST``、每行元数对 ``ASSISTANT_ROW``；第 4 位必须是**内容哈希**
    （§2.6）—— 与 ``packformat.content_revision`` 逐一相等，不再是 mtime 那套。线上助手发的那份还
    并进 D145 的 ``albums`` / ``characters`` 包数据段，那是**另一个**已单测的形状，不在这个基向量里。
    """
    manifest = local_source.build_manifest(str(library), "http://127.0.0.1:8011", "otomads")
    problems = spec_problem("manifest.json", manifest, ASSISTANT_MANIFEST)
    problems += rows_problem("manifest.json", manifest["tracks"], ASSISTANT_ROW)
    assert problems == [], problems

    revisions = {f"{album}/{title}": packformat.content_revision(path)
                 for album, title, path in local_source.library_files(str(library))}
    assert len(revisions) >= 1 and len(manifest["tracks"]) == len(revisions)
    for album, title, _url, revision in manifest["tracks"]:
        assert revision == revisions[f"{album}/{title}"]
        assert len(revision) == 16
