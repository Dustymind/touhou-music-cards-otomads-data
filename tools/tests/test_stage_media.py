"""`otomads.stage_media`：打归档 / 铺 dist 的两步（D138）。

盯住四件事：布局与 manifest 口径（相对地址、曲目名 = 磁盘 stem、与助手同一套 quote）、
**可复现**（同一份素材打两次逐字节相同）、自检真的能发现缺件/多余件、
以及**归档是不可信输入**（路径穿越与链接必须被拒）。
"""
import io
import json
import pathlib
import tarfile

import pytest

from otomads import local_source as ls
from otomads import stage_media as sm


def make_library(root: pathlib.Path, tracks: dict[str, bytes], album: str = "otomads") -> pathlib.Path:
    """造一个最小曲库：`<root>/<专辑>/<曲目>.mp3` + 一个非音频文件 + 一个点开头的文件。"""
    directory = root / album
    directory.mkdir(parents=True)
    for title, body in tracks.items():
        (directory / f"{title}.mp3").write_bytes(body)
    (directory / "说明.txt").write_text("不是音频", encoding="utf-8")
    (directory / ".hidden.mp3").write_bytes(b"dot")
    return root


TRACKS = {"Chyan_184 - 【东方电气棍】鞍山红茶馆 ~ Chinese Tea": b"a" * 32,
          "Rendering-Liu - 岁月": b"b" * 48}


@pytest.fixture
def library(tmp_path) -> pathlib.Path:
    return make_library(tmp_path / "lib", TRACKS)


def read_manifest(out: pathlib.Path) -> dict:
    return json.loads((out / sm.MANIFEST_NAME).read_text(encoding="utf-8"))


# ------------------------------------------------------------------ pack

def test_pack_layout_and_manifest(library, tmp_path):
    """归档里只有 manifest + `media/<专辑>/*.mp3`；曲目名 = 磁盘 stem；地址是**相对**的。"""
    out = tmp_path / "media.tar.gz"
    summary = sm.pack(library, out)
    assert summary["tracks"] == 2 and summary["cards"] == 0

    with tarfile.open(out) as archive:
        names = sorted(archive.getnames())
    assert names == [sm.MANIFEST_NAME,
                     "media/otomads/Chyan_184 - 【东方电气棍】鞍山红茶馆 ~ Chinese Tea.mp3",
                     "media/otomads/Rendering-Liu - 岁月.mp3"]

    with tarfile.open(out) as archive:
        manifest = json.loads(archive.extractfile(sm.MANIFEST_NAME).read().decode("utf-8"))
    assert manifest["schema"] == 1 and manifest["pack"] == "otomads"
    assert [row[1] for row in manifest["tracks"]] == sorted(TRACKS)
    for row in manifest["tracks"]:
        assert row[0] == "otomads"
        assert row[2].startswith("media/otomads/") and not row[2].startswith("/")


def test_pack_is_reproducible(library, tmp_path):
    """同一份曲库打两次，归档**逐字节相同**（tar 成员排序 + mtime/uid/gid 归零 + gzip mtime 归零）。"""
    first, second = tmp_path / "a.tar.gz", tmp_path / "b.tar.gz"
    sm.pack(library, first)
    sm.pack(library, second)
    assert first.read_bytes() == second.read_bytes()


def test_pack_says_what_is_wrong_when_the_album_is_missing(tmp_path):
    empty = tmp_path / "lib"
    (empty / "otomads").mkdir(parents=True)
    with pytest.raises(SystemExit, match="没有专辑"):
        sm.pack(empty, tmp_path / "x.tar.gz")
    with pytest.raises(SystemExit, match="曲库目录不存在"):
        sm.pack(tmp_path / "nope", tmp_path / "x.tar.gz")


def test_pack_includes_cards_when_asked(library, tmp_path):
    cards = tmp_path / "cards"
    cards.mkdir()
    (cards / "魔理沙.png").write_bytes(b"png")
    (cards / ".DS_Store").write_bytes(b"junk")     # 点开头的不进归档
    out = tmp_path / "media.tar.gz"
    assert sm.pack(library, out, cards=cards)["cards"] == 1
    with tarfile.open(out) as archive:
        assert "cards-otomads/魔理沙.png" in archive.getnames()
        assert "cards-otomads/.DS_Store" not in archive.getnames()


# ------------------------------------------------------------------ stage

def test_stage_from_archive(library, tmp_path):
    archive = tmp_path / "media.tar.gz"
    sm.pack(library, archive)
    out = tmp_path / "dist"
    summary = sm.stage(out, archive=str(archive))
    assert summary["tracks"] == 2
    assert (out / "media" / "otomads" / "Rendering-Liu - 岁月.mp3").read_bytes() == TRACKS["Rendering-Liu - 岁月"]
    assert sm.verify(out) == []
    assert read_manifest(out)["tracks"][0][2].startswith("media/otomads/")


def test_stage_with_base_bakes_absolute_urls(library, tmp_path):
    """`--base` 把媒体地址重烘成绝对（manifest 与媒体不同源时才需要）。"""
    archive = tmp_path / "media.tar.gz"
    sm.pack(library, archive)
    out = tmp_path / "dist"
    sm.stage(out, archive=str(archive), base="https://user.github.io/repo/")
    urls = [row[2] for row in read_manifest(out)["tracks"]]
    assert all(url.startswith("https://user.github.io/repo/media/otomads/") for url in urls), urls
    assert sm.verify(out) == []


def test_stage_directly_from_the_library(library, tmp_path):
    """不起归档也能铺（本地全静态构建用）：布局与 manifest 与走归档时一致。"""
    out = tmp_path / "dist"
    summary = sm.stage(out, library=library)
    assert summary["source"] == "library" and summary["tracks"] == 2
    assert sm.verify(out) == []
    assert (out / sm.MANIFEST_NAME).is_file()


def test_stage_needs_exactly_one_source(tmp_path):
    with pytest.raises(SystemExit, match="二选一"):
        sm.stage(tmp_path / "dist")
    with pytest.raises(SystemExit, match="二选一"):
        sm.stage(tmp_path / "dist", archive="x.tar.gz", library=tmp_path)


def test_stage_rejects_path_traversal(tmp_path):
    """归档来自网络 ⇒ 按不可信输入处理：`../` 成员必须被拒，且不许写到 out 之外。"""
    evil = tmp_path / "evil.tar.gz"
    with tarfile.open(evil, "w:gz") as archive:
        info = tarfile.TarInfo("../escaped.txt")
        payload = b"pwned"
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    out = tmp_path / "dist"
    with pytest.raises(SystemExit, match="不安全的路径"):
        sm.stage(out, archive=str(evil))
    assert not (tmp_path / "escaped.txt").exists()


def test_stage_rejects_links(tmp_path):
    evil = tmp_path / "link.tar.gz"
    with tarfile.open(evil, "w:gz") as archive:
        info = tarfile.TarInfo("media/otomads/hack.mp3")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        archive.addfile(info)
    with pytest.raises(SystemExit, match="不支持的成员类型"):
        sm.stage(tmp_path / "dist", archive=str(evil))


# ------------------------------------------------------------------ 自检

def test_verify_reports_missing_and_extra(library, tmp_path):
    out = tmp_path / "dist"
    sm.stage(out, library=library)
    manifest = read_manifest(out)
    manifest["tracks"].append(["otomads", "不存在的曲子", "media/otomads/x.mp3"])   # 缺件
    (out / sm.MANIFEST_NAME).write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    (out / "media" / "otomads" / "幽灵.mp3").write_bytes(b"ghost")                 # 多余件
    problems = sm.verify(out)
    assert any("磁盘上没有：不存在的曲子" in problem for problem in problems)
    assert any("manifest 没列：幽灵" in problem for problem in problems)


def test_verify_checks_the_album_and_the_url_shape(library, tmp_path):
    out = tmp_path / "dist"
    sm.stage(out, library=library)
    manifest = read_manifest(out)
    manifest["tracks"][0][0] = "别的东西"
    manifest["tracks"][1][2] = "https://example.com/随便.mp3"
    (out / sm.MANIFEST_NAME).write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    problems = sm.verify(out)
    assert any("专辑不是" in problem for problem in problems)
    assert any("地址与口径不符" in problem for problem in problems)


# ------------------------------------------------------------------ 口径

def test_media_url_reuses_the_helper_convention():
    """相对地址 = 助手 `media_path` 去掉前导 `/`；绝对地址 = `base` + 助手那一段（同一套 quote）。"""
    title = "Rendering-Liu - 岁月 ~ x"
    helper = ls.media_path("otomads", title)
    assert sm.media_url("otomads", title) == helper.lstrip("/")
    assert sm.media_url("otomads", title, "https://h/sub/") == "https://h/sub" + helper
    assert "%" in helper                                        # 非 ASCII 真的被编码了


def test_cli_stage_returns_zero_and_prints_selfcheck(library, tmp_path, capsys):
    out = tmp_path / "dist"
    assert sm.main(["stage", "--from", str(library), "--out", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "自检通过" in printed and "2 首" in printed


def test_cli_stage_returns_one_when_selfcheck_fails(library, tmp_path, capsys):
    out = tmp_path / "dist"
    sm.stage(out, library=library)
    (out / "media" / "otomads" / "幽灵.mp3").write_bytes(b"ghost")
    assert sm.main(["stage", "--from", str(library), "--out", str(out)]) == 1
    assert "自检没通过" in capsys.readouterr().out
