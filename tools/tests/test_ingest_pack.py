"""曲包录入器（`otomads.ingest_pack`）：按角色落文件、幂等、key 与 source 校验。"""
from __future__ import annotations

import json
import pathlib

import pytest

from otomads import ingest_pack


def make_data(tmp_path: pathlib.Path, monkeypatch) -> pathlib.Path:
    """最小数据目录：一个曲包清单 + 两个角色。"""
    monkeypatch.setattr(ingest_pack.repo, "DATA", tmp_path)
    (tmp_path / "characters.toml").write_text(
        '[[character]]\nkey = "cirno"\nname = "cirno"\norder = 1\n\n'
        '[[character]]\nkey = "marisa"\nname = "marisa"\norder = 2\n', encoding="utf-8")
    packs = tmp_path / "packs"
    packs.mkdir()
    (packs / "demo.toml").write_text(
        '[pack]\nid = "demo"\n\n[[album]]\nkey = "demo"\nname = "demo"\n', encoding="utf-8")
    return tmp_path


def test_track_block_key_order_and_escaping():
    block = ingest_pack.track_block({
        "album": "demo", "title": '带"引号"与\\反斜杠', "extra": "角色曲", "author": "", "source": "https://a",
    })
    lines = block.splitlines()
    assert lines[0] == "[[track]]"
    assert lines[1] == 'album = "demo"'                      # 键序固定
    assert 'title = "带\\"引号\\"与\\\\反斜杠"' in block        # 引号与反斜杠转义
    assert "author" not in block                             # 空值不写


def test_append_rows_creates_then_skips_duplicates(tmp_path, monkeypatch):
    make_data(tmp_path, monkeypatch)
    rows = [
        {"bv": "BV1", "title": "其一", "author": "作者", "character": "cirno"},
        {"source": "https://example.com/x", "title": "其二", "author": "作者", "character": "cirno"},
    ]
    assert ingest_pack.append_rows("demo", rows) == {"cirno": {"added": 2, "skipped": 0}}
    text = (tmp_path / "packs" / "demo" / "cirno.toml").read_text(encoding="utf-8")
    assert 'key = "cirno"' in text
    assert text.count("[[track]]") == 2
    assert "https://www.bilibili.com/video/BV1/" in text     # bv 拼成地址
    assert "https://example.com/x" in text                   # 完整 source 原样用

    # 幂等：同一条命令再跑一次，全都跳过
    assert ingest_pack.append_rows("demo", rows) == {"cirno": {"added": 0, "skipped": 2}}
    assert (tmp_path / "packs" / "demo" / "cirno.toml").read_text(encoding="utf-8").count("[[track]]") == 2


def test_append_rows_groups_by_character(tmp_path, monkeypatch):
    make_data(tmp_path, monkeypatch)
    summary = ingest_pack.append_rows("demo", [
        {"title": "一", "character": "cirno"},
        {"title": "二", "character": "marisa"},
        {"title": "三", "character": "cirno"},
    ])
    assert summary == {"cirno": {"added": 2, "skipped": 0}, "marisa": {"added": 1, "skipped": 0}}
    assert (tmp_path / "packs" / "demo" / "cirno.toml").read_text(encoding="utf-8").count("[[track]]") == 2
    assert (tmp_path / "packs" / "demo" / "marisa.toml").read_text(encoding="utf-8").count("[[track]]") == 1


def test_append_rows_keeps_existing_text_and_appends_at_the_end(tmp_path, monkeypatch):
    make_data(tmp_path, monkeypatch)
    path = tmp_path / "packs" / "demo" / "cirno.toml"
    path.parent.mkdir(parents=True)
    path.write_text('# 人工注释\nkey = "cirno"\n\n[[track]]\nalbum = "demo"\ntitle = "旧"\n', encoding="utf-8")
    ingest_pack.append_rows("demo", [{"title": "新", "character": "cirno", "author": "作者"}])
    text = path.read_text(encoding="utf-8")
    assert "# 人工注释" in text
    assert text.index('title = "旧"') < text.index('title = "新"')


@pytest.mark.parametrize(("row", "message"), [
    ({"title": "其一", "character": "nope"}, "角色 key 不存在"),          # 写错一个 key 会被静默错挂
    ({"title": "其一", "character": "cirno", "source": "ftp://a"}, "http"),  # 只认 http(s)
    ({"character": "cirno"}, "缺少 character / title"),
])
def test_append_rows_rejects_bad_rows(tmp_path, monkeypatch, row, message):
    make_data(tmp_path, monkeypatch)
    with pytest.raises(SystemExit, match=message):
        ingest_pack.append_rows("demo", [row])


def test_main_dry_run_writes_nothing(tmp_path, monkeypatch):
    make_data(tmp_path, monkeypatch)
    rows = tmp_path / "rows.json"
    rows.write_text(json.dumps([{"title": "其一", "character": "cirno"}]), encoding="utf-8")
    assert ingest_pack.main(["--pack", "demo", "--rows", str(rows), "--dry-run"]) == 0
    assert not (tmp_path / "packs" / "demo").exists()


def test_track_block_writes_authors_array():
    """多作者写成 TOML 数组；键序里 `authors` 紧跟在 `author` 之后（两者只会有一个）。"""
    block = ingest_pack.track_block({
        "album": "demo", "title": "标题", "extra": "角色曲",
        "authors": ["甲", '带"引号"'], "source": "https://a",
    })
    lines = block.splitlines()
    assert lines[0] == "[[track]]"
    assert lines[1] == 'album = "demo"'
    assert 'authors = ["甲", "带\\"引号\\""]' in block      # 数组元素同样转义
    assert "author = " not in block                        # 没写整串那一行


def test_append_rows_writes_authors_from_the_row(tmp_path, monkeypatch):
    make_data(tmp_path, monkeypatch)
    assert ingest_pack.append_rows("demo", [
        {"title": "合写", "authors": ["乙", "甲"], "character": "cirno"},
        {"title": "单人", "author": "丙", "character": "cirno"},
    ]) == {"cirno": {"added": 2, "skipped": 0}}
    text = (tmp_path / "packs" / "demo" / "cirno.toml").read_text(encoding="utf-8")
    assert 'authors = ["乙", "甲"]' in text                 # 数组写法原样落盘（顺序保留，显示时才排序）
    assert 'author = "丙"' in text                          # 老写法不受影响


@pytest.mark.parametrize("authors", [[], "甲", ["甲", " "]])
def test_append_rows_rejects_bad_authors(tmp_path, monkeypatch, authors):
    make_data(tmp_path, monkeypatch)
    with pytest.raises(SystemExit, match="authors"):
        ingest_pack.append_rows("demo", [{"title": "标题", "authors": authors, "character": "cirno"}])
