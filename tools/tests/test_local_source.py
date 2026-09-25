"""本地曲库助手的测试：manifest 形状、Range、CORS、端口回退。"""
from __future__ import annotations

import json
import os
import socket
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from otomads import local_source


@pytest.fixture()
def library(tmp_path):
    (tmp_path / "otomads").mkdir()
    (tmp_path / "otomads" / "thwy - 岁月.mp3").write_bytes(b"ID3" + bytes(range(256)) * 4)
    (tmp_path / "测试专辑").mkdir()
    (tmp_path / "测试专辑" / "01. 曲目.mp3").write_bytes(b"ID3" + b"\x00" * 999)
    (tmp_path / "readme.txt").write_text("not audio", encoding="utf-8")
    return tmp_path


@pytest.fixture()
def server(library):
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
    cfg.write_text(
        f'[server]\nhost = "127.0.0.1"\nport = 8123\n[library]\nroot = "{library}"\n'
        '[pack]\nid = "my-pack"\n', encoding="utf-8")
    conf = local_source.load_config(str(cfg))
    assert conf["port"] == 8123 and conf["root"] == str(library) and conf["pack_id"] == "my-pack"
    overridden = local_source.load_config(str(cfg), port=9000, pack_id="other")
    assert overridden["port"] == 9000 and overridden["pack_id"] == "other"


import urllib.parse  # noqa: E402  (供上面的 quote 使用)


# --------------------------------------------- 媒体版本号（D144）

def test_manifest_carries_per_track_media_revisions(library):
    """清单带**逐曲**版本号（名字+大小+mtime）：音频变了但链接没变时，前端靠换 URL 绕开缓存。

    整表 ``revision`` 同时给一份（给"只看顶层"的消费者），两者都是同一套算法的输出。
    """
    first = local_source.build_manifest(str(library), "http://127.0.0.1:8011", "otomads")
    assert len(first["revision"]) == 16
    assert all(len(row) == 4 and len(row[3]) == 16 for row in first["tracks"])

    # 文件没动 ⇒ 再算一次**一模一样**（可复现；前端因此不会白白换 URL 让所有客户端重下）
    assert local_source.build_manifest(str(library), "http://127.0.0.1:8011", "otomads") == first

    victim = library / "otomads" / "thwy - 岁月.mp3"
    stamp = victim.stat()
    os.utime(victim, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 1_000_000))
    second = local_source.build_manifest(str(library), "http://127.0.0.1:8011", "otomads")

    # **只有动过的那一首换版本**（逐曲的意义：不然整包 321 MB 会全部重下）
    changed = [index for index, (a, b) in enumerate(zip(first["tracks"], second["tracks"])) if a != b]
    assert len(changed) == 1, changed
    assert second["revision"] != first["revision"]
    # 变的只有版本号，地址本身一个字没动 —— 缓存就是靠这一位失效的
    assert [row[2] for row in first["tracks"]] == [row[2] for row in second["tracks"]]


def test_served_manifest_is_never_cached_and_carries_revision(server, library):
    """助手发 manifest 必须**不缓存**（``no-store``）—— 前端就是靠每次拿到最新的版本号。"""
    with urllib.request.urlopen(f"{server}/manifest.json") as resp:
        payload = json.loads(resp.read().decode("utf-8"))
        assert resp.headers["Cache-Control"] == "no-store"
    assert len(payload["revision"]) == 16
    assert all(len(row) == 4 for row in payload["tracks"])
