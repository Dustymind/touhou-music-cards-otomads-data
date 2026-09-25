"""本地曲库助手：给应用提供「本地专辑源」+ 音频文件（支持 Range / CORS）。

应用会向它请求两样东西：

1. ``GET /manifest.json`` —— 曲目表，形状与远端源表一致：``[专辑, 曲目, URL]`` 数组，
   外加 ``pack`` 标识（预留给多重架构：本地曲目属于哪个曲包），以及**本包自己的曲目表**
   （``albums`` + ``characters``，主仓库 D145：应用拿它代替随前端部署的那份自带数据 ⇒
   加曲目只动数据仓库）。**动态生成**：扫一遍曲库就知道有哪些曲目，URL 按请求的 Host 拼，
   所以换端口/加文件都不用重新生成。
2. ``GET /media/<专辑>/<曲目>.mp3`` —— 音频本体，支持 Range 与 CORS。

manifest 里的音频地址按**请求**现拼（所以换域名/端口不用重新生成）：优先取反向代理的
``X-Forwarded-Proto`` / ``X-Forwarded-Host``，其次取 ``Host``；两者都没有时才退回监听地址。
单端口部署（应用与曲库同源，见 `deploy/Caddyfile`）走的就是这条路，因此 https 站点也能拿到
``https://`` 的音频地址，不会触发混合内容拦截。显式覆盖用 ``[server].public_base_url`` 或 ``--public-base``。

为什么不能直接用 ``python3 -m http.server``：

* 应用用 ``fetch()`` 拉 manifest，会被 CORS 挡住（http.server 不发 ``Access-Control-Allow-Origin``）；
* ``http.server`` 不支持 Range，拖进度条会失效。

配置（默认读仓库根的 ``local-source.toml``，已被 .gitignore）：

.. code-block:: toml

    [server]
    host = "127.0.0.1"
    port = 8011
    port_tries = 10

    [library]
    root = "../.music"        # 相对配置文件所在目录

    [pack]
    id = "otomads"
    label_en = "Local album"
    label_zh = "本地专辑"

用法::

    uv run python -m otomads.local_source                 # 起服务
    uv run python -m otomads.local_source --print-url     # 只打印实际地址
    uv run python -m otomads.local_source --print-table   # 只打印曲目表 JSON
    uv run python -m otomads.local_source --root /mnt/music --port 8022
"""
from __future__ import annotations

import argparse
import http.server
import json
import os
import re
import socket
import socketserver
import sys
import tomllib
from urllib.parse import quote, unquote

from . import packformat
from . import paths as repo

DEFAULT_CONFIG = repo.ROOT / "local-source.toml"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8011
DEFAULT_TRIES = 10
DEFAULT_ROOT = repo.ROOT / ".music"
MANIFEST_PATH = "manifest.json"
AUDIO_EXTENSIONS = (".mp3",)
RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")


def load_config(path, *, host=None, port=None, root=None, pack_id=None,
                public_base=None) -> dict:
    """优先级：命令行 > local-source.toml > 内置默认值。"""
    cfg: dict = {}
    base_dir = repo.ROOT
    if path and os.path.exists(path):
        base_dir = os.path.dirname(os.path.abspath(path))
        with open(path, "rb") as fh:
            cfg = tomllib.load(fh)

    def resolve(value):
        if not value:
            return None
        return os.path.normpath(value if os.path.isabs(value) else os.path.join(base_dir, value))

    server = cfg.get("server", {})
    library = cfg.get("library", {})
    pack = cfg.get("pack", {})
    return {
        "config_file": path if path and os.path.exists(path) else None,
        "host": host or server.get("host", DEFAULT_HOST),
        "port": int(port if port is not None else server.get("port", DEFAULT_PORT)),
        "port_tries": int(server.get("port_tries", DEFAULT_TRIES)),
        "root": resolve(root) or resolve(library.get("root")) or str(DEFAULT_ROOT),
        # 反向代理可能既不发 X-Forwarded-*、Host 也不是对外域名 → 显式覆盖
        "public_base_url": ((public_base or server.get("public_base_url") or "").strip() or None),
        "pack_id": pack_id or pack.get("id", "otomads"),
        "pack_label_en": pack.get("label_en", "Local album"),
        "pack_label_zh": pack.get("label_zh", "本地专辑"),
    }


def library_files(root: str) -> list[tuple[str, str, str]]:
    """曲库 → `[(专辑, 曲目, 绝对路径), …]`：与 `scan_library` 同一套跳过规则，只是**带路径**。

    `build_manifest` 要拿它算 `revision`（名字 + 大小 + mtime，见 `packformat.media_revision`）；
    `scan_library` 的返回形状是给别处（与 `fetch_audio` 的口径一致）用的，不能动。
    """
    found: list[tuple[str, str, str]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if not name.startswith(".")]
        for name in filenames:
            if name.startswith(".") or not name.lower().endswith(AUDIO_EXTENSIONS):
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            album, _, filename = rel.rpartition("/")
            found.append((album, os.path.splitext(filename)[0], full))
    return sorted(found)


def scan_library(root: str) -> list[tuple[str, str]]:
    """曲库 → `[(专辑, 曲目), …]`。专辑 = 第一层目录名，曲目 = 去掉扩展名的文件名。

    **点开头的东西一律跳过**（目录与文件）：抓取器把下载的原件放在 `.raw/`、状态放在 `.state/`，
    它们都在曲库根下面；不跳过的话 manifest 会多出 `album = ".raw"` 的垃圾条目，
    界面上的条目数也就跟着错 ✗（契约见 `docs/packs-audio-v1.md` §2）。
    """
    return [(album, title) for album, title, _path in library_files(root)]


def media_path(album: str, title: str) -> str:
    """曲目 → ``/media/<专辑>/<曲目>.mp3``（非 ASCII 按 URL 规范编码）。"""
    return f"/media/{quote(album)}/{quote(title)}.mp3"


def build_manifest(root: str, base_url: str, pack_id: str,
                   loudness: str | None = None, snapshot: dict | None = None) -> dict:
    """曲库 → manifest（``[专辑, 曲目, 绝对地址]`` 行）。

    可选 ``loudness``：本源响度表的路径，**相对 manifest 自身**（例如 ``loudness/otomads.json``）。
    带上它 = "响度表跟着源部署"（主仓库 D139）：前端优先按这个地址取表，没声明才回落到应用侧那份
    （注册表里的 ``loudnessUrl``，相对数据集目录）。

    ⚠️ 本机助手**故意不声明**：它只发 ``/manifest.json`` 与 ``/media/*``，不发响度表
    （那份表在应用的 ``data/<模式>/loudness/`` 里，助手形态靠回落拿）。声明了却发不出来 = 增益失效，
    所以只有真把表打进同一份归档的 ``stage_media.pack`` 才传这个参数。

    ``revision``（主仓库 D144）：**音频本身**的版本号（名字+大小+mtime）。前端把它拼进媒体地址，
    于是"曲库变了但链接没变"也能立刻拿到新音频，而不是吃浏览器/CDN 的缓存。manifest 自己走
    ``Cache-Control: no-store``（见 `_send_manifest`）⇒ 每次都是最新的，前端因此拿得到新版本号。

    版本号写**两处**：行里的第 4 位（**逐曲** —— 只让变过的那几首换 URL，不牵动整包 321 MB），
    外加顶层的 ``revision``（整表兜底，给"只看顶层"的消费者）。两者都是同一套算法的输出。

    可选 ``snapshot``（主仓库 D145，C 路线）：曲包真源 → 的"包数据"段（``albums`` + ``characters``，
    :func:`otomads.packformat.pack_snapshot`），原样并进 manifest 的这两个**顶层**键。应用拿它代替
    随前端部署的那份自带曲目表 ⇒ 加曲目只动数据仓库。**不传这个参数时输出与改前逐字一致**
    （老调用方 / 老清单语义不变；前端没看到这两个键就照旧走自带那份兜底）。
    """
    files = library_files(root)
    rows: list[list[str]] = []
    for album, title, path in files:
        rows.append([album, title, base_url.rstrip("/") + media_path(album, title),
                     packformat.media_revision([(f"{album}/{title}", path)])])
    manifest = {
        "schema": 1,
        "pack": pack_id,
        "revision": packformat.media_revision([(f"{album}/{title}", path)
                                               for album, title, path in files]),
        "tracks": rows,
    }
    if loudness:
        manifest["loudness"] = loudness
    if snapshot:
        manifest["albums"] = snapshot["albums"]
        manifest["characters"] = snapshot["characters"]
    return manifest


def pack_snapshot_from_repo() -> dict | None:
    """本仓库曲包真源 → 清单里的「包数据」段；**没有曲包**（或目录是空的）⇒ ``None``（老形状）。

    实现在 :func:`otomads.packformat.repo_snapshot`（那里也解释了"在哪份里跑就带哪份的曲目表"）——
    这里只留一个与助手语义一致的名字。**每次请求现读**（35 个 TOML，几毫秒）⇒ 往 TOML 里加一首
    立刻反映到 manifest 上，不必重启助手（D145 §7.4 的验收就是这么走的）。
    """
    return packformat.repo_snapshot()


def find_bindable_port(host: str, port: int, tries: int) -> int | None:
    for offset in range(max(1, tries)):
        candidate = port + offset
        if candidate > 65535:
            break
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # 真正的服务器也开了 SO_REUSEADDR；探测时不设的话，端口上残留的 TIME_WAIT
        # 会让探测误判"被占用"，于是助手白白跳到 8012/8013（实测踩过）
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, candidate))
            return candidate
        except OSError:
            continue
        finally:
            probe.close()
    return None


class _LimitedReader:
    """只让 copyfile 读走 Range 指定的那一段。"""

    def __init__(self, handle, remaining: int) -> None:
        self.handle, self.remaining = handle, remaining

    def read(self, size: int = -1) -> bytes:
        if self.remaining <= 0:
            return b""
        if size is None or size < 0 or size > self.remaining:
            size = self.remaining
        data = self.handle.read(size)
        self.remaining -= len(data)
        return data

    def close(self) -> None:
        self.handle.close()


class LocalMusicHandler(http.server.SimpleHTTPRequestHandler):
    server_version = "TMC-LocalMusic/0.1"

    # ---- 基地址：按请求现拼，换域名/端口/协议都不必改任何表 ----
    @property
    def base_url(self) -> str:
        # 显式覆盖优先（代理没转发 Host/Proto 时用得上）
        configured = getattr(self.server, "public_base_url", None)  # type: ignore[attr-defined]
        if configured:
            return configured if configured.endswith("/") else configured + "/"
        # 反向代理：scheme 看 X-Forwarded-Proto（Caddy/nginx 默认会加，缺省按 http）
        proto = (self.headers.get("X-Forwarded-Proto") or "http").split(",")[0].strip() or "http"
        # host 看 X-Forwarded-Host，其次 Host，最后才用监听地址
        host = (self.headers.get("X-Forwarded-Host")
                or self.headers.get("Host")
                or f"{self.server.server_address[0]}:{self.server.server_address[1]}")  # type: ignore[attr-defined]
        return f"{proto}://{host.split(',')[0].strip()}/"

    def end_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Range")
        self.send_header("Access-Control-Expose-Headers",
                         "Content-Range, Content-Length, Accept-Ranges")
        super().end_headers()

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def translate_path(self, path: str) -> str:
        """URL 去掉 ``/media/`` 前缀并把百分号解码，中文/空格文件名照常命中。"""
        path = unquote(path.split("?", 1)[0].split("#", 1)[0], errors="surrogatepass")
        if path.startswith("/media/"):
            path = path[len("/media"):]
        return super().translate_path(path)

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        sys.stderr.write("  %s\n" % (fmt % args))

    # ---- manifest ----
    def _send_manifest(self, body: bool) -> None:
        payload = json.dumps(
            build_manifest(self.server.music_root, self.base_url, self.server.pack_id,  # type: ignore[attr-defined]
                           snapshot=pack_snapshot_from_repo()),
            ensure_ascii=False, indent=1).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if body:
            self.wfile.write(payload)

    def send_head(self):  # noqa: ANN201
        if unquote(self.path.split("?", 1)[0].lstrip("/")) == MANIFEST_PATH:
            self._send_manifest(body=self.command != "HEAD")
            return None

        raw_range = self.headers.get("Range")
        if not raw_range or self.command not in ("GET", "HEAD"):
            return super().send_head()

        matched = RANGE_RE.fullmatch(raw_range.strip())
        if not matched:
            return super().send_head()

        path = self.translate_path(self.path)
        if os.path.isdir(path):
            return super().send_head()
        try:
            handle = open(path, "rb")  # noqa: SIM115
        except OSError:
            self.send_error(404, "File not found")
            return None

        try:
            size = os.fstat(handle.fileno()).st_size
        except OSError:
            handle.close()
            return super().send_head()

        start_s, end_s = matched.group(1), matched.group(2)
        if start_s == "":
            start = max(0, size - int(end_s or 0))
            end = size - 1
        else:
            start = int(start_s)
            end = min(int(end_s) if end_s else size - 1, size - 1)

        if start >= size or start > end:
            handle.close()
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None

        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        if self.command == "HEAD":
            handle.close()
            return None
        handle.seek(start)
        return _LimitedReader(handle, end - start + 1)


class ThreadingServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def serve(conf: dict, *, strict_port: bool = False) -> int:
    if not os.path.isdir(conf["root"]):
        print(f"❌ 曲库目录不存在：{conf['root']}\n"
              f"   用 --root 指定，或在 {DEFAULT_CONFIG} 的 [library].root 里配置。", file=sys.stderr)
        return 2

    port = conf["port"]
    if not strict_port:
        found = find_bindable_port(conf["host"], port, conf["port_tries"])
        if found is None:
            print(f"❌ {conf['host']}:{port} 起连续 {conf['port_tries']} 个端口都不可用", file=sys.stderr)
            return 3
        port = found

    handler = lambda *a, **kw: LocalMusicHandler(*a, directory=conf["root"], **kw)  # noqa: E731
    try:
        httpd = ThreadingServer((conf["host"], port), handler)
    except OSError as exc:
        print(f"❌ 无法监听 {conf['host']}:{port}：{exc}", file=sys.stderr)
        return 3

    httpd.music_root = conf["root"]              # type: ignore[attr-defined]
    httpd.pack_id = conf["pack_id"]              # type: ignore[attr-defined]
    httpd.public_base_url = conf["public_base_url"]  # type: ignore[attr-defined]
    tracks = scan_library(conf["root"])
    base_url = f"http://{conf['host']}:{port}/"
    if port != conf["port"]:
        print(f"⚠️  端口 {conf['port']} 被占用，已改用 {port}；应用侧填 {base_url}")
    print(f"配置文件  : {conf['config_file'] or '(未使用，取默认值)'}")
    print(f"曲库目录  : {conf['root']}（{len(tracks)} 首）")
    print(f"曲包      : {conf['pack_id']}")
    print(f"manifest  : {base_url}{MANIFEST_PATH}")
    if conf["public_base_url"]:
        print(f"对外基地址: {conf['public_base_url']}（manifest 里的音频地址按它拼）")
    else:
        print("音频地址  : 按请求的 X-Forwarded-Proto/Host（单端口反代部署）或 Host 现拼")
    print("Ctrl-C 停止。\n")
    with httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n已停止。")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    ap.add_argument("--host")
    ap.add_argument("--port", type=int)
    ap.add_argument("--root")
    ap.add_argument("--pack")
    ap.add_argument("--public-base", help="对外基地址（反向代理未转发 Host/Proto 时用），如 https://example.com/music/")
    ap.add_argument("--strict-port", action="store_true")
    ap.add_argument("--print-url", action="store_true")
    ap.add_argument("--print-table", action="store_true")
    args = ap.parse_args(argv)

    conf = load_config(args.config, host=args.host, port=args.port,
                       root=args.root, pack_id=args.pack, public_base=args.public_base)
    port = conf["port"] if args.strict_port else (
        find_bindable_port(conf["host"], conf["port"], conf["port_tries"]) or conf["port"])
    base_url = f"http://{conf['host']}:{port}/"
    if args.print_url:
        print(base_url)
        return 0
    if args.print_table:
        print(json.dumps(build_manifest(conf["root"], base_url, conf["pack_id"],
                                       snapshot=pack_snapshot_from_repo()),
                         ensure_ascii=False, indent=1))
        return 0
    return serve(conf, strict_port=args.strict_port)


if __name__ == "__main__":
    raise SystemExit(main())
