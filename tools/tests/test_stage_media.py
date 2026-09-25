"""`otomads.stage_media`：打归档 / 铺 dist 的两步（D138）。

盯住四件事：布局与 manifest 口径（相对地址、曲目名 = 磁盘 stem、与助手同一套 quote）、
**可复现**（同一份素材打两次逐字节相同）、自检真的能发现缺件/多余件、
以及**归档是不可信输入**（路径穿越与链接必须被拒）。
"""
import io
import json
import os
import pathlib
import tarfile

import pytest

from otomads import local_source as ls
from otomads import paths
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
def data_repo(tmp_path, monkeypatch) -> pathlib.Path:
    """临时"数据仓库"根（把 `paths.DATA` 指过去）：默认**没有**注册表 ⇒ 归档不带响度表。

    必须隔离：`pack`/`stage` 会去读源注册表里声明的响度表，指到真仓库的话测试会跟着真实数据漂移。
    """
    root = tmp_path / "data-repo"
    root.mkdir()
    monkeypatch.setattr(paths, "DATA", root)
    return root


@pytest.fixture
def library(tmp_path, data_repo) -> pathlib.Path:
    return make_library(tmp_path / "lib", TRACKS)


def declare_table(data_repo: pathlib.Path, gains: dict, pack_id: str = "otomads") -> pathlib.Path:
    """在临时数据仓库里写一份"注册表声明 + 响度表"，返回表路径。"""
    (data_repo / "sources").mkdir(exist_ok=True)
    (data_repo / "sources" / f"{pack_id}.toml").write_text(
        f'[[source]]\nid = "local"\ntable_url = "manifest.json"\n'
        f'loudness = "loudness/{pack_id}.json"\nkind = "local"\nenabled = true\n',
        encoding="utf-8")
    (data_repo / "loudness").mkdir(exist_ok=True)
    table = data_repo / "loudness" / f"{pack_id}.json"
    table.write_text(json.dumps({"schema": 1, "targetDb": -11.3, "measuredDb": gains, "gains": gains},
                                ensure_ascii=False), encoding="utf-8")
    return table


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


# ------------------------------------------------------------------ 响度表跟着源走（D139）

def test_pack_declares_and_ships_the_loudness_table(library, data_repo, tmp_path):
    """注册表声明了表 ⇒ 归档里带上它，且 manifest 用**相对 manifest** 的路径声明它。"""
    declare_table(data_repo, dict.fromkeys(TRACKS, 0.9))
    out = tmp_path / "media.tar.gz"
    summary = sm.pack(library, out)
    assert summary["loudness"] == 1
    with tarfile.open(out) as archive:
        names = sorted(archive.getnames())
        assert "loudness/otomads.json" in names
        manifest = json.loads(archive.extractfile(sm.MANIFEST_NAME).read().decode("utf-8"))
    assert manifest["loudness"] == "loudness/otomads.json"


def test_pack_without_a_declared_table_adds_neither_field_nor_file(library, tmp_path):
    out = tmp_path / "media.tar.gz"
    assert sm.pack(library, out)["loudness"] == 0
    with tarfile.open(out) as archive:
        manifest = json.loads(archive.extractfile(sm.MANIFEST_NAME).read().decode("utf-8"))
        assert "loudness" not in manifest
        assert not [name for name in archive.getnames() if name.startswith("loudness/")]


def test_pack_skips_a_declared_but_missing_table(library, data_repo, tmp_path, capsys):
    """声明了但表不在 ⇒ 不声明、不带文件（声明了却发不出来 = 前端 404，而且不会回落）。"""
    declare_table(data_repo, {})
    (data_repo / "loudness" / "otomads.json").unlink()
    out = tmp_path / "media.tar.gz"
    assert sm.pack(library, out)["loudness"] == 0
    assert "响度表不存在" in capsys.readouterr().out
    with tarfile.open(out) as archive:
        manifest = json.loads(archive.extractfile(sm.MANIFEST_NAME).read().decode("utf-8"))
    assert "loudness" not in manifest


def test_stage_from_library_ships_the_table(library, data_repo, tmp_path):
    declare_table(data_repo, dict.fromkeys(TRACKS, 0.9))
    out = tmp_path / "dist"
    summary = sm.stage(out, library=library)
    assert summary["loudness"] == 1
    assert (out / "loudness" / "otomads.json").is_file()
    assert read_manifest(out)["loudness"] == "loudness/otomads.json"


def test_stage_base_rewrite_keeps_the_loudness_declaration(library, data_repo, tmp_path):
    """`--base` 只重烘媒体地址，**不能把响度表声明弄丢**。"""
    declare_table(data_repo, dict.fromkeys(TRACKS, 0.9))
    archive = tmp_path / "media.tar.gz"
    sm.pack(library, archive)
    out = tmp_path / "dist"
    sm.stage(out, archive=str(archive), base="https://h/sub/")
    manifest = read_manifest(out)
    assert manifest["loudness"] == "loudness/otomads.json"
    assert all(row[2].startswith("https://h/sub/") for row in manifest["tracks"])


def test_loudness_coverage_reports_missing_and_extra(library, data_repo, tmp_path):
    """覆盖自查：缺的、多的都要报出来（只报数，不当错误 —— 缺 = 那首没量过，合法）。"""
    titles = sorted(TRACKS)
    declare_table(data_repo, {titles[0]: 0.9, "幽灵 - 不存在": 1.0})
    out = tmp_path / "dist"
    sm.stage(out, library=library)
    coverage = sm.loudness_coverage(out)
    assert coverage["declared"] == "loudness/otomads.json"
    assert coverage["keys"] == 2
    assert coverage["missing"] == [titles[1]]
    assert coverage["extra"] == ["幽灵 - 不存在"]


def test_cli_prints_the_loudness_line(library, data_repo, tmp_path, capsys):
    declare_table(data_repo, dict.fromkeys(TRACKS, 0.9))
    assert sm.main(["stage", "--from", str(library), "--out", str(tmp_path / "dist")]) == 0
    assert "响度表 loudness/otomads.json：2 条" in capsys.readouterr().out


def manifest_of(archive: pathlib.Path) -> dict:
    """从**归档**里读 manifest（`read_manifest` 那个是给 `stage --from` 的目录形态用的）。"""
    with tarfile.open(archive) as handle:
        return json.loads(handle.extractfile(sm.MANIFEST_NAME).read().decode("utf-8"))


def test_pack_manifest_carries_media_revisions(library, tmp_path):
    """D144：逐曲版本号写在第 4 位 + 整表 ``revision``；**前三项与助手逐字一致**（只加不改）。

    版本号只进清单、不进地址；前端把它拼成 `?v=`，于是"音频换了但链接没变"也能立刻拿到新的。
    """
    out = tmp_path / "media.tar.gz"
    sm.pack(library, out)
    manifest = manifest_of(out)

    assert len(manifest["revision"]) == 16
    for row in manifest["tracks"]:
        assert len(row) == 4 and len(row[3]) == 16
        assert row[0] == "otomads"
        assert row[2].startswith("media/otomads/") and not row[2].startswith("/")

    # 没声明时不许凭空造键（老调用方/别的包不受影响）
    bare = sm.build_manifest(["a"], "otomads")
    assert "revision" not in bare and len(bare["tracks"][0]) == 3

    # 文件变了 ⇒ **只有那一首**的版本号跟着变（逐曲，不是整包一起换）
    victim = library / "otomads" / "Rendering-Liu - 岁月.mp3"
    stamp = victim.stat()
    os.utime(victim, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 1_000_000))
    again = tmp_path / "again.tar.gz"
    sm.pack(library, again)
    moved = [row for row, old in zip(manifest_of(again)["tracks"], manifest["tracks"])
             if row[3] != old[3]]
    assert len(moved) == 1, moved
    # 地址一个字没动 —— 失效的只有版本号那一位
    assert [row[2] for row in manifest_of(again)["tracks"]] == [row[2] for row in manifest["tracks"]]


# --------------------------------------------- 曲目表跟着源走（D145，C 路线）

def declare_pack(data_repo: pathlib.Path, pack_id: str = "otomads",
                 character: str = "cirno") -> pathlib.Path:
    """在临时"数据仓库"里写一份最小曲包（清单 + 一角色一份），返回曲包根。"""
    packs_dir = data_repo / "packs"
    (packs_dir / pack_id).mkdir(parents=True, exist_ok=True)
    (packs_dir / f"{pack_id}.toml").write_text(
        f'[pack]\nid = "{pack_id}"\n\n[[album]]\nkey = "{pack_id}"\nname = "{pack_id}"\n'
        f'kind = "other"\npack = "{pack_id}"\norder = 100\nshow_album_name = false\n',
        encoding="utf-8")
    (packs_dir / pack_id / f"{character}.toml").write_text(
        f'key = "{character}"\n\n[[track]]\nalbum = "{pack_id}"\n'
        f'title = "岁月"\nauthor = "Rendering-Liu"\nextra = "角色曲"\n', encoding="utf-8")
    return packs_dir


def test_pack_manifest_carries_the_pack_snapshot(library, data_repo, tmp_path):
    """D145：`pack` 写出的清单里带 `albums` + `characters`（源自带"这个包有哪些曲目"）。

    **曲目表按曲包 TOML、地址按磁盘文件**：这里曲包只声明了一首，而曲库有两个文件 ——
    于是清单行的**条目数**（源状态那一行按它显示）与快照的**曲目表**本来就是两件事。
    """
    declare_pack(data_repo)
    out = tmp_path / "media.tar.gz"
    sm.pack(library, out)
    manifest = manifest_of(out)

    assert len(manifest["tracks"]) == 2                       # 地址表：磁盘上有什么
    assert manifest["albums"] == [{"key": "otomads", "name": "otomads", "kind": "other",
                                   "pack": "otomads", "order": 100, "showAlbumName": False}]
    assert manifest["characters"] == [
        {"key": "cirno", "music": [["otomads", "岁月", "角色曲", "Rendering-Liu"]]}]
    # 归档仍然可复现（多两个键不影响"同一份素材打两次逐字节相同"）
    again = tmp_path / "again.tar.gz"
    sm.pack(library, again)
    assert out.read_bytes() == again.read_bytes()


def test_pack_without_packs_keeps_the_old_manifest_shape(library, data_repo, tmp_path):
    """没有曲包真源 ⇒ 清单退回老形状（既不报错、也不凭空造键）。"""
    out = tmp_path / "media.tar.gz"
    sm.pack(library, out)
    manifest = manifest_of(out)
    assert "albums" not in manifest and "characters" not in manifest
    assert sm.build_manifest(["a"], "otomads") == {
        "schema": 1, "pack": "otomads", "tracks": [["otomads", "a", "media/otomads/a.mp3"]]}


def test_stage_base_rewrite_keeps_the_pack_snapshot(library, data_repo, tmp_path):
    """`--base` 只重烘媒体地址，**不能把曲目表弄丢**（和响度表声明一个道理）。"""
    declare_pack(data_repo)
    archive = tmp_path / "media.tar.gz"
    sm.pack(library, archive)
    out = tmp_path / "dist"
    sm.stage(out, archive=str(archive), base="https://h/sub/")
    manifest = read_manifest(out)
    assert all(row[2].startswith("https://h/sub/") for row in manifest["tracks"])
    assert manifest["characters"] == [
        {"key": "cirno", "music": [["otomads", "岁月", "角色曲", "Rendering-Liu"]]}]


def test_stage_from_library_carries_the_pack_snapshot(library, data_repo, tmp_path):
    """`stage --from <曲库>` 写出的清单同样带曲目表（本机全静态构建那条路）。"""
    declare_pack(data_repo)
    out = tmp_path / "dist"
    sm.stage(out, library=library)
    manifest = read_manifest(out)
    assert manifest["albums"][0]["key"] == "otomads"
    assert [c["key"] for c in manifest["characters"]] == ["cirno"]
