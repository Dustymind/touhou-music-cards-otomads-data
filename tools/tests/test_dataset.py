"""`otomads.dataset`（本仓库自己的数据集构建）的测试：形状、id 规则、计数、源注册表、
确定性、响度表拷贝。

这里写出的字节就是主仓库消费的东西（`pack-audio.json` 直接进 contentHash 的输入），
所以除了形状，还钉住"两次运行逐字节相同"与"坏 table_url 构建期就炸"。
"""
from __future__ import annotations

import json
import pathlib
import subprocess

import pytest

from otomads import dataset, packformat, paths


MANIFEST = """[pack]
id = "demo"
label_en = "Demo"
label_zh = "演示"
kind = "local"
order = 100

[[album]]
key = "demo"
name = "demo"
kind = "other"
pack = "demo"
order = 100
show_album_name = false
"""

REGISTRY = """[[source]]
id = "local"
label_en = "Local library"
label_zh = "本地曲库"
table_url = "https://example.com/manifest.json"
loudness = "loudness/otomads.json"
kind = "local"
order = 1
enabled = true
proxyable = false
description_en = "EN"
description_zh = "中"
"""

LOUDNESS = '{\n "schema": 1,\n "gains": {}\n}\n'


def write_tree(root: pathlib.Path, files: dict[str, str]) -> None:
    """把字面量写成文件。`newline="\\n"` **必须显式写**：下面 `test_loudness_is_copied_byte_for_byte`
    比的是字节，而 Windows 上 `write_text` 默认会把 `\\n` 翻成 `\\r\\n` —— 那样"真源是 LF"
    这个前提就不成立了，测试会因为**替身自己**写错而红（与被测代码无关）。"""
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")


def track(title: str, **fields) -> str:
    """一条 `[[track]]` 的 TOML（键值用 json.dumps 渲染，转义口径与 TOML 基本字符串一致）。"""
    lines = ["[[track]]", 'album = "demo"', f"title = {json.dumps(title, ensure_ascii=False)}",
             'extra = "角色曲"']
    for key, value in fields.items():
        lines.append(f"{key} = {json.dumps(value, ensure_ascii=False) if not isinstance(value, int) else value}")
    return "\n".join(lines) + "\n"


def character(key: str, *, tracks: str = "", card: list[str] | None = None) -> str:
    head = f'key = "{key}"\n'
    if card:
        head += "card = [" + ", ".join(json.dumps(face, ensure_ascii=False) for face in card) + "]\n"
    return head + "\n" + tracks


def scaffold(root: pathlib.Path, *, pack_files: dict[str, str], manifest: str = MANIFEST,
             registry: str = REGISTRY, with_loudness: bool = True) -> None:
    """写一份最小真源：一个曲包（清单 + 每角色一份曲目文件）+ 源注册表（+ 响度表）。"""
    files = {"packs/demo.toml": manifest, "sources/otomads.toml": registry}
    for key, body in pack_files.items():
        files[f"packs/demo/{key}.toml"] = body
    write_tree(root, files)
    if with_loudness:
        write_tree(root, {"loudness/otomads.json": LOUDNESS})


@pytest.fixture()
def data_root(tmp_path, monkeypatch):
    """把 `paths.DATA` 指到最小真源；`paths.ROOT` 不动（git_source 仍看真仓库）。"""
    monkeypatch.setattr(paths, "DATA", tmp_path)
    return tmp_path


def build(root: pathlib.Path, data_root: pathlib.Path) -> tuple[pathlib.Path, dict]:
    out = root / "out"
    return out, dataset.build_dataset(out)


# ------------------------------------------------------------------ 形状与字节格式

def test_build_writes_the_six_json_files_in_the_shared_byte_format(data_root, tmp_path):
    scaffold(data_root, pack_files={"cirno": character("cirno", tracks=track("冰之妖精"))})
    out, payloads = build(tmp_path, data_root)

    assert sorted(path.name for path in out.iterdir() if path.is_file()) == [
        "albums.json", "characters.json", "index.json", "pack-audio.json", "sources.json", "tracks.json"]
    assert sorted(payloads) == [
        "albums.json", "characters.json", "index.json", "pack-audio.json", "sources.json", "tracks.json"]
    for name, payload in payloads.items():
        text = (out / name).read_text(encoding="utf-8")
        assert text == json.dumps(payload, ensure_ascii=False, indent=1) + "\n"
    assert list(payloads["albums.json"]) == ["schema", "albums"]
    assert list(payloads["characters.json"]) == ["schema", "characters"]
    assert list(payloads["tracks.json"]) == ["schema", "tracks"]
    assert list(payloads["pack-audio.json"]) == ["schema", "entries"]
    assert list(payloads["sources.json"]) == ["schema", "sources"]
    assert all(payload["schema"] == 2 for payload in payloads.values())


def test_built_files_use_lf_line_endings(data_root, tmp_path):
    """六个 JSON + 响度表一律 `\\n` 行尾。

    这些**字节**就是主仓库 contentHash 的输入（`tmc.build.content_hash` 读的是解析后的
    载荷，但"两次运行逐字节相同"与快照对账都是按字节比的），必须与 Linux 侧完全相同。
    Windows 上 `Path.write_text` 默认把 `\\n` 翻成 `\\r\\n` ⇒ 同一份真源在两边产出两套字节，
    而且**一声不响**（只表现为内容指纹对不上）。

    在 Linux 上这条恒真 —— 所以它其实是给 Windows 跑的回归测试（实测：没加
    `newline=""` 时 `pack-audio.json` 多出 1342 个 `\\r`）。
    """
    scaffold(data_root, pack_files={"cirno": character("cirno", tracks=track("一"))})
    out, _payloads = build(tmp_path, data_root)
    offenders = sorted(path.relative_to(out).as_posix()
                       for path in out.rglob("*") if path.is_file() and b"\r" in path.read_bytes())
    assert offenders == []


# ------------------------------------------------------------------ 角色顺序 / 曲目 id

def test_characters_are_identity_less_and_ids_follow_pack_order(data_root, tmp_path):
    scaffold(data_root, pack_files={
        "zoe": character("zoe", tracks=track("Z1") + track("Z2")),
        "amy": character("amy", tracks="".join(track(f"T{i:02d}") for i in range(1, 13))),
    })
    out, payloads = build(tmp_path, data_root)
    chars = payloads["characters.json"]["characters"]

    # 顺序 = 曲包文件顺序（packformat 给的），**不是**主仓库的身份 order
    _packs, _albums, tracks, _cards, _covers = packformat.load_packs()
    assert [c["key"] for c in chars] == list(dict.fromkeys(t["character"] for t in tracks))
    assert [c["key"] for c in chars] == ["amy", "zoe"]
    assert chars[0]["music"] == [f"amy_otomad_{i:03d}" for i in range(1, 13)]
    assert chars[1]["music"] == ["zoe_otomad_001", "zoe_otomad_002"]
    assert all(set(c) == {"key", "music"} for c in chars)      # 身份字段一个都不在这里


def test_card_and_covers_only_when_the_pack_overrides_them(data_root, tmp_path):
    scaffold(data_root, pack_files={
        "cirno": character("cirno", card=["冰.png"],
                           tracks=track("一", cover="https://img/1.png")
                           + track("二", cover="https://img/2.png")),
        "marisa": character("marisa", tracks=track("三")),
    })
    out, payloads = build(tmp_path, data_root)
    chars = {c["key"]: c for c in payloads["characters.json"]["characters"]}

    assert chars["cirno"]["card"] == ["冰.png"]
    assert chars["cirno"]["covers"] == ["https://img/1.png", "https://img/2.png"]
    assert all(isinstance(item, str) for item in chars["cirno"]["card"] + chars["cirno"]["covers"])
    assert list(chars["cirno"]) == ["key", "music", "card", "covers"]
    assert "card" not in chars["marisa"]                       # 没覆盖就没有那个键
    assert "covers" not in chars["marisa"]


def test_tracks_carry_the_author_forms_like_the_main_repo(data_root, tmp_path):
    scaffold(data_root, pack_files={"cirno": character("cirno", tracks=(
        track("一", author="甲") + track("二", authors=["甲", "乙"])))})
    out, payloads = build(tmp_path, data_root)
    tracks = payloads["tracks.json"]["tracks"]

    assert list(tracks["cirno_otomad_001"]) == ["album", "title", "extra", "author"]
    assert tracks["cirno_otomad_001"]["author"] == "甲"
    assert list(tracks["cirno_otomad_002"]) == ["album", "title", "extra", "author", "authors"]
    assert tracks["cirno_otomad_002"] == {"album": "demo", "title": "二", "extra": "角色曲",
                                          "author": "甲 & 乙", "authors": ["甲", "乙"]}


def test_albums_mirror_build_albums(data_root, tmp_path):
    manifest = """[pack]
id = "demo"

[[album]]
key = "late"
name = "Late"
kind = "other"
pack = "demo"
order = 100
show_album_name = false

[[album]]
key = "early"
name = "Early"
kind = "other"
pack = "demo"
order = 50
"""
    scaffold(data_root, manifest=manifest, pack_files={"cirno": character("cirno", tracks=track("一"))})
    out, payloads = build(tmp_path, data_root)
    albums = payloads["albums.json"]["albums"]

    assert [a["key"] for a in albums] == ["early", "late"]     # 按 order 排
    assert list(albums[0]) == ["key", "name", "kind", "pack", "order"]
    assert list(albums[1]) == ["key", "name", "kind", "pack", "order", "showAlbumName"]
    assert albums[1]["showAlbumName"] is False


# ------------------------------------------------------------------ 音频指纹

def test_pack_audio_entries_match_the_shared_vector():
    tracks = [
        {"album": "demo", "title": "B", "extra": "角色曲", "start_time": "00:00:10.000"},
        {"album": "demo", "title": "A", "extra": "角色曲", "source": "https://example.com/a",
         "stop_time": "00:01:00.000"},
    ]
    expected = [["demo", "A", "", "00:01:00.000", "https://example.com/a"],
                ["demo", "B", "00:00:10.000", "", ""]]
    assert dataset.pack_audio_entries(tracks) == expected       # 按值排序
    assert dataset.build_pack_audio(tracks) == {"schema": 2, "entries": expected}


# ------------------------------------------------------------------ 源注册表

SECOND_REGISTRY = """[[source]]
id = "second"
label_en = "Second"
label_zh = "第二"
table_url = "manifest.json"
kind = "local"
order = 2
enabled = false
description_en = "Two"

[[source]]
id = "local"
label_en = "Local library"
label_zh = "本地曲库"
table_url = "https://example.com/manifest.json"
loudness = "loudness/otomads.json"
kind = "local"
order = 1
enabled = true
proxyable = true
description_en = "EN"
description_zh = "中"
"""


def test_sources_shape_order_and_loudness_url(data_root, tmp_path):
    scaffold(data_root, registry=SECOND_REGISTRY,
             pack_files={"cirno": character("cirno", tracks=track("一"))})
    out, payloads = build(tmp_path, data_root)
    sources = payloads["sources.json"]["sources"]

    assert [s["id"] for s in sources] == ["local", "second"]   # 按 order 排
    assert list(sources[0]) == ["id", "label", "tableUrl", "kind", "order", "enabled",
                                "proxyable", "description", "loudnessUrl"]
    assert list(sources[1]) == ["id", "label", "tableUrl", "kind", "order", "enabled",
                                "proxyable", "description"]
    assert sources[0]["label"] == {"en": "Local library", "zh": "本地曲库"}
    assert sources[0]["description"] == {"en": "EN", "zh": "中"}
    assert sources[0]["loudnessUrl"] == "loudness/otomads.json"
    assert sources[1]["proxyable"] is False                    # 缺省 False
    assert sources[1]["description"]["zh"] == ""               # 缺省空串


@pytest.mark.parametrize("bad", ["/manifest.json", "ftp://example.com/x.json", "data:x", "", "//host/x.json"])
def test_bad_table_url_fails_loudly(bad):
    entry = {"id": "x", "label_en": "X", "label_zh": "X", "table_url": bad,
             "kind": "local", "order": 1, "enabled": True}
    with pytest.raises(SystemExit):
        dataset.build_sources([entry])


@pytest.mark.parametrize("ok", ["https://example.com/manifest.json", "HTTP://example.com/x.json",
                                "manifest.json", "data/sources/x.json"])
def test_table_url_shapes_that_are_allowed(ok):
    assert dataset.table_url_problem(ok) is None


# ------------------------------------------------------------------ 响度表

def test_loudness_is_copied_byte_for_byte(data_root, tmp_path):
    scaffold(data_root, pack_files={"cirno": character("cirno", tracks=track("一"))})
    out, _payloads = build(tmp_path, data_root)
    assert (out / "loudness/otomads.json").read_bytes() == LOUDNESS.encode("utf-8")


def test_loudness_is_skipped_when_the_source_file_is_absent(data_root, tmp_path):
    scaffold(data_root, pack_files={"cirno": character("cirno", tracks=track("一"))},
             with_loudness=False)
    out, payloads = build(tmp_path, data_root)
    assert not (out / "loudness").exists()                     # 静默跳过，不当成构建失败
    assert payloads["index.json"]["counts"]["characters"] == 1


# ------------------------------------------------------------------ 确定性 / index

def test_two_runs_are_byte_identical(data_root, tmp_path):
    scaffold(data_root, pack_files={
        "cirno": character("cirno", card=["冰.png"], tracks=track("一", cover="https://img/1.png")),
        "marisa": character("marisa", tracks=track("二")),
    })
    first, _payloads = build(tmp_path, data_root)
    second = tmp_path / "out2"
    dataset.build_dataset(second)

    def files(root: pathlib.Path) -> dict:
        return {path.relative_to(root): path.read_bytes()
                for path in sorted(root.rglob("*")) if path.is_file()}

    assert files(first) == files(second)


def test_index_source_block_names_this_repo_and_omits_content_hash(data_root, tmp_path):
    scaffold(data_root, pack_files={"cirno": character("cirno", tracks=track("一"))})
    out, payloads = build(tmp_path, data_root)
    index = payloads["index.json"]

    assert list(index) == ["schema", "mode", "source", "counts"]
    assert index["mode"] == "otomads" and index["schema"] == 2
    assert "contentHash" not in index                          # 身份合并之后才算得出来（消费方算）
    assert list(index["source"]) == ["repo", "commit", "dirty"]
    assert index["source"]["repo"] == "touhou-music-cards-otomads-data"
    head = subprocess.run(["git", "-C", str(paths.ROOT), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()
    assert index["source"]["commit"] == head
    assert isinstance(index["source"]["dirty"], bool)
    assert list(index["counts"]) == ["characters", "albums", "trackEntries", "distinctTracks"]
    assert index["counts"] == {"characters": 1, "albums": 1, "trackEntries": 1, "distinctTracks": 1}


# ------------------------------------------------------------------ 与本仓库真源的计数一致

def test_counts_match_the_pack_truth_source(tmp_path):
    """在**真仓库**上构建：计数与顺序必须与 `packformat.load_packs()` 一致。"""
    payloads = dataset.build_dataset(tmp_path / "ds")
    _packs, albums, tracks, _cards, _covers = packformat.load_packs()
    counts = payloads["index.json"]["counts"]
    chars = payloads["characters.json"]["characters"]

    assert counts["characters"] == len({t["character"] for t in tracks})
    assert counts["albums"] == len(albums)
    assert counts["trackEntries"] == len(tracks)
    assert counts["distinctTracks"] == len(tracks)
    assert [c["key"] for c in chars] == list(dict.fromkeys(t["character"] for t in tracks))
    ids = [track_id for c in chars for track_id in c["music"]]
    assert len(ids) == len(set(ids)) == len(tracks)
    assert all(c["music"][0] == f'{c["key"]}_otomad_001' for c in chars if c["music"])


def test_cli_builds_into_the_given_directory(data_root, tmp_path):
    scaffold(data_root, pack_files={"cirno": character("cirno", tracks=track("一"))})
    out = tmp_path / "cli-out"
    assert dataset.main(["--out", str(out)]) == 0
    assert (out / "index.json").exists() and (out / "pack-audio.json").exists()
