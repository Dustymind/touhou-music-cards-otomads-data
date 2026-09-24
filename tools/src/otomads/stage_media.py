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

    manifest.json                 # {"schema":1,"pack":"otomads","tracks":[[专辑, 曲目, 地址], …],
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

**幂等、可重复**：同一份曲库打两次，归档逐字节相同（tar 成员按名排序、mtime/uid/gid 归零、gzip mtime 归零）。
"""
from __future__ import annotations

import argparse
import gzip
import json
import pathlib
import shutil
import sys
import tarfile
import tempfile
import urllib.request

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
                   loudness: str | None = None) -> dict:
    """``{"schema":1,"pack":…,"tracks":[[专辑, 曲目, 地址], …],"loudness":…}``（行形状与助手一致）。

    ``loudness`` **相对 manifest 自身**（见模块 docstring 与主仓库 D139）；不给就不写这个键。
    """
    manifest = {
        "schema": 1,
        "pack": pack_id or album,
        "tracks": [[album, title, media_url(album, title, base)] for title in titles],
    }
    if loudness:
        manifest["loudness"] = loudness
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
    manifest = build_manifest(sorted(tracks), album, loudness=table[0] if table else None)
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        (root / MANIFEST_NAME).write_text(render(manifest), encoding="utf-8")
        media = root / MEDIA_DIR / album
        media.mkdir(parents=True)
        for title, path in sorted(tracks.items()):
            shutil.copyfile(path, media / f"{title}{path.suffix}")
        if table is not None:                     # 响度表跟着源走（路径 = manifest 里声明的那个）
            target = root / table[0]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(table[1], target)
        copied_cards = 0
        if cards is not None and cards.is_dir():
            target = root / CARDS_DIR
            target.mkdir()
            for path in sorted(cards.iterdir()):
                if path.is_file() and not path.name.startswith("."):
                    shutil.copyfile(path, target / path.name)
                    copied_cards += 1
        out.parent.mkdir(parents=True, exist_ok=True)
        _write_archive(root, out)
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


def _extract(archive_path: pathlib.Path, out: pathlib.Path) -> None:
    with tarfile.open(archive_path, "r:*") as archive:
        out.mkdir(parents=True, exist_ok=True)
        members = _safe_members(archive)
        try:
            archive.extractall(out, members=members, filter="data")   # 3.12+：顺带用官方过滤器
        except TypeError:                                            # 3.11 还没有 filter 参数
            archive.extractall(out, members=members)


def _download(url: str, dest: pathlib.Path) -> pathlib.Path:
    """下载归档（跟随重定向；走环境里的 http(s)_proxy）。"""
    print(f"⬇️  拉取素材：{url}")
    target = dest / pathlib.Path(url.split("?", 1)[0]).name
    with urllib.request.urlopen(url, timeout=120) as response, open(target, "wb") as handle:
        shutil.copyfileobj(response, handle)
    return target


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
        if table is not None and table[1].is_file():
            target = out / table[0]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(table[1], target)
        else:
            table = None
        manifest = build_manifest(sorted(tracks), album, base=base,
                                  loudness=table[0] if table else None)
        (out / MANIFEST_NAME).write_text(render(manifest), encoding="utf-8")
        copied_cards = 0
        if cards is not None and cards.is_dir():
            target = out / CARDS_DIR
            target.mkdir(exist_ok=True)
            for path in sorted(cards.iterdir()):
                if path.is_file() and not path.name.startswith("."):
                    shutil.copyfile(path, target / path.name)
                    copied_cards += 1
        return {"tracks": len(tracks), "cards": copied_cards, "source": "library", "out": out,
                "manifest": manifest, "loudness": 1 if table else 0}

    with tempfile.TemporaryDirectory() as tmp:
        if archive.startswith(("http://", "https://")):
            local = _download(archive, pathlib.Path(tmp))
        else:
            local = pathlib.Path(archive)
            if not local.is_file():
                raise SystemExit(f"❌ 归档不存在：{local}")
        if not str(local).endswith(ARCHIVE_SUFFIXES):
            raise SystemExit(f"❌ 只认 tar 归档（{ARCHIVE_SUFFIXES}），收到 {local.name}")
        _extract(local, out)

    manifest_path = out / MANIFEST_NAME
    tracks = audio_files(out / MEDIA_DIR / album)
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if base:                                  # 要绝对地址就把媒体地址重烘一遍（**保留响度表声明**）
            titles = [row[1] for row in manifest.get("tracks", [])]
            manifest = build_manifest(titles, album, manifest.get("pack"), base=base,
                                      loudness=manifest.get("loudness"))
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


def loudness_coverage(out: pathlib.Path, album: str = DEFAULT_ALBUM) -> dict:
    """manifest 声明的响度表 ↔ 音频的对应关系（D139）。

    只报数、**不当错误**：表里缺某首 = 那一首还没量过（合法，前端系数按 1）；表里有对不上的键 =
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

    args = parser.parse_args(argv)

    if args.command == "pack":
        summary = pack(args.library, args.out, args.album, args.cards)
        print(f"✅ 归档 {summary['archive']}：{summary['tracks']} 首 / "
              f"{summary['cards']} 张卡面 / {summary['bytes'] / 1048576:.1f} MB")
        print(f"   响度表：{'已带上（跟着源走，D139）' if summary['loudness'] else '没带（前端会回落应用侧那份）'}")
        print("   发布它（例如 GitHub Release 资产），构建时用："
              "stage --archive <它的 URL> --out dist")
        return 0

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
