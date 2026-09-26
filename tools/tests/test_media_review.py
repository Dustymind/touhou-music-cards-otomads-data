"""`otomads.stage_media` 的**铺之前自检**（`review`，D147）与曲名归一化口径。

为什么要单独一个文件：这套判据是**部署的守卫**（CI 里铺 CDN 之前跑），
与 `test_stage_media.py` 那份"打包/铺盘"的守卫关注点不同 —— 那边管"打得对不对"，
这边管"**这次要铺的东西**对不对"，还包括"归档 ↔ 本仓库 packs 是否一致"。

`make_library` / `declare_pack` 与 `test_stage_media.py` 里那两个同形（测试之间不互相 import，
免得一个文件的改动牵动另一个）。归档用 `pack()` 真打一个出来，再按需要改写里面的 manifest ——
**不造假 tar**，走的都是真路径。
"""
import io
import json
import pathlib
import shutil
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


def declare_pack(data_repo: pathlib.Path, pack_id: str = "otomads", character: str = "cirno",
                 titles: tuple[str, ...] = ("岁月",)) -> None:
    """临时"数据仓库"里写一份曲包（清单 + 一角色一份），让 `packformat.repo_snapshot()` 有东西可比。

    **先清空角色目录**：这个助手说的是"把曲包声明成这个样子"，不是"往上追加" ——
    否则上一次调用留下的角色文件会一起被读进来（踩过：想验"归档里多了个角色"却怎么也验不出来）。
    """
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
                 f'extra = "角色曲"\n')
    (packs_dir / pack_id / f"{character}.toml").write_text(body, encoding="utf-8")


@pytest.fixture
def data_repo(tmp_path, monkeypatch) -> pathlib.Path:
    """把 `paths.DATA` 指到临时目录（照 `test_stage_media.py` 的口径隔离真仓库）。"""
    root = tmp_path / "data-repo"
    root.mkdir()
    monkeypatch.setattr(paths, "DATA", root)
    return root


@pytest.fixture
def library(tmp_path, data_repo) -> pathlib.Path:
    return make_library(tmp_path / "lib", TRACKS)


@pytest.fixture
def archive(library, data_repo, tmp_path) -> pathlib.Path:
    declare_pack(data_repo, titles=("岁月", "山茶花"))
    out = tmp_path / "media.tar.gz"
    sm.pack(library, out)
    return out


def rewrite_manifest(source: pathlib.Path, out: pathlib.Path, mutate) -> pathlib.Path:
    """把归档整个复制一份、只改里面的 manifest（其余成员逐字节照搬）。"""
    with tarfile.open(source, "r:gz") as tar:
        members = [(info, tar.extractfile(info).read() if info.isfile() else None)
                   for info in tar.getmembers()]
    manifest = json.loads(dict((info.name, body) for info, body in members if info.isfile())["manifest.json"])
    mutate(manifest)
    with tarfile.open(out, "w:gz") as target:
        for info, body in members:
            if info.name == sm.MANIFEST_NAME:
                body = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
                info = tarfile.TarInfo(sm.MANIFEST_NAME)
                info.size = len(body)
            target.addfile(info, io.BytesIO(body) if body is not None else None)
    return out


# ------------------------------------------------------------------ 好归档

def test_review_accepts_a_freshly_packed_archive(archive):
    problems, warnings = sm.review_archive(archive)
    assert problems == []
    assert warnings == []          # 曲目表与地址表对得上，也与本仓库 packs 一致


def test_review_cli_returns_zero_and_prints_a_summary(archive, capsys):
    assert sm.main(["review", "--archive", str(archive)]) == 0
    printed = capsys.readouterr().out
    assert "归档自检通过" in printed and "2 行地址" in printed


# ------------------------------------------------------------------ 硬失败

def test_review_rejects_a_manifest_without_the_pack_data(archive, tmp_path):
    """缺 D145 的"包数据" ⇒ 应用只会走兜底 ⇒ **这次部署等于没生效**，必须拦住。"""
    broken = rewrite_manifest(archive, tmp_path / "broken.tar.gz",
                              lambda m: (m.pop("albums"), m.pop("characters")))
    problems, _warnings = sm.review_archive(broken)
    assert any("albums" in problem for problem in problems)
    assert any("characters" in problem for problem in problems)
    assert sm.main(["review", "--archive", str(broken)]) == 1


def test_review_rejects_a_row_without_a_file(archive, tmp_path):
    broken = rewrite_manifest(archive, tmp_path / "nofile.tar.gz", lambda m: m["tracks"].append(
        ["otomads", "幽灵曲目", "media/otomads/%E5%B9%BD%E7%81%B5%E6%9B%B2%E7%9B%AE.mp3", "x"]))
    problems, _warnings = sm.review_archive(broken)
    assert any("幽灵曲目" in problem for problem in problems)


def test_review_rejects_a_manifest_without_any_track(archive, tmp_path):
    broken = rewrite_manifest(archive, tmp_path / "empty.tar.gz",
                              lambda m: m["characters"][0].update(music=[]))
    problems, _warnings = sm.review_archive(broken)
    assert any("一条曲目都没有" in problem for problem in problems)


# ------------------------------------------------------------------ 只警告（合法的中间态）

def test_review_warns_about_a_track_that_is_not_fetched_yet(archive, tmp_path):
    """曲目表里有、地址表里没有 = 还没抓/还没打包 ⇒ 只警告（否则补全骨架期间根本铺不上去）。"""
    lagging = rewrite_manifest(archive, tmp_path / "lagging.tar.gz", lambda m: m["characters"][0]
                               ["music"].append(["otomads", "还没抓的曲目", "角色曲"]))
    problems, warnings = sm.review_archive(lagging, compare_with_repo=False)
    assert problems == []
    assert any("还没抓的曲目" in warning for warning in warnings)


def test_review_warns_when_the_archive_lags_behind_the_packs(archive, data_repo):
    """**归档 ↔ 本仓库 packs 不一致**：归档少 = 改完 packs 忘了重打包（CI 日志里必须吼一声）。"""
    declare_pack(data_repo, titles=("岁月", "山茶花", "刚加进 packs 的第三首"))
    problems, warnings = sm.review_archive(archive)
    assert problems == []
    assert any("忘了重打包" in warning for warning in warnings)


def test_review_warns_when_the_archive_has_an_orphan_character(archive, data_repo):
    """归档里的角色在仓库 packs 里已经没有了（换了 key？归档比仓库旧？）⇒ 另一个方向的警告。"""
    declare_pack(data_repo, character="marisa", titles=("岁月",))
    problems, warnings = sm.review_archive(archive)
    assert problems == []
    assert any("归档比仓库旧" in warning for warning in warnings)


def test_compare_snapshots_is_silent_when_there_is_no_repo_snapshot(archive):
    """独立跑（没有 packs/）时不该报假警 —— 只警告"仓库有、归档没有"这一侧。"""
    assert sm.compare_snapshots(sm.archive_snapshot(sm.manifest_of(archive)), None) == []


def test_review_warns_instead_of_dying_when_the_repo_packs_cannot_be_read(archive, data_repo):
    """**回归守卫**：本仓库 `packs/` 读不动（硬失败）⇒ 只警告，**不能**把这条构建打挂。

    `build_cdn_site.py`（CF 的构建入口）也走 `review_archive`：`packs/` 里一个硬失败
    （最典型的是 D153 之前那个顶层 `cover` 数组形状 —— 该跑一次 `fetch_covers` 去迁移）
    会让一份**好归档**整条构建失败、一个文件都不铺。这条对照本来就是"只警告"的（D147）。
    """
    (data_repo / "packs" / "otomads" / "cirno.toml").write_text(
        'key = "cirno"\n\n# 每首曲目一张 B 站封面直链\n# 由 `python -m otomads.fetch_covers` 生成\n'
        'cover = [\n  "https://i0.hdslb.com/a.jpg@703w_1000h_1c.webp",\n]\n\n'
        '[[track]]\nalbum = "otomads"\nauthor = "thwy"\ntitle = "岁月"\nextra = "角色曲"\n',
        encoding="utf-8")

    problems, warnings = sm.review_archive(archive)

    assert problems == []                                  # 归档本身是好的
    assert any("packs/ 读不动" in warning for warning in warnings)
    assert any("fetch_covers" in warning for warning in warnings)   # 报错原文照抄，能照着修
    assert sm.main(["review", "--archive", str(archive)]) == 0


# ------------------------------------------------------------------ 曲名归一化（与前端同口径）

def test_normalize_title_follows_the_disk_name_convention():
    assert packs.normalize_title("thwy - 岁月") == "岁月"
    assert packs.normalize_title("  A   B  ") == "a b"
    assert packs.normalize_title("【东方电气棍】鞍山的唢呐师") == "【东方电气棍】鞍山的唢呐师"
    # 作者名里带 `-` ⇒ 前缀剥不掉（两边都一样），靠后缀那条救
    assert packs.normalize_title("Rendering-Liu - 山茶花") == "rendering-liu - 山茶花"
    assert packs.titles_match("Rendering-Liu - 山茶花", "山茶花") is True
    assert packs.titles_match("thwy - 岁月", "别的曲名") is False
