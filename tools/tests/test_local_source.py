"""本地曲库助手的测试：manifest 形状、Range、CORS、端口回退。"""
from __future__ import annotations

import json
import os
import socket
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from otomads import local_source, packformat


@pytest.fixture()
def library(tmp_path):
    (tmp_path / "otomads").mkdir()
    (tmp_path / "otomads" / "thwy - 岁月.mp3").write_bytes(b"ID3" + bytes(range(256)) * 4)
    (tmp_path / "测试专辑").mkdir()
    (tmp_path / "测试专辑" / "01. 曲目.mp3").write_bytes(b"ID3" + b"\x00" * 999)
    (tmp_path / "readme.txt").write_text("not audio", encoding="utf-8")
    return tmp_path


@pytest.fixture()
def server(library, tmp_path, monkeypatch):
    # 助手每次请求都会读**本仓库**的 `packs/`（D145 的曲目表）。这些用例只验清单形状 / `Range` /
    # 缓存头，与曲目表无关 ⇒ 把 `DATA` 指到一个**没有 packs/** 的临时根：既不依赖真数据，
    # 也不受它当前是什么形状影响（`test_served_manifest_carries_the_pack_snapshot` 自己会指回去）。
    monkeypatch.setattr(local_source.packformat.repo, "DATA", tmp_path / "no-packs")
    handler = lambda *a, **kw: local_source.LocalMusicHandler(  # noqa: E731
        *a, directory=str(library), **kw)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.music_root = str(library)      # type: ignore[attr-defined]
    httpd.pack_id = "otomads"            # type: ignore[attr-defined]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def test_scan_library_ignores_non_audio(library):
    assert local_source.scan_library(str(library)) == [
        ("otomads", "thwy - 岁月"), ("测试专辑", "01. 曲目")]


def test_manifest_shape_and_urls(server):
    with urllib.request.urlopen(f"{server}/manifest.json") as resp:
        payload = json.loads(resp.read().decode("utf-8"))
        assert resp.headers["Access-Control-Allow-Origin"] == "*"
    assert payload["schema"] == 1 and payload["pack"] == "otomads"
    assert [row[:2] for row in payload["tracks"]] == [
        ["otomads", "thwy - 岁月"], ["测试专辑", "01. 曲目"]]
    assert payload["tracks"][0][2].startswith(server) and payload["tracks"][0][2].endswith(".mp3")


def test_range_request_returns_206(server):
    url = f"{server}/media/otomads/{urllib.parse.quote('thwy - 岁月')}.mp3"
    req = urllib.request.Request(url, headers={"Range": "bytes=0-99"})
    with urllib.request.urlopen(req) as resp:
        body = resp.read()
        assert resp.status == 206
        assert resp.headers["Content-Range"].startswith("bytes 0-99/")
        assert resp.headers["Accept-Ranges"] == "bytes"
        assert resp.headers["Access-Control-Allow-Origin"] == "*"
    assert len(body) == 100 and body[:3] == b"ID3"


def test_suffix_range_and_bad_range(server):
    url = f"{server}/media/{urllib.parse.quote('测试专辑')}/{urllib.parse.quote('01. 曲目')}.mp3"
    req = urllib.request.Request(url, headers={"Range": "bytes=-10"})
    with urllib.request.urlopen(req) as resp:
        assert resp.status == 206 and len(resp.read()) == 10
    req = urllib.request.Request(url, headers={"Range": "bytes=99999-"})
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(req)
    assert excinfo.value.code == 416


def test_only_the_whitelist_is_served(server, library):
    """白名单 + **只解码一次**：`/media/%252e%252e/…` 不许爬出 `/media/`（回归用例）。

    父类 `translate_path` 会**再解一次** ⇒ 从前 `/media/%252e%252e/local-source.toml` 能读走曲库根下
    任何文件、`/media/%252e%252e/` 还能列目录（2026-09-29 健壮性自查里实测复现）。现在：白名单 +
    空段 / `.` / `..` / 点开头段 / 反斜杠 / NUL 一律 404，目录（含 `/media/`）也 404、不列。
    """
    (library / "local-source.toml").write_text('[library]\nroot = "."\n', encoding="utf-8")
    (library / ".state").mkdir()
    (library / ".state" / "demo.json").write_text("{}", encoding="utf-8")

    for path in ("/media/%252e%252e/local-source.toml",
                 "/media/%252e%252e/.state/demo.json",
                 "/media/%252e%252e/",
                 "/media/%2e%2e/readme.txt",
                 "/media/../readme.txt",
                 "/readme.txt",
                 "/media/",
                 "/media/otomads/"):
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(server + path)
        assert excinfo.value.code == 404, path

    # 白名单里的那份照常 200 —— 回归不能把正常路径一起挡了
    url = f"{server}/media/otomads/{urllib.parse.quote('thwy - 岁月')}.mp3"
    with urllib.request.urlopen(url) as resp:
        assert resp.status == 200 and resp.read(3) == b"ID3"


def test_port_fallback_picks_free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    busy = sock.getsockname()[1]
    try:
        found = local_source.find_bindable_port("127.0.0.1", busy, 5)
        assert found is not None and found != busy
    finally:
        sock.close()


def test_config_priority(tmp_path, library):
    cfg = tmp_path / "local-source.toml"
    # 路径用 `json.dumps` 渲染：JSON 的字符串转义口径与 TOML 基本字符串一致。
    # 直接 f-string 插进去的话，Windows 的 `C:\Users\…` 里 `\U` 会被 tomllib 当成
    # Unicode 转义（`TOMLDecodeError: Invalid hex value`）—— 纯粹是**替身**写法的问题。
    cfg.write_text(
        f'[server]\nhost = "127.0.0.1"\nport = 8123\n[library]\n'
        f'root = {json.dumps(str(library))}\n[pack]\nid = "my-pack"\n', encoding="utf-8")
    conf = local_source.load_config(str(cfg))
    assert conf["port"] == 8123 and conf["root"] == str(library) and conf["pack_id"] == "my-pack"
    overridden = local_source.load_config(str(cfg), port=9000, pack_id="other")
    assert overridden["port"] == 9000 and overridden["pack_id"] == "other"


import urllib.parse  # noqa: E402  (供上面的 quote 使用)


# --------------------------------------------- 媒体版本号（D144 / §2.6）

def test_manifest_carries_per_track_content_revisions(library):
    """清单带**逐曲**版本号（文件的**内容**哈希，§2.6）：音频变了但链接没变时，前端靠换 URL 绕开缓存。

    整表 ``revision`` 同时给一份（给"只看顶层"的消费者），两者都是同一套算法的输出 ——
    值就是归档侧的 :func:`packformat.content_revision`（不再有第二套 mtime 口径）。
    """
    files = local_source.library_files(str(library))
    entries = [(f"{album}/{title}", path) for album, title, path in files]
    hashes = {name: packformat.content_revision(path) for name, path in entries}
    first = local_source.build_manifest(str(library), "http://127.0.0.1:8011", "otomads")

    assert len(first["revision"]) == 16
    assert all(len(row) == 4 and len(row[3]) == 16 for row in first["tracks"])
    assert {(row[0], row[1]): row[3] for row in first["tracks"]} == {
        (album, title): hashes[f"{album}/{title}"] for album, title, _path in files}
    assert first["revision"] == packformat.revisions_of(
        [(name, hashes[name]) for name, _path in entries])

    # 文件没动 ⇒ 再算一次**逐字节一样**（可复现；前端因此不会白白换 URL 让所有客户端重下）
    again = local_source.build_manifest(str(library), "http://127.0.0.1:8011", "otomads")
    assert json.dumps(again, ensure_ascii=False) == json.dumps(first, ensure_ascii=False)

    victim = library / "otomads" / "thwy - 岁月.mp3"
    stamp = victim.stat()
    os.utime(victim, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 1_000_000))
    touched = local_source.build_manifest(str(library), "http://127.0.0.1:8011", "otomads")

    # **只动 mtime 不算内容变**：版本号一个字不动（§2.6 —— 文件相同的两台机器因此对得上）
    assert touched == first

    # 改一个字节（大小不变、mtime 变）⇒ **只有那一首**换版本（逐曲的意义：不然整包 321 MB 全部重下）
    body = bytearray(victim.read_bytes())
    body[8] ^= 0xFF
    victim.write_bytes(bytes(body))
    stamp = victim.stat()
    os.utime(victim, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 5_000_000))   # 与上面那次 mtime 必然不同
    changed = local_source.build_manifest(str(library), "http://127.0.0.1:8011", "otomads")

    moved = [index for index, (a, b) in enumerate(zip(first["tracks"], changed["tracks"])) if a != b]
    assert moved == [0], moved                      # 只有 `otomads/thwy - 岁月`（排序第一）那一行
    assert changed["revision"] != first["revision"]
    # 变的只有版本号，地址本身一个字没动 —— 缓存就是靠这一位失效的
    assert [row[2] for row in first["tracks"]] == [row[2] for row in changed["tracks"]]


def test_build_manifest_reuses_cached_content_hashes(library, monkeypatch):
    """助手每次请求都出清单 ⇒ 内容哈希走**进程级缓存**：文件没变就不重读那 ~300 MB（§2.6）。

    缓存键是文件的同一性 ``(绝对路径, st_size, st_mtime_ns)``：只动 mtime ⇒ 那一首重算一次
    （值仍是内容哈希），其余仍命中。
    """
    monkeypatch.setattr(packformat, "_CONTENT_REVISION_CACHE", {})
    calls: list[str] = []
    original = packformat.content_revision

    def counting(path):
        calls.append(str(path))
        return original(path)

    monkeypatch.setattr(packformat, "content_revision", counting)
    local_source.build_manifest(str(library), "http://127.0.0.1:8011", "otomads")
    assert len(calls) == 2                      # 冷缓存：两个音频各哈希一次
    local_source.build_manifest(str(library), "http://127.0.0.1:8011", "otomads")
    assert len(calls) == 2                      # 第二次一次都没重读（键命中）

    victim = library / "otomads" / "thwy - 岁月.mp3"
    stamp = victim.stat()
    os.utime(victim, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 1_000_000))
    local_source.build_manifest(str(library), "http://127.0.0.1:8011", "otomads")
    assert len(calls) == 3                      # mtime 变了 ⇒ 只有那一首重算，另一首仍命中


def test_served_manifest_is_never_cached_and_carries_revision(server, library):
    """助手发 manifest 必须**不缓存**（``no-store``）—— 前端就是靠每次拿到最新的版本号。"""
    with urllib.request.urlopen(f"{server}/manifest.json") as resp:
        payload = json.loads(resp.read().decode("utf-8"))
        assert resp.headers["Cache-Control"] == "no-store"
    assert len(payload["revision"]) == 16
    assert all(len(row) == 4 for row in payload["tracks"])


# --------------------------------------------- 曲目表跟着源走（D145，C 路线）

def test_build_manifest_without_a_snapshot_stays_unchanged(library):
    """老调用方（**不传** ``snapshot``）的输出逐字不变：键、顺序、行形状都与改前一致。

    "多两个顶层键"必须只发生在**显式要曲目表**的调用上 —— 否则老清单 / 老前端会看到没约定的东西。
    """
    manifest = local_source.build_manifest(str(library), "http://127.0.0.1:8011", "otomads")
    assert list(manifest) == ["schema", "pack", "revision", "tracks"]   # 连插入顺序都不变（JSON 文本才不变）
    assert manifest["tracks"][0][:2] == ["otomads", "thwy - 岁月"]
    assert manifest["tracks"][0][2] == "http://127.0.0.1:8011" + local_source.media_path(
        "otomads", "thwy - 岁月")
    assert "albums" not in json.dumps(manifest) and "characters" not in json.dumps(manifest)


def test_served_manifest_carries_the_pack_snapshot(server, library, tmp_path, monkeypatch):
    """助手发的 manifest 带**本包自己的曲目表**（``albums`` + ``characters``，D145）。

    **曲目表按曲包 TOML、地址按磁盘文件** —— 同一份数据的两种视图：磁盘上是 `作者 - 标题.mp3`，
    TOML 里作者与标题是分开的两个字段（D95/D96 的口径不能改；应用按归一化曲名把两边配上）。
    `card` / `covers` 这两张可选覆写表也跟着走（应用侧按角色 key 取图）：`cover` 在 TOML 里
    写在**每条** `[[track]]` 里（D153），值是**裸原图直链**（不加任何图床后缀 / 尺寸参数），
    到这里已经拼成"与 `music` 同序"的数组 —— **线上形状没变**。
    """
    packs_dir = tmp_path / "packs"
    (packs_dir / "otomads").mkdir(parents=True)
    (packs_dir / "otomads.toml").write_text(
        '[pack]\nid = "otomads"\n\n[[album]]\nkey = "otomads"\nname = "otomads"\n'
        'kind = "other"\npack = "otomads"\norder = 100\nshow_album_name = false\n',
        encoding="utf-8")
    cover = "https://i0.hdslb.com/bfs/archive/88ad053c21de0ce0eab53e56561c2c6dadc79e36.jpg"
    (packs_dir / "otomads" / "cirno.toml").write_text(
        f'key = "cirno"\ncard = ["チルノ-mad.png"]\n\n[[track]]\nalbum = "otomads"\n'
        f'author = "thwy"\ntitle = "岁月"\nextra = "角色曲"\ncover = "{cover}"\n', encoding="utf-8")
    monkeypatch.setattr(local_source.packformat.repo, "DATA", tmp_path)
    monkeypatch.setattr(local_source.packformat, "_SNAPSHOT_CACHE", {})   # 别让别的用例的缓存顶上

    with urllib.request.urlopen(f"{server}/manifest.json") as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    assert payload["pack"] == "otomads"
    assert [row[1] for row in payload["tracks"]] == ["thwy - 岁月", "01. 曲目"]
    assert payload["albums"] == [{"key": "otomads", "name": "otomads", "kind": "other",
                                  "pack": "otomads", "order": 100, "showAlbumName": False}]
    assert payload["characters"] == [
        {"key": "cirno", "music": [["otomads", "岁月", "角色曲", "thwy"]],
         "card": ["チルノ-mad.png"], "covers": [cover]}]


def test_served_manifest_without_packs_falls_back_to_the_old_shape(server, library, tmp_path,
                                                                   monkeypatch):
    """没有曲包真源（独立跑时指到别处 / 目录空）⇒ 清单退回老形状，**不报错**（前端照旧走兜底）。"""
    monkeypatch.setattr(local_source.packformat.repo, "DATA", tmp_path / "empty")
    with urllib.request.urlopen(f"{server}/manifest.json") as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    assert "albums" not in payload and "characters" not in payload
    assert len(payload["tracks"]) == 2
