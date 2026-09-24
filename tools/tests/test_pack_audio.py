"""曲包音频键（`source` / `start_time` / `stop_time`）与抓取/裁剪流程的测试。

契约：`docs/packs-audio-v1.md`。这里只测**离线可验证**的部分（解析、校验、幂等规则、
硬链接与缓存失效）；真正的下载靠 `--track` 手工验一次。
"""
from __future__ import annotations

import argparse
import pathlib
import json
import os
import sys

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



def test_audio_descriptors_feed_the_content_hash():
    rows = packs.audio_descriptors([make_track(title="一", source="https://a", start_time="00:00:01.000")])
    assert rows == [["demo", "一", "00:00:01.000", "", "https://a"]]


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


def stub_download(source: str, raw) -> None:
    """替掉真下载：写一个假的"原件"，让流程的其余部分可以离线跑。"""
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_bytes(b"ID3" + b"\x00" * 32)


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
