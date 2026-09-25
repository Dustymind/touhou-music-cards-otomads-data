"""把音MAD 素材整理成**可静态部署**的两步：`pack`（打成一个归档）与 `stage`（铺进 `dist/`）。

用法::

    # ① 本地打一个可发布的归档（放到 GitHub Release 资产 / 对象存储都行）
    uv run python -m otomads.stage_media pack --library <曲库> --out otomads-media.tar.gz [--cards <卡面目录>]

    # ② 构建时铺进 dist（从归档拉，或直接从本地曲库铺）
    uv run python -m otomads.stage_media stage --archive <URL|路径> --out dist [--base https://host/sub/]
    uv run python -m otomads.stage_media stage --from <曲库> --out dist

为什么要它（主仓库 D138）：应用本身是**纯静态**可部署的，唯一缺的是音MAD 的素材 ——
音频（86 首 / 约 324 MB）与音MAD 卡面 **都不进任何仓库**。做法是：

* ``pack``：按**部署布局**把 ``<曲库>/<专辑>/*.mp3``（与可选卡面）打进归档，并在归档里放一份
  **相对地址**的 ``manifest.json``（``media/otomads/<文件>.mp3``）⇒ 归档与部署基地址**无关**，
  同一份归档在域名根与子目录（GitHub Pages 项目页 ``user.github.io/<repo>/``）下都能用；
  CI 里因此可以只写一行 ``curl … | tar -xz -C dist``（**不需要 Python**）。
* ``stage``：把归档铺进 ``dist/``（构建时从 Release 资产拉），或直接从本地曲库铺；
  要绝对地址就 ``--base``（manifest 里媒体地址写成绝对，用于 manifest 与媒体不同源的场合）。

归档布局（解出来就是部署根）::

    manifest.json                 # {"schema":1,"pack":"otomads","revision":…,
                                  #  "tracks":[[专辑, 曲目, 地址, 版本], …],
                                  #  "albums":[…],"characters":[…],   ← 本包自己的曲目表（D145）
                                  #  "loudness":"loudness/otomads.json"}（有表才写这个键）
    media/otomads/<文件>.mp3       # 文件名 = 磁盘文件名（`作者 - 标题.mp3`），**不能改**
    loudness/otomads.json         # 本源响度表：**跟着源走**（D139），路径与 manifest 里声明的一致
    cards-otomads/<文件>           # 可选：音MAD 图集（`local_only`，只能同源 `./cards-otomads/`）

口径（照抄助手与前端，**别在这里另立一套**）：

* 曲目名 = **磁盘文件名的 stem**（带 `作者 - 标题` 前缀）；前端 `resolveTrack` 用归一化后缀兜底匹配；
* 媒体地址复用 ``local_source.media_path`` 的同一套 ``quote``（去掉前导 ``/`` 即相对地址）；
* `tracks` 行形状与助手 ``local_source.build_manifest`` 完全一致；
* 响度表路径写在 manifest 的 ``loudness`` 键里、**相对 manifest 自身**（前端优先按它取表，没声明才
  回落到应用侧那份 —— 见主仓库 D139）。表由本仓库的 ``measure_loudness`` / ``fetch_audio`` 生成，
  路径取源注册表里 ``loudness`` 声明的那个（默认 ``loudness/otomads.json``）。
* **曲目表也写在 manifest 里**（``albums`` / ``characters``，主仓库 D145）：**曲目**来自曲包 TOML
  （``<仓库根>/packs/``），**地址**来自磁盘文件 —— 应用拿它代替随前端部署的那份自带数据，
  于是加曲目只动本仓库 + 铺源。曲库里多一个没有曲包条目的 mp3 时：清单行数会多（源状态那一行按行数），
  但**曲目表以曲包为准**（那一首选不到 —— 这正是 C 之前的老症状，现在它是有意的）。

**幂等、可重复**：同一份曲库打两次，归档逐字节相同（tar 成员按名排序、mtime/uid/gid 归零、gzip mtime 归零）。
"""
from __future__ import annotations

import argparse
import gzip
import json
import pathlib
import shutil
import tarfile
import tempfile
import urllib.request
from urllib.parse import unquote

from . import local_source as ls
from . import packformat
from . import paths as repo

#: 归档里的固定文件名（与助手 ``MANIFEST_PATH`` 同名：部署根的那一份就是给前端取的）
MANIFEST_NAME = ls.MANIFEST_PATH
#: 部署布局里的两个目录
MEDIA_DIR = "media"
CARDS_DIR = "cards-otomads"
#: 默认专辑目录名（= 曲包 id；前端按 (专辑, 曲目) 匹配，所以这个名字不能换）
DEFAULT_ALBUM = "otomads"
#: 认得的归档后缀（只支持 tar 家族：标准库能读，不用额外依赖）
ARCHIVE_SUFFIXES = (".tar.gz", ".tgz", ".tar")


# ------------------------------------------------------------------ 扫描与 manifest

def audio_files(directory: pathlib.Path) -> dict[str, pathlib.Path]:
    """目录里的音频 → ``{stem: 路径}``（跳过点开头的文件、只看 ``AUDIO_EXTENSIONS``）。

    只扫**这一层**：``pack`` 只打一个专辑目录，``stage`` 校验时也只面对铺好的
    ``media/<专辑>/``（布局由本模块保证是平的）。
    """
    found: dict[str, pathlib.Path] = {}
    if not directory.is_dir():
        return found
    for path in sorted(directory.iterdir()):
        if (path.name.startswith(".") or not path.is_file()
                or not path.name.lower().endswith(ls.AUDIO_EXTENSIONS)):
            continue
        found[path.stem] = path
    return found


def library_tracks(library: pathlib.Path, album: str) -> dict[str, pathlib.Path]:
    """``<曲库>/<专辑>/`` 下的音频；空或不存在时给出可执行的报错。"""
    if not library.is_dir():
        raise SystemExit(f"❌ 曲库目录不存在：{library}")
    tracks = audio_files(library / album)
    if not tracks:
        raise SystemExit(f"❌ 曲库里没有专辑 {album!r} 的音频：{library / album}"
                         f"（曲库结构是 <root>/<专辑>/<曲目>.mp3）")
    return tracks


def media_url(album: str, title: str, base: str | None = None) -> str:
    """曲目 → 媒体地址：默认**相对**（``media/<专辑>/<曲目>.mp3``），给了 ``base`` 就是绝对。

    相对地址是"归档与部署基地址无关"的关键：前端把解析出的 URL 直接赋给 ``audio.src``，
    相对地址按**页面**解析 ⇒ 域名根与子目录部署都不用重新生成归档。
    """
    path = ls.media_path(album, title)          # /media/<专辑>/<曲目>.mp3（同一套 quote）
    return f"{base.rstrip('/')}{path}" if base else path.lstrip("/")


def build_manifest(titles: list[str], album: str = DEFAULT_ALBUM,
                   pack_id: str | None = None, base: str | None = None,
                   loudness: str | None = None, revisions: dict[str, str] | None = None,
                   revision: str | None = None, snapshot: dict | None = None) -> dict:
    """``{"schema":1,"pack":…,"revision":…,"tracks":[[专辑, 曲目, 地址, 版本], …],"loudness":…}``
    （行形状与助手一致；第 4 位是逐曲版本号，顶层 `revision` 是整表版本号）。

    ``loudness`` **相对 manifest 自身**（见模块 docstring 与主仓库 D139）；不给就不写这个键。

    ``revisions`` / ``revision``（主仓库 D144）：**音频本身**的版本号，由调用方算好传进来 ——
    归档侧是内容哈希（`packformat.content_revision`，D149），本机助手侧仍是 mtime
    （`packformat.media_revision`）。行里的前三项与助手**逐字一致**，第 4 位与顶层键才是新增的
    —— 它们只进清单，不改地址。前端把版本拼进媒体地址：音频变了但链接没变时 URL 会跟着变
    ⇒ 不吃浏览器/CDN 的缓存；而且**逐曲**的写法只让变过的那几首换 URL，不会让整包 321 MB 全部重下。

    ``snapshot``（主仓库 D145，C 路线）：曲包真源 → 的"包数据"段（`albums` + `characters`，
    `packformat.pack_snapshot`）。给了就并进 manifest 的这两个**顶层**键 —— 静态源与助手从此
    都自带"这个包有哪些曲目"，应用因此不必为加一首曲目重新部署。**不给 ⇒ 输出与改前逐字一致**
    （老调用方 / 老清单语义不变）。
    """
    rows: list[list[str]] = []
    for title in titles:
        row = [album, title, media_url(album, title, base)]
        if revisions and title in revisions:
            row.append(revisions[title])
        rows.append(row)
    manifest = {"schema": 1, "pack": pack_id or album, "tracks": rows}
    if revision:
        manifest["revision"] = revision
    if loudness:
        manifest["loudness"] = loudness
    if snapshot:
        manifest["albums"] = snapshot["albums"]
        manifest["characters"] = snapshot["characters"]
    return manifest


def declared_loudness(pack_id: str = DEFAULT_ALBUM) -> tuple[str, pathlib.Path] | None:
    """本源在注册表里声明的响度表 → ``(相对 manifest 的路径, 磁盘路径)``；没声明就是 ``None``。

    路径直接用注册表里写的那个（`loudness/otomads.json`）——**声明与实际放进归档的位置必须一致**，
    否则前端按声明取会 404。找不到注册表 / 没写 `loudness` 键 ⇒ `None`（归档里不带表，前端回落）。
    """
    path = packformat.loudness_path(pack_id)
    if path is None:
        return None
    try:
        return (path.relative_to(repo.DATA).as_posix(), path)
    except ValueError:                      # 注册表把表写到仓库外了（不该发生）
        return None


def render(manifest: dict) -> str:
    """manifest → 文本（``indent`` + 不转义中文，diff 才看得懂）。"""
    return json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


# ------------------------------------------------------------------ pack

def _add_file(archive: tarfile.TarFile, path: pathlib.Path, arcname: str) -> None:
    """加一个文件，**元数据归零**（mtime/uid/gid/权限固定）⇒ 同一份素材打两次逐字节相同。"""
    info = archive.gettarinfo(str(path), arcname=arcname)
    info.mtime = 0
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.mode = 0o644
    with open(path, "rb") as handle:
        archive.addfile(info, handle)


def _write_archive(root: pathlib.Path, out: pathlib.Path) -> None:
    """把拼好的目录树打成 ``.tar.gz``（gzip 的 mtime 与**文件名**都去掉，归档才可复现）。

    `filename=""` 不是多余的：`GzipFile` 默认会把输出文件名写进 gzip 头的 FNAME 字段，
    于是"同一份素材打到不同路径"就会得到不同的字节（踩过）。
    """
    with open(out, "wb") as raw:
        with gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as gz:
            with tarfile.open(fileobj=gz, mode="w|") as archive:
                for path in sorted(root.rglob("*")):
                    if path.is_file():
                        _add_file(archive, path, str(path.relative_to(root)))


def pack(library: pathlib.Path, out: pathlib.Path, album: str = DEFAULT_ALBUM,
         cards: pathlib.Path | None = None) -> dict:
    """打一个可发布的归档（布局 = 部署根，**含本源响度表**），返回摘要。"""
    tracks = library_tracks(library, album)
    table = declared_loudness(album)
    if table is not None and not table[1].is_file():
        print(f"⚠️  注册表声明的响度表不存在，归档里不带它：{table[1]}"
              f"（跑 `uv run --project tools python -m otomads.measure_loudness` 生成；"
              f"前端会回落到应用侧那份）")
        table = None
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        media = root / MEDIA_DIR / album
        media.mkdir(parents=True)
        for title, path in sorted(tracks.items()):
            shutil.copyfile(path, media / f"{title}{path.suffix}")
        _copy_loudness(root, table)
        copied_cards = _copy_cards(cards, root / CARDS_DIR)
        _assemble(root, tracks, table, album, None, out)
    return {"archive": out, "tracks": len(tracks), "cards": copied_cards,
            "bytes": out.stat().st_size, "loudness": 1 if table else 0}


# ------------------------------------------------------------------ stage

def _safe_members(archive: tarfile.TarFile) -> list[tarfile.TarInfo]:
    """挑出可安全解开的成员：**拒绝**绝对路径、``..``、以及符号链接/硬链接/设备等特殊成员。

    归档可以来自网络（Release 资产），所以这里按"不可信输入"处理：不守着就会写到
    ``dist/`` 之外（tar 的经典路径穿越）。
    """
    members: list[tarfile.TarInfo] = []
    for member in archive.getmembers():
        name = pathlib.PurePosixPath(member.name)
        if name.is_absolute() or ".." in name.parts or member.name.startswith("/"):
            raise SystemExit(f"❌ 归档里有不安全的路径：{member.name}")
        if member.issym() or member.islnk() or member.isdev():
            raise SystemExit(f"❌ 归档里有不支持的成员类型（链接/设备）：{member.name}")
        if member.isfile() or member.isdir():
            members.append(member)
    return members


def extract(archive_path: pathlib.Path, out: pathlib.Path) -> None:
    """把归档解到 `out`（= 部署根）——**按不可信输入处理**：拒绝对路径、`..`、链接/设备。

    归档可能来自网络（Release 资产 / 对象存储），所以这条路是**唯一**该用来铺盘的地方：
    手写 `tarfile.extractall` 会踩经典的路径穿越。`stage` 与 CF Pages 的构建脚本都走它。
    """
    with tarfile.open(archive_path, "r:*") as archive:
        out.mkdir(parents=True, exist_ok=True)
        members = _safe_members(archive)
        try:
            archive.extractall(out, members=members, filter="data")   # 3.12+：顺带用官方过滤器
        except TypeError:                                            # 3.11 还没有 filter 参数
            archive.extractall(out, members=members)


def download_archive(url: str, target: pathlib.Path) -> pathlib.Path:
    """取归档到 `target`：认 `file://` 与 http(s)（跟随重定向、走环境里的 `http(s)_proxy`）。

    `stage --archive` 与 Cloudflare 的构建入口（`tools/build_cdn_site.py`）共用这一份 ——
    以前两边各写一遍（差一个 `file://`、差一个 120 秒超时）。
    """
    print(f"⬇️  取归档：{url}")
    if url.startswith("file://"):
        shutil.copyfile(url[len("file://"):], target)
    else:
        with urllib.request.urlopen(url, timeout=120) as response, open(target, "wb") as handle:
            shutil.copyfileobj(response, handle)
    print(f"    落盘 {target}（{target.stat().st_size} B）")
    return target


def _copy_loudness(root: pathlib.Path, table) -> None:
    """响度表**跟着源走**：放进归档的位置 = 注册表声明的位置（manifest 里写的也是它）。"""
    if table is None:
        return
    target = root / table[0]
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(table[1], target)


def _copy_cards(cards: pathlib.Path | None, target: pathlib.Path) -> int:
    """可选的音MAD 卡面目录 → `cards-otomads/`（跳过点开头的文件）；返回拷了几张。"""
    if cards is None or not cards.is_dir():
        return 0
    target.mkdir(parents=True, exist_ok=True)
    copied = 0
    for path in sorted(cards.iterdir()):
        if path.is_file() and not path.name.startswith("."):
            shutil.copyfile(path, target / path.name)
            copied += 1
    return copied


def _assemble(root: pathlib.Path, tracks: dict[str, pathlib.Path], table, album: str,
              pack_id: str | None, out: pathlib.Path) -> dict:
    """把 `tracks` 写成 manifest、再把 `root` 打成归档 —— **`pack` 与 `repack` 共用这一段**。

    以前两边各抄一份（11–14 行逐字同形），而这里正是"本机 `pack` == CI `repack` 逐字节相同"那条
    性质的所在地（`tools/tests/test_repack.py` 盯着）：抄两份等于白留一个漂移面。
    逐曲版本号用**内容**哈希（D149）且**只算一次** —— 整表版本号吃同一批哈希，不再重复读那 330 MB。
    """
    revisions = {title: packformat.content_revision(path) for title, path in tracks.items()}
    manifest = build_manifest(
        sorted(tracks), album, pack_id,
        loudness=table[0] if table else None,
        revisions=revisions,
        revision=packformat.revisions_of([(f"{title}{path.suffix}", revisions[title])
                                          for title, path in tracks.items()]),
        # 曲目表跟着源走（D145）：清单里的**曲目**按曲包 TOML，**地址**按磁盘文件
        snapshot=packformat.repo_snapshot(),
    )
    (root / MANIFEST_NAME).write_text(render(manifest), encoding="utf-8")
    out.parent.mkdir(parents=True, exist_ok=True)
    _write_archive(root, out)
    return manifest


def stage(out: pathlib.Path, archive: str | None = None, library: pathlib.Path | None = None,
          album: str = DEFAULT_ALBUM, base: str | None = None,
          cards: pathlib.Path | None = None) -> dict:
    """把素材铺进 ``out``（= ``dist/``）：从归档，或直接从本地曲库。"""
    out.mkdir(parents=True, exist_ok=True)
    if (archive is None) == (library is None):
        raise SystemExit("❌ 二选一：`--archive <URL|路径>` 或 `--from <曲库>`")
    if library is not None:                       # 直接从曲库铺（本地全静态构建用）
        tracks = library_tracks(library, album)
        media = out / MEDIA_DIR / album
        media.mkdir(parents=True, exist_ok=True)
        for title, path in sorted(tracks.items()):
            shutil.copyfile(path, media / f"{title}{path.suffix}")
        table = declared_loudness(album)
        if table is not None and not table[1].is_file():
            table = None
        _copy_loudness(out, table)
        manifest = build_manifest(sorted(tracks), album, base=base,
                                  loudness=table[0] if table else None,
                                  snapshot=packformat.repo_snapshot())
        (out / MANIFEST_NAME).write_text(render(manifest), encoding="utf-8")
        copied_cards = _copy_cards(cards, out / CARDS_DIR)
        return {"tracks": len(tracks), "cards": copied_cards, "source": "library", "out": out,
                "manifest": manifest, "loudness": 1 if table else 0}

    with tempfile.TemporaryDirectory() as tmp:
        if archive.startswith(("http://", "https://")):
            name = pathlib.Path(archive.split("?", 1)[0]).name or "archive.tar.gz"
            local = download_archive(archive, pathlib.Path(tmp) / name)
        else:
            local = pathlib.Path(archive)
            if not local.is_file():
                raise SystemExit(f"❌ 归档不存在：{local}")
        if not str(local).endswith(ARCHIVE_SUFFIXES):
            raise SystemExit(f"❌ 只认 tar 归档（{ARCHIVE_SUFFIXES}），收到 {local.name}")
        extract(local, out)

    manifest_path = out / MANIFEST_NAME
    tracks = audio_files(out / MEDIA_DIR / album)
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if base:                                  # 要绝对地址就把媒体地址重烘一遍（**保留响度表与曲目表声明**）
            titles = [row[1] for row in manifest.get("tracks", [])]
            # 版本号（逐曲第 4 位 + 顶层 `revision`）**原样透传**：重烘的只是地址，
            # 客户端该不该重下媒体不该因为"换了个基地址"而变化。
            revisions = {row[1]: row[3] for row in manifest.get("tracks", []) if len(row) > 3}
            manifest = build_manifest(titles, album, manifest.get("pack"), base=base,
                                      loudness=manifest.get("loudness"),
                                      revisions=revisions or None,
                                      revision=manifest.get("revision"),
                                      snapshot=packformat.pack_snapshot_of(manifest))
            manifest_path.write_text(render(manifest), encoding="utf-8")
    else:                                         # 归档里没有 manifest（手工做的）→ 按铺好的文件生成
        manifest = build_manifest(sorted(tracks), album, base=base)
        manifest_path.write_text(render(manifest), encoding="utf-8")
    cards_dir = out / CARDS_DIR
    cards_copied = sum(1 for p in cards_dir.iterdir() if p.is_file()) if cards_dir.is_dir() else 0
    return {"tracks": len(tracks), "cards": cards_copied, "source": archive, "out": out,
            "manifest": manifest}


def verify(out: pathlib.Path, album: str = DEFAULT_ALBUM) -> list[str]:
    """铺完自检：manifest 的行数、专辑、以及每一行的文件**真的在磁盘上**（缺一条就报出来）。"""
    problems: list[str] = []
    manifest_path = out / MANIFEST_NAME
    if not manifest_path.is_file():
        return [f"缺少 {MANIFEST_NAME}"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    rows = manifest.get("tracks") or []
    on_disk = audio_files(out / MEDIA_DIR / album)
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, list) or len(row) < 3:
            problems.append(f"manifest 行形状不对：{row!r}")
            continue
        row_album, title, url = row[0], row[1], row[2]
        if row_album != album:
            problems.append(f"manifest 行里的专辑不是 {album!r}：{row_album!r}（{title}）")
        if title not in on_disk:
            problems.append(f"manifest 列了但磁盘上没有：{title}")
        # 地址形状：相对（`media/…`）或绝对（`<base>/media/…`）都必须以同一段结尾（同一套 quote）
        expected = ls.media_path(album, title).lstrip("/")
        tail = str(url).split("?", 1)[0]
        if tail != expected and not tail.endswith("/" + expected):
            problems.append(f"manifest 行的地址与口径不符：{url}")
        seen.add(title)
    for title in sorted(set(on_disk) - seen):
        problems.append(f"磁盘上有但 manifest 没列：{title}")
    return problems


# ------------------------------------------------------------------ repack（CI 侧重打，D149）

def repack(previous: pathlib.Path, out: pathlib.Path) -> dict:
    """用**本仓库现在的 `packs/` + 响度表**重打一份归档：**媒体来自上一份归档**（D149）。

    为什么需要它：CI 里没有曲库（音频不在任何仓库里）⇒ "从零打包"只能在有曲库的机器上做。
    但**改仓库里那些数据**（曲目表的标题/作者/附加信息/卡面、删曲目、重算的响度表）不需要新音频 ——
    这类改动可以让 CI 拿上一份归档里的媒体 + 仓库现在的真源重打一份 ⇒ **推一下仓库就自动上线**。

    不含：**新音频**（`fetch_audio` 只能在有曲库的机器上跑）。声明了却没有音频的曲目照旧不出现，
    由调用方（CI 里紧接着跑的 `review`）警告出来 —— 与本地打包时的口径一致（那种曲目本来就播不了）。

    产物与 :func:`pack` **同一口径**（行、专辑、曲目表、响度表、可复现），版本号用内容哈希
    ⇒ CI 重打的清单与本机打的**逐字节相同**（有测试钉着）。
    """
    if not previous.is_file():
        raise SystemExit(f"❌ 上一份归档不存在：{previous}")
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        extract(previous, root)                     # 归档是**不可信输入**，走同一条安全检查
        before = manifest_of(previous)
        album = str(before.get("pack") or DEFAULT_ALBUM)
        media = root / MEDIA_DIR / album
        tracks = audio_files(media)
        if not tracks:
            raise SystemExit(f"❌ 上一份归档里没有 {MEDIA_DIR}/{album}/ 的音频：{previous}")

        table = declared_loudness(album)
        if table is not None and not table[1].is_file():
            print(f"⚠️  注册表声明的响度表不存在，归档里不带它：{table[1]}")
            table = None
        _copy_loudness(root, table)                 # 表在**仓库**里 ⇒ 重算过的表会跟着重打进来
        manifest = _assemble(root, tracks, table, album, before.get("pack"), out)

    # "换了没有"要**比归档本身**：响度表变了而清单没变的情况也得算改了（清单里只记了表的路径）。
    # 先比大小（不等就一定变了）⇒ 变了的时候省掉两次 330 MB 读；等长再按**同一套内容哈希**比
    # （`packformat.content_revision`，不再在这里自己写一遍 chunked sha1）。
    changed = (previous.stat().st_size != out.stat().st_size
               or packformat.content_revision(previous) != packformat.content_revision(out))
    return {"archive": out, "tracks": len(tracks), "loudness": 1 if table else 0,
            "bytes": out.stat().st_size, "changed": changed, "manifest": manifest}


# ------------------------------------------------------------------ review（铺之前自检，D147）

def manifest_of(path: pathlib.Path) -> dict:
    """读出归档里的 `manifest.json`（铺之前要检查、部署之后要比对，都从它开始）。"""
    with tarfile.open(path, "r:gz") as archive:
        try:
            raw = archive.extractfile(MANIFEST_NAME)
        except KeyError:
            raise SystemExit(f"❌ 归档里没有 {MANIFEST_NAME}：{path}") from None
        return json.load(raw)          # **在 with 里读**：成员是绑定在归档上的，出了 with 就关了


def archive_snapshot(manifest: dict) -> dict | None:
    """归档 manifest 里的「包数据」段（`albums` + `characters`，D145）；老清单没有 ⇒ `None`。"""
    return packformat.pack_snapshot_of(manifest)


def _music_by_key(snapshot: dict | None) -> dict[str, list]:
    """`{角色 key: [music 条目, …]}`（快照的规范形，便于两个快照对比）。"""
    if not snapshot:
        return {}
    return {character["key"]: character["music"] for character in snapshot["characters"]}


def compare_snapshots(archive: dict | None, repo: dict | None) -> list[str]:
    """归档里的曲目表 ↔ 本仓库 `packs/` 现在的曲目表 → 差异描述（**只警告**，见 `review_archive`）。"""
    left, right = _music_by_key(archive), _music_by_key(repo)
    if not right:
        return []
    notes: list[str] = []
    missing = sorted(set(right) - set(left))                    # 仓库有、归档没有（还没抓？）
    if missing:
        notes.append(f"归档里少了 {len(missing)} 个角色的曲目（{'、'.join(missing[:3])}"
                     + ("…" if len(missing) > 3 else "") + "）—— 改完 packs 忘了重打包？")
    extra = sorted(set(left) - set(right))
    if extra:
        notes.append(f"归档里有 {len(extra)} 个角色在仓库 packs 里已经没有了（{'、'.join(extra[:3])}"
                     + ("…" if len(extra) > 3 else "") + "）—— 归档比仓库旧？")
    changed = [key for key in sorted(set(left) & set(right)) if left[key] != right[key]]
    if changed:
        counts = [f"{key}（归档 {len(left[key])} / 仓库 {len(right[key])} 条）" for key in changed[:3]]
        notes.append(f"{len(changed)} 个角色的曲目条目与归档不一致（改完 packs 忘了重打包？）："
                     f"{'、'.join(counts)}"
                     + ("…" if len(changed) > 3 else ""))
    return notes


def review_archive(path: pathlib.Path, *, compare_with_repo: bool = True) -> tuple[list[str], list[str]]:
    """铺之前自检一个素材归档 → ``(硬失败, 警告)``。

    归档是**部署物**：坏一个字节就是线上坏，而目标站（Cloudflare Pages 的素材站）**没有别的守卫**
    —— 它只是被铺上去。判据与 :func:`verify` 同一套（那边管"打"、这里管"铺"），另加两条：

    **硬失败**（一定是坏的部署）

    * 清单缺 `schema` / `pack` / `tracks` / `albums` / `characters` —— 后两个是 D145 的"包数据"，
      缺了应用只会走兜底，**这次部署等于没生效**；
    * 行形状不对，或**地址表里的每一行都要有对应文件**（缺 = 播到那首 404）；
    * `characters` 里一条曲目都没有（同上）。

    **只警告**（合法的中间态，不该拦住部署）

    * 曲目表里有、地址表里没有（那一首还没抓 / 还没打包）；
    * 地址表里有、曲目表里没有（曲库里多放了一个没写进曲包的 mp3 —— 按 D145 的口径曲目表以曲包为准）；
    * **归档与本仓库 `packs/` 不一致**（D147）：归档少 = 改完 packs 忘了重打包；归档多 = 归档比仓库旧。
      两个方向都只是"这次铺的不是仓库现在这份"，不是坏部署 —— 但值得在 CI 日志里吼一声。
    """
    problems: list[str] = []
    warnings: list[str] = []
    if not path.is_file():
        return [f"找不到归档：{path}"], []

    with tarfile.open(path, "r:gz") as archive:
        names = archive.getnames()
        members = {name for name in names if name.startswith(MEDIA_DIR + "/")}
        try:
            raw = archive.extractfile(MANIFEST_NAME)
        except KeyError:
            return [f"归档里没有 {MANIFEST_NAME}"], []
        manifest = json.load(raw)

    for key in ("schema", "pack", "tracks", "albums", "characters"):
        if key not in manifest:
            problems.append(f"manifest 缺 {key}")
    if problems:
        return problems, warnings

    rows = manifest["tracks"]
    for row in rows:
        if not isinstance(row, list) or len(row) < 3 or not all(isinstance(x, str) for x in row[:3]):
            problems.append(f"manifest 行形状不对：{row!r}")
    good_rows = [row for row in rows if isinstance(row, list) and len(row) >= 3]
    entries = [entry for character in manifest["characters"] for entry in character["music"]]
    if not entries:
        problems.append("characters 里一条曲目都没有（应用只会走兜底 ⇒ 这次部署等于没生效）")

    pack_id = manifest["pack"]
    for row in good_rows:
        # 地址可能是相对的（`media/…`）或绝对的（`https://host/media/…`）：只看末尾那段。
        # 地址里的文件名是**百分号编码**的（与 :func:`local_source.media_path` 同一套 quote），
        # 而归档成员名是原始 UTF-8 ⇒ 比之前先 unquote（D96/D141：两者必须是同一个名字）
        tail = str(row[2]).split("?", 1)[0].rstrip("/")
        encoded = pathlib.PurePosixPath(tail).name
        if f"{MEDIA_DIR}/{pack_id}/{unquote(encoded)}" not in members:
            problems.append(f"地址表里有、文件没有：{unquote(encoded)}（{row[1]}）")

    missing_rows = [entry[1] for entry in entries
                    if not any(packformat.titles_match(row[1], entry[1]) for row in good_rows)]
    if missing_rows:
        warnings.append(f"曲目表里有 {len(missing_rows)} 条在地址表里找不到（还没抓/还没打包？）："
                        f"{'、'.join(missing_rows[:3])}" + ("…" if len(missing_rows) > 3 else ""))
    extra_rows = [row[1] for row in good_rows
                  if not any(packformat.titles_match(row[1], entry[1]) for entry in entries)]
    if extra_rows:
        warnings.append(f"地址表里有 {len(extra_rows)} 条不在曲目表里（曲包里没写 ⇒ 应用里选不到，"
                        f"合法）：{'、'.join(extra_rows[:3])}" + ("…" if len(extra_rows) > 3 else ""))

    if compare_with_repo:
        warnings.extend(compare_snapshots(archive_snapshot(manifest), packformat.repo_snapshot()))
    return problems, warnings


def loudness_coverage(out: pathlib.Path, album: str = DEFAULT_ALBUM) -> dict:
    """manifest 声明的响度表 ↔ 音频的对应关系（D139）。
    改名后的残留（也是提示）。归档里没声明表就返回 ``{}`` 的空壳（``declared`` 为 None）。
    """
    report: dict = {"declared": None, "keys": 0, "missing": [], "extra": []}
    manifest_path = out / MANIFEST_NAME
    if not manifest_path.is_file():
        return report
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    declared = manifest.get("loudness")
    if not declared:
        return report
    report["declared"] = declared
    table_path = out / str(declared)
    if not table_path.is_file():
        report["error"] = f"manifest 声明了响度表，但文件不在：{declared}"
        return report
    data = json.loads(table_path.read_text(encoding="utf-8"))
    gains = data.get("gains") if isinstance(data, dict) else None
    keys = set(gains or {})
    files = set(audio_files(out / MEDIA_DIR / album))
    report["keys"] = len(keys)
    report["missing"] = sorted(files - keys)
    report["extra"] = sorted(keys - files)
    return report


# ------------------------------------------------------------------ CLI

def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--album", default=DEFAULT_ALBUM,
                        help=f"专辑目录名（= 曲包 id，默认 {DEFAULT_ALBUM}）")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="音MAD 素材打归档 / 铺进 dist（纯静态部署用；素材不进仓库，见主仓库 D138）")
    sub = parser.add_subparsers(dest="command", required=True)

    packer = sub.add_parser("pack", help="打一个可发布的归档（布局 = 部署根）")
    packer.add_argument("--library", type=pathlib.Path, default=ls.DEFAULT_ROOT,
                        help=f"曲库根（<root>/<专辑>/*.mp3，默认 {ls.DEFAULT_ROOT}）")
    packer.add_argument("--out", type=pathlib.Path, default=pathlib.Path("otomads-media.tar.gz"),
                        help="归档输出路径（默认 otomads-media.tar.gz）")
    packer.add_argument("--cards", type=pathlib.Path, default=None,
                        help=f"可选：音MAD 卡面目录，内容会放进归档的 {CARDS_DIR}/")
    _add_common(packer)

    stager = sub.add_parser("stage", help="把素材铺进 dist（构建时用）")
    stager.add_argument("--archive", default=None, help="归档地址（URL 或本地路径）")
    stager.add_argument("--from", dest="library", type=pathlib.Path, default=None,
                        help="改从本地曲库直接铺（不起归档）")
    stager.add_argument("--out", type=pathlib.Path, default=pathlib.Path("dist"),
                        help="铺到哪里（默认 dist）")
    stager.add_argument("--base", default=None,
                        help="媒体地址的基地址（给了就写成绝对；默认写相对地址）")
    stager.add_argument("--cards", type=pathlib.Path, default=None,
                        help="配合 --from：音MAD 卡面目录（内容铺到 cards-otomads/）")
    _add_common(stager)

    repacker = sub.add_parser("repack", help="用仓库现在的 packs/ + 响度表重打一份归档（CI 用，D149）")
    repacker.add_argument("--previous", type=pathlib.Path, required=True,
                          help="上一份归档（**媒体**的来源）")
    repacker.add_argument("--out", type=pathlib.Path, default=pathlib.Path("otomads-media.tar.gz"),
                          help="重打到哪（默认 otomads-media.tar.gz）")
    # 注意：**没有** `--album` —— 重打的专辑名来自上一份归档，命令行给了也不会被读（以前挂着这个死参数）

    reviewer = sub.add_parser("review", help="铺之前自检一个归档（CDN 工作流用；本地也能跑）")
    reviewer.add_argument("--archive", type=pathlib.Path,
                          default=pathlib.Path("otomads-media.tar.gz"),
                          help="要自检的归档（默认 otomads-media.tar.gz）")
    reviewer.add_argument("--no-packs", action="store_true",
                          help="不比对本仓库的 packs/（归档不是这份仓库打的时候）")
    # 同样**没有** `--album`：review 的专辑名也从归档里读

    args = parser.parse_args(argv)

    if args.command == "repack":
        summary = repack(args.previous, args.out)
        print(f"✅ 重打 {summary['archive']}：{summary['tracks']} 首 / "
              f"{summary['bytes'] / 1048576:.1f} MB / 响度表 {'带上' if summary['loudness'] else '没带'}")
        print("   与上一份相比：" + ("**变了** —— 该换资产、该重铺" if summary["changed"]
                                    else "没变 —— 不必换资产（站点侧改动仍可照常重铺）"))
        return 0

    if args.command == "review":
        problems, warnings = review_archive(args.archive, compare_with_repo=not args.no_packs)
        for warning in warnings:
            print(f"⚠️  {warning}")
        for problem in problems[:10]:
            print(f"  - {problem}")
        if problems:
            print(f"❌ 归档自检没过（{len(problems)} 处）：{args.archive}")
            return 1
        manifest = manifest_of(args.archive)
        entries = sum(len(character["music"]) for character in manifest["characters"])
        print(f"✅ 归档自检通过：{args.archive} → {len(manifest['tracks'])} 行地址 / "
              f"{len(manifest['characters'])} 个角色 / {entries} 条曲目条目 / "
              f"顶层 revision {manifest.get('revision')}")
        return 0

    if args.command == "pack":
        summary = pack(args.library, args.out, args.album, args.cards)
        print(f"✅ 归档 {summary['archive']}：{summary['tracks']} 首 / "
              f"{summary['cards']} 张卡面 / {summary['bytes'] / 1048576:.1f} MB")
        print(f"   响度表：{'已带上（跟着源走，D139）' if summary['loudness'] else '没带（前端会回落应用侧那份）'}")
        print("   发布它（例如 GitHub Release 资产），构建时用："
              "stage --archive <它的 URL> --out dist")
        return 0

    if args.cards is not None and args.archive is not None:
        print("⚠️  `--cards` 只在 `--from <曲库>` 那条路上有用"
              "（从归档铺时卡面本来就在归档里）—— 这次忽略它")
    summary = stage(args.out, args.archive, args.library, args.album, args.base, args.cards)
    problems = verify(args.out, args.album)
    if not (args.out / "index.html").is_file():
        print(f"⚠️  {args.out} 里没有 index.html —— 先 `pnpm build`（这一步只铺素材，不构建应用）")
    print(f"✅ 铺好 {summary['out']}：{summary['tracks']} 首 / {summary['cards']} 张卡面"
          f"（来源：{summary['source']}）")
    print(f"   {MANIFEST_NAME}：{len(summary['manifest']['tracks'])} 行，"
          f"媒体地址{'绝对' if args.base else '相对'}（{'--base ' + args.base if args.base else '与部署基地址无关'}）")
    coverage = loudness_coverage(args.out, args.album)
    if coverage.get("declared"):
        print(f"   响度表 {coverage['declared']}：{coverage['keys']} 条（跟着源走，D139）")
        if coverage.get("error"):
            print(f"   ✗ {coverage['error']}")
        if coverage["missing"]:
            print(f"   ⚠️  表里缺 {len(coverage['missing'])} 首（合法：没量过 ⇒ 增益按 1）")
        if coverage["extra"]:
            print(f"   ⚠️  表里有 {len(coverage['extra'])} 个键对不上音频（改名残留？）")
    else:
        print("   响度表：源没声明（前端会回落到应用侧那份 `data/<模式>/loudness/`）")
    for problem in problems:
        print(f"✗ {problem}")
    if problems:
        print(f"❌ 自检没通过（{len(problems)} 处）")
        return 1
    print("✅ 自检通过：manifest 与磁盘上的音频一一对应")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
