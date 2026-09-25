"""`stage_media.repack`（CI 侧重打归档，D149）的守卫。

**核心性质**：本机 `pack` 出来的归档，CI `repack` 一遍**逐字节相同** —— 这条成立才敢让 CI 拿
"上一份归档的媒体 + 仓库现在的曲目表"去重打（版本号必须用**内容**哈希，见 `packformat.content_revision`；
照 mtime 算的话两边永远对不上，客户端每次都要重下全部 324 MB）。

`make_library` / `declare_pack` 与 `test_media_review.py` 里那两个同形（测试之间不互相 import）。
"""
import json
import pathlib
import tarfile

import pytest

from otomads import packformat as packs
from otomads import paths
from otomads import stage_media as sm

TRACKS = {"thwy - 岁月": b"a" * 32, "Rendering-Liu - 山茶花": b"b" * 48}


def make_library(root: pathlib.Path, tracks: dict[str, bytes], album: str = "otomads") -> pathlib.Path:
    directory = root / album
    directory.mkdir(parents=True)
    for title, body in tracks.items():
        (directory / f"{title}.mp3").write_bytes(body)
    return root


def declare_pack(data_repo: pathlib.Path, *, titles: tuple[str, ...] = ("岁月", "山茶花"),
                 extra: str = "角色曲", pack_id: str = "otomads", character: str = "cirno") -> None:
    """把"仓库现在的曲包"写成这个样子（先清空，语义是"声明"不是"追加"）。"""
    import shutil
    packs_dir = data_repo / "packs"
    shutil.rmtree(packs_dir / pack_id, ignore_errors=True)
    (packs_dir / pack_id).mkdir(parents=True, exist_ok=True)
    (packs_dir / f"{pack_id}.toml").write_text(
        f'[pack]\nid = "{pack_id}"\n\n[[album]]\nkey = "{pack_id}"\nname = "{pack_id}"\n'
        f'kind = "other"\npack = "{pack_id}"\norder = 100\n', encoding="utf-8")
    body = f'key = "{character}"\n'
    for index, title in enumerate(titles):
        author = "thwy" if index == 0 else "Rendering-Liu"
        body += (f'\n[[track]]\nalbum = "{pack_id}"\nauthor = "{author}"\ntitle = "{title}"\n'
                 f'extra = "{extra}"\n')
    (packs_dir / pack_id / f"{character}.toml").write_text(body, encoding="utf-8")


@pytest.fixture
def data_repo(tmp_path, monkeypatch) -> pathlib.Path:
    root = tmp_path / "data-repo"
    root.mkdir()
    monkeypatch.setattr(paths, "DATA", root)
    declare_pack(root)
    return root


@pytest.fixture
def local_archive(tmp_path, data_repo) -> pathlib.Path:
    library = make_library(tmp_path / "lib", TRACKS)
    out = tmp_path / "local.tar.gz"
    sm.pack(library, out)
    return out


def manifest_of(path: pathlib.Path) -> dict:
    return sm.manifest_of(path)


def test_repack_reproduces_the_local_pack(local_archive, tmp_path):
    """**核心性质**：本机打的归档，CI 重打一遍**逐字节相同**（`changed` 也必须是 False）。"""
    again = tmp_path / "repacked.tar.gz"
    summary = sm.repack(local_archive, again)
    assert summary["changed"] is False
    assert again.read_bytes() == local_archive.read_bytes()


def test_repack_picks_up_a_pack_change(local_archive, data_repo, tmp_path):
    """仓库里改了曲目表（这里改附加信息）⇒ 重打的清单跟着变，媒体不动。"""
    declare_pack(data_repo, extra="道中曲")
    again = tmp_path / "repacked.tar.gz"
    summary = sm.repack(local_archive, again)
    assert summary["changed"] is True
    entries = [entry for character in manifest_of(again)["characters"] for entry in character["music"]]
    assert {entry[2] for entry in entries} == {"道中曲"}

    with tarfile.open(local_archive) as old, tarfile.open(again) as new:
        old_media = {n: old.extractfile(n).read() for n in old.getnames() if n.startswith("media/")}
        new_media = {n: new.extractfile(n).read() for n in new.getnames() if n.startswith("media/")}
    assert new_media == old_media          # 媒体逐字节不变（来源就是上一份归档）


def test_repack_ships_the_repo_loudness_table(local_archive, data_repo, tmp_path):
    """响度表在**仓库**里 ⇒ 重算过的表会跟着重打进来（而清单本身不变 ⇒ 必须比归档、不能只比清单）。"""
    assert "loudness" not in manifest_of(local_archive)
    (data_repo / "sources").mkdir()
    (data_repo / "sources" / "otomads.toml").write_text(
        '[[source]]\nid = "local"\ntable_url = "manifest.json"\n'
        'loudness = "loudness/otomads.json"\nkind = "local"\nenabled = true\n', encoding="utf-8")
    (data_repo / "loudness").mkdir()
    (data_repo / "loudness" / "otomads.json").write_text(
        json.dumps({"schema": 1, "gains": {"thwy - 岁月": 1.0}}), encoding="utf-8")

    again = tmp_path / "repacked.tar.gz"
    summary = sm.repack(local_archive, again)
    assert summary["loudness"] == 1 and summary["changed"] is True
    assert manifest_of(again)["loudness"] == "loudness/otomads.json"
    with tarfile.open(again) as archive:
        assert "loudness/otomads.json" in archive.getnames()


def test_repack_refuses_a_missing_previous(tmp_path):
    with pytest.raises(SystemExit, match="上一份归档不存在"):
        sm.repack(tmp_path / "nope.tar.gz", tmp_path / "out.tar.gz")


def test_repack_cli_prints_whether_it_changed(local_archive, tmp_path, capsys):
    out = tmp_path / "repacked.tar.gz"
    assert sm.main(["repack", "--previous", str(local_archive), "--out", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "重打" in printed and "没变" in printed
