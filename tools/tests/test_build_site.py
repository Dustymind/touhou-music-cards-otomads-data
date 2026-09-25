"""`tools/build_cdn_site.py` 的守卫（D148）—— 它就是 Cloudflare Pages 那条构建命令。

两条要钉住的：**产物逐字节等于归档**（构建不许"加工"内容：manifest 里的逐曲版本号是本机 mtime 指纹，
构建容器里重算必然错）；**自检不过就一个文件都不铺**（退出码 1 ⇒ CF 那次构建失败、线上保持原样）。

用真子进程跑（不是 import 进来调函数）：脚本自带 `sys.path` 引导、只认环境变量、退出码就是判据 ——
这些都只有按"CF 会怎么跑它"跑一遍才算验过。
"""
import os
import pathlib
import subprocess
import sys
import tarfile

import pytest

from otomads import paths
from otomads import stage_media as sm

SCRIPT = pathlib.Path(sm.__file__).resolve().parents[2] / "build_cdn_site.py"
TRACKS = {"thwy - 岁月": b"a" * 32, "Rendering-Liu - 山茶花": b"b" * 48}


@pytest.fixture
def data_repo(tmp_path, monkeypatch) -> pathlib.Path:
    root = tmp_path / "data-repo"
    (root / "packs" / "otomads").mkdir(parents=True)
    (root / "packs" / "otomads.toml").write_text(
        '[pack]\nid = "otomads"\n\n[[album]]\nkey = "otomads"\nname = "otomads"\n'
        'kind = "other"\npack = "otomads"\norder = 100\n', encoding="utf-8")
    (root / "packs" / "otomads" / "cirno.toml").write_text(
        'key = "cirno"\n\n[[track]]\nalbum = "otomads"\nauthor = "thwy"\ntitle = "岁月"\n'
        'extra = "角色曲"\n\n[[track]]\nalbum = "otomads"\nauthor = "Rendering-Liu"\n'
        'title = "山茶花"\nextra = "角色曲"\n', encoding="utf-8")
    monkeypatch.setattr(paths, "DATA", root)
    return root


@pytest.fixture
def archive(tmp_path, data_repo) -> pathlib.Path:
    library = tmp_path / "lib" / "otomads"
    library.mkdir(parents=True)
    for title, body in TRACKS.items():
        (library / f"{title}.mp3").write_bytes(body)
    out = tmp_path / "media.tar.gz"
    sm.pack(library.parent, out)
    return out


def run_script(archive: pathlib.Path, tmp_path: pathlib.Path, out: pathlib.Path):
    env = {
        **os.environ,
        "OTOMADS_MEDIA_URL": f"file://{archive}",
        "OTOMADS_ARCHIVE": str(tmp_path / "fetched.tar.gz"),
        "OTOMADS_OUT": str(out),
    }
    return subprocess.run([sys.executable, str(SCRIPT)], cwd=tmp_path, env=env,
                          capture_output=True, text=True)


def test_build_script_lays_the_archive_out_byte_for_byte(archive, tmp_path):
    out = tmp_path / "dist"
    result = run_script(archive, tmp_path, out)
    assert result.returncode == 0, result.stderr
    assert "铺好" in result.stdout and "2 行地址" in result.stdout

    with tarfile.open(archive) as tar:
        names = tar.getnames()
        for name in names:
            laid = out / name
            assert laid.is_file(), f"没铺出来：{name}"
            assert laid.read_bytes() == tar.extractfile(name).read(), f"字节不同：{name}"
        # 输出目录里**只有**归档的东西（不多不少：构建不该往里塞别的东西）
        top = sorted({name.split("/", 1)[0] for name in names})
    assert sorted(entry.name for entry in out.iterdir()) == top


def test_build_script_refuses_a_broken_archive(archive, tmp_path):
    """自检不过 ⇒ 退出码 1、输出目录**一个文件都不建**（线上保持原样）。"""
    broken = tmp_path / "broken.tar.gz"
    with tarfile.open(archive, "r:gz") as src, tarfile.open(broken, "w:gz") as dst:
        for info in src.getmembers():
            body = src.extractfile(info).read() if info.isfile() else None
            if info.name == sm.MANIFEST_NAME:          # 抽掉 D145 的"包数据"
                import io
                import json
                manifest = json.loads(body)
                manifest.pop("albums")
                manifest.pop("characters")
                body = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
                info = tarfile.TarInfo(sm.MANIFEST_NAME)
                info.size = len(body)
                dst.addfile(info, io.BytesIO(body))
                continue
            dst.addfile(info, io.BytesIO(body) if body is not None else None)

    out = tmp_path / "dist"
    result = run_script(broken, tmp_path, out)
    assert result.returncode == 1
    assert "不铺" in result.stdout
    assert not out.exists()
