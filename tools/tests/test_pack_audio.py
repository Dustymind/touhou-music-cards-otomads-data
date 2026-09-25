"""曲包音频键（`source` / `start_time` / `stop_time`）与抓取/裁剪流程的测试。

契约：`docs/packs-audio-v1.md`。这里只测**离线可验证**的部分（解析、校验、幂等规则、
硬链接与缓存失效）；真正的下载靠 `--track` 手工验一次。
"""
from __future__ import annotations

import argparse
import array
import math
import pathlib
import json
import os
import shutil
import subprocess
import sys
import types

import pytest

from otomads import fetch_audio, local_source, loudness, measure_loudness, packformat as packs

PACKS = [{"id": "demo", "label": {"en": "Demo", "zh": "演示"}, "kind": "local", "order": 100}]
ALBUMS = [{"key": "demo", "name": "demo", "kind": "other", "pack": "demo", "order": 100}]
CHARS = [{"key": "cirno", "music": []}]


def make_track(**overrides) -> dict:
    track = {"character": "cirno", "album": "demo", "title": "曲目", "extra": "角色曲", "pack": "demo"}
    track.update(overrides)
    return track


# ------------------------------------------------------------------ 时间与区间

@pytest.mark.parametrize(("text", "seconds"), [
    ("00:00:00.000", 0.0),
    ("00:00:40.000", 40.0),
    ("00:01:10.500", 70.5),
    ("01:02:03.004", 3723.004),
    ("99:59:59.999", 359_999.999),
    (" 00:00:07.000 ", 7.0),          # 两头空白容忍
])
def test_parse_time_ok(text, seconds):
    assert packs.parse_time(text) == pytest.approx(seconds)


@pytest.mark.parametrize("text", [
    "00:00:00",          # 缺毫秒
    "00:00:00.00",       # 毫秒必须三位
    "0:0:00.000",        # 分/秒必须两位
    "00:60:00.000",      # 分越界
    "00:00:60.000",      # 秒越界
    "1.5", "abc", "", "00:00:00.0000",
])
def test_parse_time_rejects_bad_format(text):
    with pytest.raises(ValueError):
        packs.parse_time(text)


def test_trim_seconds_semantics():
    assert packs.trim_seconds({}) is None                                  # 都没写 → 不裁
    assert packs.trim_seconds({"start_time": "00:00:40.000"}) == (40.0, None)   # 只给 start → 裁到结尾
    assert packs.trim_seconds({"stop_time": "00:01:10.000"}) == (0.0, 70.0)     # 只给 stop → 从开头
    assert packs.trim_seconds({"start_time": "00:00:40.000",
                               "stop_time": "00:01:10.000"}) == (40.0, 30.0)


@pytest.mark.parametrize("pair", [
    ("00:01:10.000", "00:00:40.000"),    # 倒挂
    ("00:00:40.000", "00:00:40.000"),    # 相等
])
def test_trim_seconds_rejects_reversed(pair):
    with pytest.raises(ValueError):
        packs.trim_seconds({"start_time": pair[0], "stop_time": pair[1]})


# ------------------------------------------------------------------ 文件名与原件键

def test_audio_filename_matches_manifest_convention():
    assert packs.audio_filename(make_track(author="川先僧", title="普通肥猫魔法使")) == "川先僧 - 普通肥猫魔法使.mp3"
    assert packs.audio_filename(make_track(author="  川先僧  ")) == "川先僧 - 曲目.mp3"
    assert packs.audio_filename(make_track()) == "曲目.mp3"          # 没作者就只用标题


def test_source_key_is_stable_and_distinct():
    one = packs.source_key("https://www.bilibili.com/video/BV1kw411q7S8/")
    assert one == packs.source_key("https://www.bilibili.com/video/BV1kw411q7S8/")
    assert len(one) == 16
    assert one != packs.source_key("https://www.bilibili.com/video/BV1kw411q7S9/")


# ------------------------------------------------------------------ 解析（含未知键）

def write_pack(tmp_path, tracks: str, manifest: str = '[pack]\nid = "demo"\n', character: str = "cirno"):
    """写一个最小曲包：`packs/demo.toml`（清单）+ `packs/demo/<角色>.toml`（曲目）。"""
    packs_dir = tmp_path / "packs"
    (packs_dir / "demo").mkdir(parents=True, exist_ok=True)
    (packs_dir / "demo.toml").write_text(manifest, encoding="utf-8")
    (packs_dir / "demo" / f"{character}.toml").write_text(
        f'key = "{character}"\n\n{tracks}', encoding="utf-8")


def test_load_packs_reads_audio_keys(tmp_path, monkeypatch):
    write_pack(tmp_path, """
[[track]]
album = "demo"
author = "作者"
title = "标题"
extra = "角色曲"
source = "https://example.com/a"
start_time = "00:00:10.000"
stop_time = "00:00:20.000"
""")
    monkeypatch.setattr(packs.repo, "DATA", tmp_path)
    _packs, _albums, tracks, _cards = packs.load_packs()
    assert tracks[0]["character"] == "cirno"                 # 角色由文件的 key 决定
    assert tracks[0]["source"] == "https://example.com/a"
    assert tracks[0]["start_time"] == "00:00:10.000"
    assert packs.trim_seconds(tracks[0]) == (10.0, 10.0)


@pytest.mark.parametrize(("line", "message"), [
    ('starttime = "00:00:10.000"', "不认识的键"),          # 拼错的键不许静默丢弃
    ('character = "cirno"', "不认识的键"),                 # 角色由文件的 key 决定，不再逐条写
    ('source = "ftp://example.com/a"', "source 必须是"),   # 只认 http(s)
    ('start_time = "10"', "时间格式"),                     # 格式错
    ('start_time = "00:00:30.000"\nstop_time = "00:00:10.000"', "必须晚于"),   # 区间倒挂
])
def test_load_packs_rejects_bad_keys(tmp_path, monkeypatch, line, message):
    write_pack(tmp_path, f'[[track]]\nalbum = "demo"\ntitle = "标题"\n{line}\n')
    monkeypatch.setattr(packs.repo, "DATA", tmp_path)
    with pytest.raises(SystemExit, match=message):
        packs.load_packs()


def test_load_packs_allows_stop_only(tmp_path, monkeypatch):
    write_pack(tmp_path, '[[track]]\nalbum = "demo"\ntitle = "标题"\nstop_time = "00:00:10.000"\n')
    monkeypatch.setattr(packs.repo, "DATA", tmp_path)
    _packs, _albums, tracks, _cards = packs.load_packs()
    assert packs.trim_seconds(tracks[0]) == (0.0, 10.0)      # 只给 stop ⇒ 从开头裁到 10s


def test_load_packs_reads_character_cards(tmp_path, monkeypatch):
    """角色文件可以写 `card`（音MAD 侧自己的卡面，写法同 data/characters/*.toml）。"""
    packs_dir = tmp_path / "packs"
    (packs_dir / "demo").mkdir(parents=True, exist_ok=True)
    (packs_dir / "demo.toml").write_text('[pack]\nid = "demo"\n', encoding="utf-8")
    (packs_dir / "demo" / "cirno.toml").write_text(
        'key = "cirno"\ncard = ["チルノ-mad.png", "チルノ-mad2.png"]\n\n'
        '[[track]]\nalbum = "demo"\ntitle = "标题"\n', encoding="utf-8")
    monkeypatch.setattr(packs.repo, "DATA", tmp_path)
    _packs, _albums, tracks, cards = packs.load_packs()
    assert cards == {"cirno": ["チルノ-mad.png", "チルノ-mad2.png"]}
    assert tracks[0]["character"] == "cirno"
    # 没写 card 的角色不进这张表（缺省沿用共享身份的卡面）
    assert set(cards) == {"cirno"}


@pytest.mark.parametrize("line", ['card = []', 'card = "x.png"', 'card = [""]'])
def test_load_packs_rejects_bad_card(tmp_path, monkeypatch, line):
    packs_dir = tmp_path / "packs"
    (packs_dir / "demo").mkdir(parents=True, exist_ok=True)
    (packs_dir / "demo.toml").write_text('[pack]\nid = "demo"\n', encoding="utf-8")
    (packs_dir / "demo" / "cirno.toml").write_text(f'key = "cirno"\n{line}\n', encoding="utf-8")
    monkeypatch.setattr(packs.repo, "DATA", tmp_path)
    with pytest.raises(SystemExit, match="card 必须是"):
        packs.load_packs()



@pytest.mark.parametrize(("manifest", "character", "body", "message"), [
    # 清单里写曲目：曲目一律进角色文件（否则两处都能写，迟早漂移）
    ('[pack]\nid = "demo"\n\n[[track]]\nalbum = "demo"\ntitle = "标题"\n', "cirno",
     'key = "cirno"\n', "一角色一份"),
    # 角色文件缺 key
    ('[pack]\nid = "demo"\n', "cirno", '[[track]]\nalbum = "demo"\ntitle = "标题"\n', "缺少 key"),
    # 文件名与 key 不一致（写错一个字符就会被静默错挂，所以直接报）
    ('[pack]\nid = "demo"\n', "cirno", 'key = "cirno-2"\n', "文件名与 key 不一致"),
    # 角色文件里写 [pack] / [[album]]
    ('[pack]\nid = "demo"\n', "cirno", 'key = "cirno"\n[pack]\nid = "demo"\n', "只能写在清单"),
])
def test_load_packs_rejects_bad_layout(tmp_path, monkeypatch, manifest, character, body, message):
    packs_dir = tmp_path / "packs"
    (packs_dir / "demo").mkdir(parents=True, exist_ok=True)
    (packs_dir / "demo.toml").write_text(manifest, encoding="utf-8")
    (packs_dir / "demo" / f"{character}.toml").write_text(body, encoding="utf-8")
    monkeypatch.setattr(packs.repo, "DATA", tmp_path)
    with pytest.raises(SystemExit, match=message):
        packs.load_packs()



# ------------------------------------------------ 曲目表快照（D145，C 路线）

#: **共享测试向量**：与主仓库 `tools/tests/test_build.py` 里那份**同一份字面量**。
#: 它钉的是"源在自己的清单里给的曲目表"与"主仓库构建出来的自带数据"必须**逐字同形**（D145）：
#: 任一侧改了形状（少一位作者、多一个字段），两侧里总有一侧会红 —— 那正是"看得见、点不响"的成因。
PACK_MUSIC_VECTOR = [
    ({"album": "demo", "title": "只有附加信息", "extra": "角色曲"},
     ["demo", "只有附加信息", "角色曲"]),
    ({"album": "demo", "title": "单作者", "extra": "道中曲", "author": "甲"},
     ["demo", "单作者", "道中曲", "甲"]),
    ({"album": "demo", "title": "多作者", "extra": "秘封曲", "author": "甲 & 乙", "authors": ["甲", "乙"]},
     ["demo", "多作者", "秘封曲", "甲 & 乙", ["甲", "乙"]]),
]


def test_music_entry_matches_the_shared_vector():
    for case, expected in PACK_MUSIC_VECTOR:
        assert packs.music_entry(case) == expected


def test_pack_snapshot_groups_by_character_and_keeps_cards():
    """快照只给"角色 → 曲目"（+ 可选卡面覆盖），**不复制身份**（name/order 的真源仍在主仓库）。"""
    tracks = [
        make_track(title="一", extra="角色曲"),
        make_track(character="marisa", title="二", extra="道中曲", author="乙"),
        make_track(title="三", extra="秘封曲", author="丙"),
    ]
    snapshot = packs.pack_snapshot(ALBUMS, tracks, {"cirno": ["チルノ-mad.png"]})
    assert snapshot["albums"] == ALBUMS
    assert [c["key"] for c in snapshot["characters"]] == ["cirno", "marisa"]   # 首次出现的顺序
    assert snapshot["characters"][0] == {
        "key": "cirno", "music": [["demo", "一", "角色曲"], ["demo", "三", "秘封曲", "丙"]],
        "card": ["チルノ-mad.png"]}
    assert snapshot["characters"][1] == {"key": "marisa", "music": [["demo", "二", "道中曲", "乙"]]}
    # 入参不被就地改（纯函数）
    assert tracks[0]["character"] == "cirno" and "card" not in snapshot["characters"][1]


def test_pack_snapshot_sees_a_track_added_to_the_pack(tmp_path, monkeypatch):
    """往曲包 TOML 里加一首 ⇒ 它出现在快照里（C 的全部意义："加曲目只动数据仓库"）。"""
    write_pack(tmp_path, '[[track]]\nalbum = "demo"\ntitle = "一"\n')
    monkeypatch.setattr(packs.repo, "DATA", tmp_path)
    before = packs.pack_snapshot(*packs.load_packs()[1:])
    assert [entry[1] for entry in before["characters"][0]["music"]] == ["一"]

    (tmp_path / "packs" / "demo" / "cirno.toml").write_text(
        'key = "cirno"\n\n[[track]]\nalbum = "demo"\ntitle = "一"\n\n'
        '[[track]]\nalbum = "demo"\ntitle = "二"\n', encoding="utf-8")
    after = packs.pack_snapshot(*packs.load_packs()[1:])
    assert [entry[1] for entry in after["characters"][0]["music"]] == ["一", "二"]


def test_pack_snapshot_of_needs_both_keys():
    """清单 → 快照段：两个键要么都在、要么都不在；只有一半按"不带"处理（坏数据不当半信半疑地用）。"""
    assert packs.pack_snapshot_of({"tracks": []}) is None
    assert packs.pack_snapshot_of({"albums": ALBUMS}) is None
    assert packs.pack_snapshot_of({"characters": CHARS}) is None
    assert packs.pack_snapshot_of({"albums": ALBUMS, "characters": CHARS}) == {
        "albums": ALBUMS, "characters": CHARS}


# ------------------------------------------------------------------ 曲库扫描与助手

def test_scan_library_skips_dot_entries(tmp_path):
    (tmp_path / "otomads").mkdir()
    (tmp_path / "otomads" / "thwy - 岁月.mp3").write_bytes(b"ID3")
    (tmp_path / ".raw").mkdir()
    (tmp_path / ".raw" / "abcdef.mp3").write_bytes(b"ID3")          # 原件目录：不许进 manifest
    (tmp_path / ".state").mkdir()
    (tmp_path / ".state" / "hidden.mp3").write_bytes(b"ID3")
    (tmp_path / "otomads" / ".partial.mp3").write_bytes(b"ID3")     # 临时文件同理
    assert local_source.scan_library(str(tmp_path)) == [("otomads", "thwy - 岁月")]


# ------------------------------------------------------------------ 响度缓存

def test_measure_library_invalidates_reset_and_prunes_missing(tmp_path):
    library = tmp_path / "otomads"
    library.mkdir()
    (library / "a - x.mp3").write_bytes(b"ID3")
    (library / "b - y.mp3").write_bytes(b"ID3")
    output = tmp_path / "loudness.json"
    output.write_text(json.dumps({"schema": 1, "targetDb": -11.0, "gains": {},
                                  "measuredDb": {"a - x": -10.0, "b - y": -12.0, "gone - z": -9.0}}),
                      encoding="utf-8")

    summary = loudness.measure_library(library, output=output, reset=["a - x"], measure=lambda _p: -8.0)

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["measuredDb"]["a - x"] == -8.0        # 裁过的曲目被重置后重量
    assert payload["measuredDb"]["b - y"] == -12.0       # 没动过的沿用缓存
    assert "gone - z" not in payload["measuredDb"]       # 文件已不在 → 清掉
    assert summary["measured"] == 2
    assert all(loudness.MIN_GAIN <= gain <= loudness.MAX_GAIN for gain in payload["gains"].values())


# ------------------------------------------------------------------ 抓取流程（离线部分）

def test_version_key_normalizes_zero_padding():
    assert fetch_audio.version_key("2026.08.19") == fetch_audio.version_key("2026.8.19")
    assert fetch_audio.version_key("2026.9.1") > fetch_audio.version_key("2026.08.19")


def dry_args(**overrides) -> argparse.Namespace:
    base = {"dry_run": True, "force": False}
    base.update(overrides)
    return argparse.Namespace(**base)


def run_track(track, *, library, state, outputs, args, changed) -> dict:
    """调 `process_track` 并把"新认领的同源同区间登记项"并进 `outputs`（= 主流程做的事）。

    返回 outcome —— 单个线程的顺序语义与并发前完全一致，所以这批用例照旧能守行为。
    """
    outcome, claimed = fetch_audio.process_track(track, library=library, state=state, outputs=outputs,
                                                 args=args, changed=changed)
    outputs.update(claimed)
    return outcome


def test_process_track_dry_run_plans_download(tmp_path):
    track = make_track(title="标题", author="作者", source="https://example.com/a",
                       start_time="00:00:10.000", stop_time="00:00:20.000")
    outcome = run_track(track, library=tmp_path, state={"version": 1, "tracks": {}},
                        outputs={}, args=dry_args(), changed=set())
    assert outcome["status"] == "dry"
    assert "下载 + 裁剪" in outcome["detail"]


def test_process_track_reports_missing_without_source(tmp_path):
    outcome = run_track(make_track(title="标题"), library=tmp_path,
                        state={"version": 1, "tracks": {}}, outputs={},
                        args=dry_args(dry_run=False), changed=set())
    assert outcome["status"] == "missing"
    assert "没有 source" in outcome["detail"]


def test_process_track_skips_manual_file(tmp_path):
    manual = tmp_path / "demo" / packs.audio_filename(make_track(title="标题"))
    manual.parent.mkdir(parents=True)
    manual.write_bytes(b"ID3")
    outcome = run_track(make_track(title="标题"), library=tmp_path,
                        state={"version": 1, "tracks": {}}, outputs={},
                        args=dry_args(dry_run=False), changed=set())
    assert outcome["status"] == "skip"
    assert "人工入库" in outcome["detail"]


def test_render_without_trim_hardlinks_the_raw_file(tmp_path):
    raw = tmp_path / ".raw" / "abcdef.mp3"
    raw.parent.mkdir(parents=True)
    raw.write_bytes(b"ID3" + b"\x00" * 64)
    out = tmp_path / "demo" / "作者 - 标题.mp3"

    fetch_audio.render(raw, out, None, tmp_path / fetch_audio.TMP_DIR)

    assert out.read_bytes() == raw.read_bytes()
    assert os.stat(out).st_ino == os.stat(raw).st_ino        # 同一 inode：省磁盘，且证明是硬链接


# ------------------------------------------------ 裁剪 = 解码后精确切 + 重编码（2026-09-25）

def capture_ffmpeg(monkeypatch) -> list[list[str]]:
    """顶掉 `subprocess.run`，把 ffmpeg 命令记下来；**并把临时文件真造出来**。

    不造文件的话 `render()` 末尾的 `os.replace(tmp, out)` 会因为 tmp 不存在而炸 ✗。
    只换 `fetch_audio` 里的 `subprocess` 名字，不动全局的（免得影响 pytest 自己）。
    """
    calls: list[list[str]] = []

    def fake_run(command, **_kwargs):
        calls.append(list(command))
        pathlib.Path(command[-1]).write_bytes(b"ID3" + b"\x00" * 16)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(fetch_audio, "subprocess",
                        types.SimpleNamespace(run=fake_run, DEVNULL=subprocess.DEVNULL))
    return calls


def test_render_with_trim_decodes_and_reencodes(tmp_path, monkeypatch):
    """裁剪**不许**再用 `-c copy`（帧边界 + 冷启动解码：见 docs/packs-audio-v1.md §5）。

    命令的形状必须是"回退 0.5s 粗定位 → 输出侧丢掉预热段 → 限时长 → libmp3lame 重编码"。
    """
    calls = capture_ffmpeg(monkeypatch)
    raw = tmp_path / ".raw" / "abcdef.mp3"
    out = tmp_path / "demo" / "作者 - 标题.mp3"

    fetch_audio.render(raw, out, (2.339, 30.0), tmp_path / fetch_audio.TMP_DIR)

    assert len(calls) == 1
    command = calls[0]
    assert "-c" not in command and "copy" not in command, "不许流拷贝"
    assert command[0] == "ffmpeg"
    # 粗定位：2.339 − 0.5 = 1.839；输出侧再丢掉这 0.5 秒（起点因此是采样点级精确的）
    assert command[command.index("-ss") + 1] == "1.839"
    assert command[command.index("-ss", command.index("-ss") + 1) + 1] == "0.500"
    assert command.index("-ss") < command.index("-i") < len(command) - 1 - command[::-1].index("-ss")
    assert command[command.index("-t") + 1] == "30.000"
    assert command[command.index("-c:a") + 1] == "libmp3lame"
    assert command[command.index("-q:a") + 1] == "0"
    assert "-nostdin" in command
    assert out.exists()                                        # 原子改名生效


def test_render_with_trim_clamps_the_warmup_at_the_file_start(tmp_path, monkeypatch):
    """起点落在 0.5 秒以内 ⇒ 回退到文件头即可，不能再往前退（会变负数）。"""
    calls = capture_ffmpeg(monkeypatch)
    raw = tmp_path / ".raw" / "abcdef.mp3"
    out = tmp_path / "demo" / "作者 - 标题.mp3"

    fetch_audio.render(raw, out, (0.2, 5.0), tmp_path / fetch_audio.TMP_DIR)

    command = calls[0]
    assert command[command.index("-ss") + 1] == "0.000"        # 退到文件头，不是 -0.300
    assert command[command.index("-ss", command.index("-ss") + 1) + 1] == "0.200"


def test_render_with_trim_only_start_runs_to_the_end(tmp_path, monkeypatch):
    """`stop_time` 缺省（`duration is None`）⇒ 不许给 `-t`，否则就裁短了。"""
    calls = capture_ffmpeg(monkeypatch)
    raw = tmp_path / ".raw" / "abcdef.mp3"
    out = tmp_path / "demo" / "作者 - 标题.mp3"

    fetch_audio.render(raw, out, (1.388, None), tmp_path / fetch_audio.TMP_DIR)

    assert "-t" not in calls[0]


def test_render_version_invalidates_previous_trims_but_not_link_only_tracks(tmp_path, monkeypatch):
    """**回归守卫**：换渲染口径必须让"裁过的曲目"自动重裁，否则幂等检查会直接跳过它们。

    旧状态（`-c copy` 那会儿）里没有 `render` 键、`outHash` 也对得上 ⇒ 不认口径就会一直 skip，
    表现成"改了代码但音频一个字节都没变"。未裁剪的曲目是硬链接、与口径无关，**不该**被连累重跑。
    """
    monkeypatch.setattr(fetch_audio, "download", stub_download)
    trimmed = make_track(title="裁过", author="A", source="https://example.com/a",
                         start_time="00:00:10.000", stop_time="00:00:20.000")
    whole = make_track(title="整首", author="B", source="https://example.com/b")
    state: dict = {"version": 1, "tracks": {}}
    outputs: dict = {}
    args = dry_args(dry_run=False)
    monkeypatch.setattr(fetch_audio, "render",
                        lambda raw, out, wanted, tmp: (out.parent.mkdir(parents=True, exist_ok=True),
                                                       out.write_bytes(b"ID3" + b"\x00" * 8)))

    run_track(trimmed, library=tmp_path, state=state, outputs=outputs, args=args, changed=set())
    run_track(whole, library=tmp_path, state=state, outputs=outputs, args=args, changed=set())
    assert state["tracks"]["demo\u0001裁过"]["render"] == fetch_audio.RENDER_VERSION
    assert state["tracks"]["demo\u0001整首"]["render"] == fetch_audio.LINK_RENDER

    # 口径没变 ⇒ 两条都 skip（幂等）
    again = run_track(trimmed, library=tmp_path, state=state, outputs={}, args=args, changed=set())
    assert again["status"] == "skip"

    # 模拟"旧状态"：把 render 键抹掉（= `-c copy` 时代落盘的模样），outHash 仍然对得上
    state["tracks"]["demo\u0001裁过"].pop("render")
    state["tracks"]["demo\u0001整首"].pop("render")
    stale = run_track(trimmed, library=tmp_path, state=state, outputs={}, args=args, changed=set())
    assert stale["status"] == "trimmed", "换了渲染口径却没重裁 —— 幂等检查把新代码跳过去了"
    untouched = run_track(whole, library=tmp_path, state=state, outputs={}, args=args, changed=set())
    assert untouched["status"] == "skip", "未裁剪的成品是硬链接，不该被渲染口径连累重跑"


def stub_download(source: str, raw) -> None:
    """替掉真下载：写一个假的"原件"，让流程的其余部分可以离线跑。"""
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_bytes(b"ID3" + b"\x00" * 32)


# ---------------------------------- 一条 source = 一首曲目（多 P 默认取 p1，D143）

def fake_ytdlp(monkeypatch, result: dict) -> list:
    """装一个假 `yt_dlp`（`download()` 里是**函数内** import ⇒ 换 `sys.modules` 即可），不联网。

    返回被造出来的 `YoutubeDL` 实例列表：用例从 `options` 与 `extracted` / `processed` 上取证。
    """
    made: list = []

    class FakeYDL:
        def __init__(self, options):
            self.options = options
            self.extracted: list = []
            self.processed: list = []
            made.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def extract_info(self, url, download=False):
            self.extracted.append((url, download))
            return result

        def process_ie_result(self, info, download=True):
            self.processed.append((info, download))
            # 模拟"落地一个文件"：outtmpl 是 `<raw 去掉后缀>.%(ext)s`
            pathlib.Path(self.options["outtmpl"].replace(".%(ext)s", ".mp3")).write_bytes(b"ID3" + b"\x00" * 16)

    module = types.ModuleType("yt_dlp")
    module.YoutubeDL = FakeYDL
    monkeypatch.setitem(sys.modules, "yt_dlp", module)
    return made


def test_download_asks_yt_dlp_for_a_single_video(tmp_path, monkeypatch):
    """**回归守卫**：`noplaylist` 必须传给 yt-dlp。

    少了它，bilibili 多 P 视频（链接没写 `?p=`）会被 extractor 当成**整张选集**返回
    （`_yes_playlist()`），而 `outtmpl` 是固定文件名 ⇒ 各 P 互相覆盖，最后留下**最后一 P**。
    实测本包 4 条多 P source 全部中招（`月时盆` 拿到 p2「原曲只使用」而不是 p1「原曲不使用」）。
    历史脚本 `ingest_otomads.py` 传的是 `--no-playlist`，是搬到 `fetch_audio` 时丢的。
    """
    made = fake_ytdlp(monkeypatch, {"id": "BVx_p1", "title": "p01"})
    raw = tmp_path / ".raw" / "abcdef.mp3"

    fetch_audio.download("https://www.bilibili.com/video/BVx/", raw)

    assert made[0].options["noplaylist"] is True
    assert made[0].options["cachedir"] is False              # 并发时不许有共享写点
    # 解析阶段**不下载**（先看清是单个视频还是选集），落地只走 process_ie_result
    assert made[0].extracted == [("https://www.bilibili.com/video/BVx/", False)]
    assert [flag for _info, flag in made[0].processed] == [True]
    assert raw.exists()


def test_download_refuses_a_source_that_resolves_to_many_entries(tmp_path, monkeypatch):
    """兜底：万一某个 extractor 无视 `noplaylist`，宁可**报错**也不能悄悄留下最后一 P。"""
    made = fake_ytdlp(monkeypatch, {"_type": "playlist", "entries": [{"id": "a"}, {"id": "b"}]})
    raw = tmp_path / ".raw" / "abcdef.mp3"

    with pytest.raises(RuntimeError) as caught:
        fetch_audio.download("https://example.com/collection", raw)

    message = str(caught.value)
    assert "2 个条目" in message and "?p=N" in message
    assert made[0].processed == []                           # 一个字节都没下
    assert not raw.exists()


def test_fetch_version_invalidates_an_old_raw_and_refetches(tmp_path, monkeypatch):
    """**回归守卫**：抓取口径变了 ⇒ 旧原件必须换掉（光看链接与 `source` 是看不出来的）。

    D142 那轮踩过一次同类坑（渲染口径），这次是抓取口径：链接没变、`outHash` 也对得上，
    不显式判 `fetch` 就会把"可能是别的 P"的旧原件当成"已是目标状态"跳过 ✗。
    """
    downloads: list[str] = []

    def counting_download(source, raw):
        downloads.append(source)
        stub_download(source, raw)

    monkeypatch.setattr(fetch_audio, "download", counting_download)
    track = make_track(title="标题", author="A", source="https://example.com/a")
    state: dict = {"version": 1, "tracks": {}}
    args = dry_args(dry_run=False)

    run_track(track, library=tmp_path, state=state, outputs={}, args=args, changed=set())
    assert state["tracks"]["demo\u0001标题"]["fetch"] == fetch_audio.FETCH_VERSION
    assert len(downloads) == 1

    run_track(track, library=tmp_path, state=state, outputs={}, args=args, changed=set())
    assert len(downloads) == 1, "口径没变就不该重下（幂等）"

    state["tracks"]["demo\u0001标题"].pop("fetch")           # 旧版本落盘的模样
    run_track(track, library=tmp_path, state=state, outputs={}, args=args, changed=set())
    assert len(downloads) == 2, "抓取口径变了却没重下 —— 旧原件（可能是别的 P）会被留下来"


def test_dry_run_says_when_the_raw_will_be_refetched(tmp_path):
    """计划里要**说出**"原件会被换掉"，不然 `（原件已在）` 会被读成"只重裁"。"""
    track = make_track(title="标题", author="A", source="https://example.com/a")
    raw = tmp_path / fetch_audio.RAW_DIR / f"{packs.source_key('https://example.com/a')}.mp3"
    raw.parent.mkdir(parents=True)
    raw.write_bytes(b"ID3")
    state = {"version": 1, "tracks": {"demo\u0001标题": {"source": "https://example.com/a"}}}

    outcome = run_track(track, library=tmp_path, state=state, outputs={},
                        args=dry_args(), changed=set())

    assert outcome["status"] == "dry"
    assert "重下" in outcome["detail"] and "抓取口径变了" in outcome["detail"]


# ------------------------------------------- 裁剪精度（唯一一条真跑 ffmpeg 的用例）

def decode_pcm(path, rate: int) -> array.array:
    """解码成单声道 16 bit PCM（不引 numpy：测试的依赖只有 pytest）。"""
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "s16le",
                          "-acodec", "pcm_s16le", "-ar", str(rate), "-ac", "1", "-"],
                         capture_output=True, check=True, stdin=subprocess.DEVNULL).stdout
    samples = array.array("h")
    samples.frombytes(raw)
    return samples


def rms(samples, start: int = 0, count: int | None = None) -> float:
    chunk = samples[start:] if count is None else samples[start:start + count]
    return math.sqrt(sum(value * value for value in chunk) / len(chunk)) if chunk else 0.0


@pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
                    reason="需要真的 ffmpeg/ffprobe —— 这一条**故意不做替身**（替身测不出音频对不对）")
def test_render_trims_at_the_sample_and_keeps_the_first_frame(tmp_path):
    """**回归守卫**：裁剪要落在采样点上，而且开头那一帧不许是坏的。

    改之前的两处实测缺陷（`-c copy`）：

    * 只能切在 mp3 帧边界 ⇒ 时长与 `stop − start` 差 8…32 ms（真曲目上见过 −90 ms）；
    * 输入定位让 mp3 解码器**冷启动**、拿不到比特池 ⇒ 成品第 0 帧 RMS 只有真值的 2%（有时整帧静音）。

    白噪（非周期）当信号：冷启动坏掉就是"整帧接近静音"，一眼可辨。
    """
    rate = 48000
    raw = tmp_path / ".raw" / "noise.mp3"
    raw.parent.mkdir(parents=True)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
                    "-i", f"anoisesrc=c=white:r={rate}:d=3.0:a=0.5", "-ac", "2",
                    "-c:a", "libmp3lame", "-q:a", "0", str(raw)],
                   check=True, stdin=subprocess.DEVNULL)
    out = tmp_path / "demo" / "作者 - 标题.mp3"

    fetch_audio.render(raw, out, (1.000, 1.000), tmp_path / fetch_audio.TMP_DIR)

    got = decode_pcm(out, rate)
    truth = decode_pcm(raw, rate)
    assert abs(len(got) - rate) <= 2, f"时长应当恰是 1.000 s，实际 {len(got) / rate:.6f} s"
    head, expected = rms(got, 0, 1152), rms(truth, rate, 1152)
    assert head > 0.5 * expected, (
        f"开头那一帧是坏的（RMS {head:.1f} vs 真值 {expected:.1f}）—— 解码器冷启动没修好")


def test_duplicate_source_downloads_once_and_links(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch_audio, "download", stub_download)
    shared = "https://example.com/same"
    first = make_track(title="一", author="A", source=shared)
    second = make_track(title="二", author="B", source=shared)
    state: dict = {"version": 1, "tracks": {}}
    outputs: dict = {}
    args = dry_args(dry_run=False)

    one = run_track(first, library=tmp_path, state=state, outputs=outputs,
                    args=args, changed=set())
    calls = []
    monkeypatch.setattr(fetch_audio, "download", lambda source, raw: calls.append(source))
    two = run_track(second, library=tmp_path, state=state, outputs=outputs,
                    args=args, changed=set())

    assert (one["status"], two["status"]) == ("fetched", "linked")
    assert calls == []                                        # 原件已在 → 不重复下载
    out_one = tmp_path / "demo" / packs.audio_filename(first)
    out_two = tmp_path / "demo" / packs.audio_filename(second)
    assert os.stat(out_one).st_ino == os.stat(out_two).st_ino  # 硬链接：同一 inode
    assert state["tracks"]["demo\u0001二"]["out"] == "demo/B - 二.mp3"


def test_rerun_skips_when_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch_audio, "download", stub_download)
    track = make_track(title="标题", author="A", source="https://example.com/a")
    state: dict = {"version": 1, "tracks": {}}
    args = dry_args(dry_run=False)

    run_track(track, library=tmp_path, state=state, outputs={}, args=args, changed=set())
    again = run_track(track, library=tmp_path, state=state, outputs={}, args=args, changed=set())

    assert again["status"] == "skip"
    assert again["detail"] == "已是目标状态"


# ------------------------------------------------------ 并发（--jobs，D132）

def write_many_tracks(tmp_path, *, shared_pairs: int = 0, singles: int = 0) -> int:
    """写一个曲包：`shared_pairs` 组"两条曲目引用同一个 source"，外加 `singles` 条各用各的源。"""
    body = []
    n = 0
    for index in range(shared_pairs):
        for suffix in ("甲", "乙"):
            n += 1
            body.append(f'[[track]]\nalbum = "demo"\nauthor = "A"\n'
                        f'title = "T{n}{suffix}"\nextra = "角色曲"\n'
                        f'source = "https://example.com/shared{index}"\n')
    for index in range(singles):
        n += 1
        body.append(f'[[track]]\nalbum = "demo"\nauthor = "A"\n'
                    f'title = "S{n}"\nextra = "角色曲"\n'
                    f'source = "https://example.com/only{index}"\n')
    write_pack(tmp_path, "\n".join(body))
    return n


def test_parallel_fetch_keeps_every_track_in_state(tmp_path, monkeypatch):
    """并发收尾不许互相覆盖状态：8 条曲目、4 线程、6 个不同 source。

    这条盯的是"状态文件只写一次/写丢"这一类竞态 —— 每条曲目各自收尾，缺一条就红。
    """
    count = write_many_tracks(tmp_path, shared_pairs=2, singles=4)   # 2×2 + 4 = 8 条，6 个 source
    monkeypatch.setattr(packs.repo, "DATA", tmp_path)
    monkeypatch.setattr(fetch_audio, "ffmpeg_problem", lambda: None)
    monkeypatch.setattr(fetch_audio, "ensure_ytdlp", lambda **_kwargs: None)
    monkeypatch.setattr(fetch_audio, "download", stub_download)
    library = tmp_path / "library"
    library.mkdir()
    config = tmp_path / "local-source.toml"
    config.write_text(f'[library]\nroot = "{library}"\n', encoding="utf-8")

    code = fetch_audio.main(["--config", str(config), "--jobs", "4"])

    assert code == 0
    state = json.loads((library / fetch_audio.STATE_DIR / "demo.json").read_text(encoding="utf-8"))
    assert len(state["tracks"]) == count, state["tracks"].keys()
    outputs = sorted((library / "demo").glob("*.mp3"))
    assert len(outputs) == count
    # 8 条成品必须齐全（不是"8 条里活下来几条"）。标题按 write_many_tracks 的生成规则：
    # 先 2 组共享 source（T1甲/T2乙、T3甲/T4乙），再 4 条独占（S5…S8）
    assert {path.name for path in outputs} == {
        "A - T1甲.mp3", "A - T2乙.mp3", "A - T3甲.mp3", "A - T4乙.mp3",
        "A - S5.mp3", "A - S6.mp3", "A - S7.mp3", "A - S8.mp3",
    }


def test_parallel_fetch_downloads_a_shared_source_once(tmp_path, monkeypatch):
    """两条曲目共用同一个 source 时，并发下**只下一份**原件（原件按 source 存）。

    不加锁的话两个线程会同时往 `.raw/<key>.mp3` 写；这里把下载计数钉住。
    """
    write_many_tracks(tmp_path, shared_pairs=3)                     # 6 条，3 个 source，两两成对
    monkeypatch.setattr(packs.repo, "DATA", tmp_path)
    monkeypatch.setattr(fetch_audio, "ffmpeg_problem", lambda: None)
    monkeypatch.setattr(fetch_audio, "ensure_ytdlp", lambda **_kwargs: None)
    downloads: list[str] = []

    def counting_download(source: str, raw) -> None:
        downloads.append(source)
        stub_download(source, raw)

    monkeypatch.setattr(fetch_audio, "download", counting_download)
    library = tmp_path / "library"
    library.mkdir()
    config = tmp_path / "local-source.toml"
    config.write_text(f'[library]\nroot = "{library}"\n', encoding="utf-8")

    code = fetch_audio.main(["--config", str(config), "--jobs", "6"])

    assert code == 0
    assert sorted(downloads) == [f"https://example.com/shared{index}" for index in range(3)]
    # 同一 source 的两条成品是硬链接（同一 inode）—— 证明第二份是"链过来"而不是重下的
    raws = sorted((library / fetch_audio.RAW_DIR).glob("*.mp3"))
    assert len(raws) == 3
    for raw in raws:
        twins = [path for path in (library / "demo").glob("*.mp3")
                 if os.stat(path).st_ino == os.stat(raw).st_ino]
        assert len(twins) == 2, f"{raw.name} 应该被两条曲目共享"


# --------------------------------------------- 子进程不许碰用户的终端（D132 追加）

def test_every_ffmpeg_and_ytdlp_call_gets_its_own_stdin():
    """**回归守卫**：所有 `ffmpeg` / `yt-dlp` 子进程都必须显式重定向 stdin。

    症状（用户报的）：抓取跑完，终端**不回显**敲进去的命令。机理是子进程继承了终端的 stdin ——
    ffmpeg 只要看到 stdin 是**终端**就会去接管它（`-nostdin` 只管"要不要读"，拦不住"stdin 是 tty"
    这件事），一旦它在异常路径上退出就可能把终端留在非回显状态。86 首 = 86 次机会，量响度那一轮
    还会再来 86 次。

    这条用例把"每个调用点都写死"钉住 —— 只靠人工记是记不住的（Linux 沙箱里复现不出来，
    只能在 WSL2 之类环境上撞）。只读源码，不跑子进程。

    路径按**测试文件的位置**算，不按 CWD：`pytest tools/tests`（在仓库根跑）也必须能过。
    """
    import ast

    # tools/src/otomads/*.py —— 从 tests/ 往上找到 tools/
    sources = sorted((pathlib.Path(__file__).resolve().parent.parent / "src" / "otomads").glob("*.py"))
    assert sources, "没找到工具源码"
    offenders: list[str] = []
    for path in sources:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr in {"run", "Popen", "call", "check_output"}):
                continue
            # 命令字面量里第一个参数是 ffmpeg / yt-dlp / ffprobe 才算
            first = node.args[0] if node.args else None
            text = ast.dump(first) if first is not None else ""
            if not any(tool in text for tool in ("ffmpeg", "ffprobe", "yt-dlp")):
                continue
            keywords = {kw.arg for kw in node.keywords}
            if "stdin" not in keywords:
                offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == [], (
        "这些调用没给子进程独立的 stdin（会抢用户的终端）：" + ", ".join(offenders))



# ------------------------------------------------------------------ 多作者（D135）

def test_load_packs_normalizes_authors_to_the_same_stem(tmp_path, monkeypatch):
    """`authors = ["甲","乙"]` ⇒ 数组 + **与老写法逐字节相同的整串**（成品文件名/响度表键都不变）。"""
    write_pack(tmp_path, """
[[track]]
album = "demo"
authors = ["甲", "乙"]
title = "标题"
""")
    monkeypatch.setattr(packs.repo, "DATA", tmp_path)
    *_rest, tracks, _cards = packs.load_packs()
    assert tracks[0]["authors"] == ["甲", "乙"]
    assert tracks[0]["author"] == "甲 & 乙"
    assert packs.audio_filename(tracks[0]) == "甲 & 乙 - 标题.mp3"
    # 老写法算出来的名字必须一模一样 —— 换写法不用重抓音频
    assert packs.audio_filename(tracks[0]) == packs.audio_filename(make_track(author="甲 & 乙", title="标题"))


def test_audio_filename_falls_back_to_joined_authors():
    """只给 `authors`（没规范化过的手工 dict）时也要算对文件名。"""
    assert packs.audio_filename(make_track(authors=["甲", "乙"])) == "甲 & 乙 - 曲目.mp3"
    assert packs.author_of(make_track(authors=[" 甲 ", "乙 "])) == "甲 & 乙"


@pytest.mark.parametrize(("line", "message"), [
    ('author = "甲"\nauthors = ["甲", "乙"]', "只能写一个"),
    ('authors = "甲"', "authors 必须"),
    ('authors = []', "authors 必须"),
    ('authors = ["甲", "  "]', "不能有空"),
])
def test_load_packs_rejects_bad_authors(tmp_path, monkeypatch, line, message):
    write_pack(tmp_path, f'[[track]]\nalbum = "demo"\ntitle = "标题"\n{line}\n')
    monkeypatch.setattr(packs.repo, "DATA", tmp_path)
    with pytest.raises(SystemExit, match=message):
        packs.load_packs()


def test_load_packs_keeps_a_compound_author_string_intact(tmp_path, monkeypatch):
    """老写法 `author = "乙 & 甲"`：整串保留、**不拆**（人名里也可能有 `&`），也不产生 authors。"""
    write_pack(tmp_path, '[[track]]\nalbum = "demo"\nauthor = "乙 & 甲"\ntitle = "标题"\n')
    monkeypatch.setattr(packs.repo, "DATA", tmp_path)
    *_rest, tracks, _cards = packs.load_packs()
    assert tracks[0]["author"] == "乙 & 甲"
    assert "authors" not in tracks[0]


# ------------------------------------------------------------------ 响度表 × 曲目的对应关系（D135 追加）

def test_coverage_report_flags_missing_and_stale():
    """缺键 = "音频还没抓"（合法，只提示）；多键 = "改名/换曲库后的残留"。"""
    tracks = [make_track(author="甲", title="一"), make_track(author="乙", title="二")]
    report = loudness.coverage_report(tracks, {"甲 - 一": 1.0, "丙 - 三": 0.7}, packs.audio_stem)
    assert report == {"tracks": 2, "covered": 1, "missing": ["乙 - 二"], "stale": ["丙 - 三"]}
    # 响度表的键是**不带扩展名**的 stem（文件名那一位是 audio_filename，两者别混）
    assert packs.audio_stem(make_track(author="甲", title="一")) == "甲 - 一"
    assert packs.audio_filename(make_track(author="甲", title="一")) == "甲 - 一.mp3"

    lines = loudness.describe_coverage(report)
    assert any("缺 1/2 首" in line for line in lines)
    assert any("对不上任何曲目" in line for line in lines)
    # 都对得上时**不占输出**（命令输出保持干净）
    assert loudness.describe_coverage({"tracks": 2, "covered": 2, "missing": [], "stale": []}) == []


def test_coverage_report_truncates_long_lists():
    tracks = [make_track(title=f"曲{i}") for i in range(9)]
    report = loudness.coverage_report(tracks, {}, packs.audio_stem)
    assert report["covered"] == 0 and len(report["missing"]) == 9
    lines = loudness.describe_coverage(report)
    assert sum(1 for line in lines if line.startswith("  · ")) == loudness.COVERAGE_SAMPLE + 1   # +1 是"…还有 N 首"
    assert any("还有 4 首" in line for line in lines)


def test_measure_loudness_prints_coverage_warning(tmp_path, monkeypatch, capsys):
    """真跑一次 CLI：曲库只覆盖一首、曲包有两首 ⇒ 输出里必须出现缺键警告。

    量响度本身用替身顶掉（真量要 ffmpeg + 真音频，那一条由 `test_measure_library_*` 覆盖）。
    """
    output = tmp_path / "loudness.json"

    def fake_measure(directories, *, output, jobs=8, reset=(), measure=None):
        gains = {"甲 - 一": 1.0}
        output.write_text(json.dumps({"schema": 1, "targetDb": -12.0, "measuredDb": {"甲 - 一": -12.0},
                                      "gains": gains}, ensure_ascii=False), encoding="utf-8")
        return {"measured": 1, "target": -12.0, "gains": gains, "dropped": [], "output": output}

    monkeypatch.setattr(measure_loudness.loudness, "measure_library", fake_measure)
    monkeypatch.setattr(measure_loudness.packformat, "loudness_path", lambda pack: output)
    monkeypatch.setattr(measure_loudness.packformat, "available", lambda: True)
    monkeypatch.setattr(measure_loudness.packformat, "load_packs", lambda: (
        [], [], [make_track(author="甲", title="一"), make_track(author="乙", title="二")], {}))
    monkeypatch.setattr(sys, "argv", ["measure_loudness", str(tmp_path), "--pack", "demo"])
    assert measure_loudness.main() == 0

    printed = capsys.readouterr().out
    assert "缺 1/2 首" in printed and "乙 - 二" in printed
    assert "对不上任何曲目" not in printed              # 表里那个键正好是曲包里的第一条 ⇒ 没有残留


def test_measure_loudness_without_pack_dir_skips_coverage(tmp_path, monkeypatch, capsys):
    """曲包目录不在（只跑助手/单测环境）⇒ 不打印对应关系，也不报错。"""
    output = tmp_path / "loudness.json"

    def fake_measure(directories, *, output, jobs=8, reset=(), measure=None):
        return {"measured": 0, "target": 0.0, "gains": {}, "dropped": [], "output": output}

    monkeypatch.setattr(measure_loudness.loudness, "measure_library", fake_measure)
    monkeypatch.setattr(measure_loudness.packformat, "loudness_path", lambda pack: output)
    monkeypatch.setattr(measure_loudness.packformat, "available", lambda: False)
    monkeypatch.setattr(sys, "argv", ["measure_loudness", str(tmp_path), "--pack", "demo"])
    assert measure_loudness.main() == 1                  # 一首都没量到 ⇒ 退出码 1（原有口径）
    assert "⚠️" not in capsys.readouterr().out
