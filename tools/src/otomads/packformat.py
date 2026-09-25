"""附加曲包（``packs/*.toml``）的格式层：读取、严格校验与音频路径工具（本仓库侧）。

曲包是"镜像表以外的曲目"：比如音MAD（otomads）那批只存在于本机（由
``tools/src/tmc/local_source.py`` 起的本地曲库助手提供）的曲目。它们不进
``data/sources/*.json``，因此：

* ``albums.toml`` 里不必也不该为它们写条目 —— 曲包自己带 ``[[album]]``；
* 校验里"每条被引用的曲目都必须在三个镜像表里"这条要**跳过**曲包曲目，
  但不能跳过别的检查（专辑注册、角色存在、重复、附加信息合法性）。

布局：**一个曲包 = 一份清单 + 一角色一份曲目文件**（曲目文件与 ``data/characters/*.toml`` 同一风格，
一角色一份、顶层 ``key``）::

    <根>/otomads.toml                    # 清单：只放 [pack] 与 [[album]]
    <根>/otomads/kirisame-marisa.toml    # 角色文件：该角色的若干 [[track]]

**根目录**（:func:`otomads.paths.pack_roots`）：本仓库的 `packs/`。

清单（`<根>/<曲包 id>.toml`）::

    [pack]
    id = "otomads"
    label_en = "Otomads"
    label_zh = "音MAD"
    kind = "local"          # local = 曲目地址来自本地曲库助手的 manifest
    order = 100

    [[album]]
    key = "otomads"
    name = "otomads"
    kind = "other"
    pack = "otomads"
    order = 100

角色文件（``<根>/<曲包 id>/<角色 key>.toml``）::

    key = "kirisame-marisa"   # 必须与文件名一致；文件的曲目都算这个角色
    card = ["魔理沙-mad.png"]  # 可选：本模式的卡面（缺省沿用共享身份的卡面）

    [[track]]
    album = "otomads"
    author = "川先僧"
    title = "普通肥猫魔法使"
    extra = "角色曲"
    source = "https://www.bilibili.com/video/BV1kw411q7S8"   # 可选：抓取用（见 docs/packs-audio-v1.md）
    start_time = "00:00:40.000"                              # 可选：裁剪开始
    stop_time = "00:01:10.000"                               # 可选：裁剪结束

``[[track]]`` 里**不再写 ``character``**（角色由文件的 ``key`` 决定），清单里也**不许**写
``[[track]]``（曲目一律进角色文件），两条都**直接报错**而不是猜。

三个音频键（``source`` / ``start_time`` / ``stop_time``）**只在抓取与裁剪期被读**，
运行时不进 ``characters.json``、前端也看不到它们（契约见 ``docs/packs-audio-v1.md``）。
"""
from __future__ import annotations

import copy
import hashlib
import pathlib
import re
import sys
import tomllib

from . import paths as repo

#: 各段允许的键。**写错键名必须报错**：早先解析只读自己认识的键，
#: 拼错的 `starttime` 会被静默丢掉，表现为"数据里写了却不生效"（见 docs/packs-audio-v1.md §1）。
PACK_KEYS = {"id", "label_en", "label_zh", "kind", "order"}
ALBUM_KEYS = {"key", "name", "kind", "pack", "order", "show_album_name"}
#: 角色文件里 `[[track]]` 的键 —— **没有** `character`：角色由文件的 `key` 决定
TRACK_KEYS = {"album", "author", "authors", "title", "extra", "source", "start_time", "stop_time"}
#: 角色文件的顶层键（`track` 之外）：`card` 是**可选**的卡面覆盖（写法同 `data/characters/*.toml`）
CHARACTER_KEYS = {"key", "card"}


#: 裁剪时间的格式：`HH:MM:SS.mmm`（时:分:秒.毫秒）
TIME_RE = re.compile(r"^(\d{1,2}):([0-5]\d):([0-5]\d)\.(\d{3})$")


def _reject_unknown(where: str, entry: dict, allowed: set[str]) -> None:
    unknown = sorted(set(entry) - allowed)
    if unknown:
        raise SystemExit(
            f"{where}: 不认识的键 {unknown}（允许 {sorted(allowed)}）—— 写错名字会被静默忽略，所以直接报错")


def parse_time(text: str) -> float:
    """`HH:MM:SS.mmm` → 秒。格式不对抛 `ValueError`（调用方补上下文）。"""
    matched = TIME_RE.match(text.strip())
    if not matched:
        raise ValueError(f"时间格式必须是 HH:MM:SS.mmm，收到 {text!r}")
    hours, minutes, seconds, millis = (int(group) for group in matched.groups())
    return hours * 3600 + minutes * 60 + seconds + millis / 1000


def trim_seconds(track: dict) -> tuple[float, float | None] | None:
    """裁剪区间 → `(起点秒, 时长秒 | None)`；两个键都没写 ⇒ `None`（不裁剪）。

    单侧语义（docs/packs-audio-v1.md §1）：只给 `stop_time` ⇒ 从文件开头；
    只给 `start_time` ⇒ 裁到文件结尾（时长返回 `None`，交给 ffmpeg 自己读到尾）。
    """
    start_text = track.get("start_time")
    stop_text = track.get("stop_time")
    if not start_text and not stop_text:
        return None
    start = parse_time(start_text) if start_text else 0.0
    if not stop_text:
        return (start, None)
    stop = parse_time(stop_text)
    if stop <= start:
        raise ValueError(f"stop_time（{stop_text}）必须晚于 start_time（{start_text or '00:00:00.000'}）")
    return (start, stop - start)


def audio_filename(track: dict) -> str:
    """成品文件名 —— **必须**与磁盘/助手 manifest 的口径一致：`作者 - 标题.mp3`（无作者则 `标题.mp3`）。

    这个名字同时是 manifest 的匹配键、`loudness.json` 的键与单曲模式存档的一部分，所以不能改（D95/D96）。
    """
    author = author_of(track)
    return f"{author} - {track['title']}.mp3" if author else f"{track['title']}.mp3"


def audio_stem(track: dict) -> str:
    """成品文件名的 **stem**（= 响度表的键、`gainKeyOf` 的口径）：`作者 - 标题`，**不带扩展名**。

    与 `audio_filename` 分两个函数是刻意的：抓取/裁剪要**文件名**，响度表要 **stem**，
    混用会让"表与曲目对不上"的检查全程误报（实测踩过：86 首全被报成缺失 + 全被报成残留）。
    """
    return pathlib.Path(audio_filename(track)).stem


def source_key(source: str) -> str:
    """`source` → 原始件的文件名（同一来源只下一份，重复引用时复用）。"""
    return hashlib.sha1(source.strip().encode("utf-8")).hexdigest()[:16]


def normalize_title(value: str) -> str:
    """曲名归一化：去 `作者 - ` 前缀 → 压空白 → 去首尾 → 小写。

    **与主仓库 `src/music/sources.ts::normalizeTitle` 同一口径**（那边是运行时的曲目解析）：
    清单行里的曲名是**磁盘名的 stem**（`作者 - 标题`），而曲目表 `/ 曲包 TOML` 里作者是独立字段，
    两边要比就得先归一化。两份实现零 import 依赖，靠主仓库 `tools/tests/test_build.py` 里那条
    **按字面量对正则**的守卫盯着（与 `AUTHOR_JOIN` 同一个套路，D147）。

    ⚠️ 作者名里带 `-` 时前缀剥不掉（`Rendering-Liu - 岁月` 会原样留下）—— 所以匹配还要靠
    :func:`titles_match` 的后缀那条（前端 `resolveTrack` 的兜底也是这么写的）。
    """
    stripped = re.sub(r"^[^-]{1,60}?\s+-\s+", "", value)
    return re.sub(r"\s+", " ", stripped).strip().lower()


def titles_match(stored: str, wanted: str) -> bool:
    """清单行 / 磁盘名 `stored` 是否就是曲目表里的 `wanted`（归一化相等，或以 ` - 曲名` 结尾）。"""
    stored_norm = normalize_title(stored)
    wanted_norm = normalize_title(wanted)
    return stored_norm == wanted_norm or stored_norm.endswith(f" - {wanted_norm}")


def available() -> bool:
    """曲包真源是否可用（数据仓库永远有 `packs/`；保留它是为了让调用方写法一致）。"""
    return any(root.is_dir() and any(root.glob("*.toml")) for root in repo.pack_roots())


def load_packs() -> tuple[list[dict], list[dict], list[dict], dict[str, list[str]]]:
    """读全部曲包根目录 → ``(packs, albums, tracks, cards)``。

    ``cards`` 是"音MAD 侧自己的卡面覆盖"：``{角色 key: [卡面文件名, …]}``（只有写了 `card` 的角色才在里面）。
    根目录见 :func:`otomads.paths.pack_roots`（本仓库的 `packs/`）。
    """
    packs: list[dict] = []
    albums: list[dict] = []
    tracks: list[dict] = []
    cards: dict[str, list[str]] = {}
    for directory in repo.pack_roots():
        if not directory.is_dir():
            print(f"[packs] 跳过不存在的曲包根目录 {repo.shown(directory)}", file=sys.stderr)
            continue
        _load_root(directory, packs, albums, tracks, cards)
    packs.sort(key=lambda item: item["order"])
    return packs, albums, tracks, cards


def _load_root(directory: pathlib.Path, packs: list[dict], albums: list[dict],
               tracks: list[dict], cards: dict[str, list[str]]) -> None:
    """读一个曲包根目录：``<id>.toml`` 清单 + ``<id>/`` 角色文件。"""
    for path in sorted(directory.glob("*.toml")):
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
        meta = data.get("pack")
        if not meta:
            raise SystemExit(f"{path.name}: 缺少 [pack] 段")
        _reject_unknown(f"{path.name} 的 [pack]", meta, PACK_KEYS)
        pack_id = meta["id"]
        packs.append({
            "id": pack_id,
            "label": {"en": meta.get("label_en", pack_id), "zh": meta.get("label_zh", pack_id)},
            "kind": meta.get("kind", "local"),
            "order": meta.get("order", 0),
        })
        for entry in data.get("album", []):
            _reject_unknown(f"{path.name} 的 [[album]]", entry, ALBUM_KEYS)
            album = {
                "key": entry["key"],
                "name": entry["name"],
                "kind": entry.get("kind", "other"),
                "pack": entry.get("pack", pack_id),
                "order": entry.get("order", 0),
            }
            # 可选：专辑名要不要显示（不填 = true）。曲包专辑常设 false，曲目没作者时那一行就不显示
            if "show_album_name" in entry:
                album["showAlbumName"] = bool(entry["show_album_name"])
            albums.append(album)
        if data.get("track"):
            rel = repo.shown(directory)
            raise SystemExit(f"{path.name}: 曲目要写进 {rel}/{pack_id}/<角色 key>.toml（一角色一份），"
                             f"清单只放 [pack] 与 [[album]]")
        tracks.extend(_character_tracks(directory / pack_id, path.name, cards))


def _character_tracks(pack_dir: pathlib.Path, manifest: str,
                      cards: dict[str, list[str]]) -> list[dict]:
    """读 ``<根>/<曲包 id>/*.toml`` → 曲目列表（文件按名排序，文件内保持原顺序）。

    角色由文件的 ``key`` 决定，**文件名必须与它一致**：曲包里的 key 写错曾一次性丢掉 3 条曲目
    （2026-09 那次 `reisen-udongein` 少写 `-inaba`），所以这里错了直接报；报错文案带包内相对路径，
    否则 35 个 `cirno.toml` 分不清是哪个包。

    ``cards`` 是出参：文件里写了 ``card`` 就记一笔（音MAD 侧自己的卡面，写法见曲包根目录的 ``README.ai.MD``）。
    """
    if not pack_dir.is_dir():
        return []
    out: list[dict] = []
    for path in sorted(pack_dir.glob("*.toml")):
        where = f"{pack_dir.name}/{path.name}"
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
        if "pack" in data or "album" in data:
            raise SystemExit(f"{where}: [pack] / [[album]] 只能写在清单 {manifest} 里")
        _reject_unknown(where, {k: v for k, v in data.items() if k != "track"}, CHARACTER_KEYS)
        key = data.get("key")
        if not isinstance(key, str) or not key:
            raise SystemExit(f"{where}: 缺少 key（= 角色 key）")
        if path.stem != key:
            raise SystemExit(f"{where}: 文件名与 key 不一致（{path.stem} vs {key}）")
        if "card" in data:
            face = data["card"]
            if not isinstance(face, list) or not face or not all(isinstance(f, str) and f for f in face):
                raise SystemExit(f"{where}: card 必须是至少一项的字符串数组（写成 data/characters/*.toml 那样）")
            cards[key] = list(face)
        for entry in data.get("track", []):
            _reject_unknown(f"{where} 的 [[track]]", entry, TRACK_KEYS)
            track = {
                "character": key,
                "album": entry["album"],
                "title": entry["title"],
                "extra": entry.get("extra", "角色曲"),
                "pack": pack_dir.name,
            }
            _read_authors(entry, track, f"{where} / {entry.get('title')}")
            _read_audio_keys(entry, track, f"{where} / {track['title']}")
            out.append(track)
    return out


#: 多作者在**成品文件名**里的连接符。磁盘名 `作者 - 标题.mp3` 是 manifest 匹配键、响度表键
#: 与单曲存档的一部分（D95/D96，不能改），所以 `authors = ["甲", "乙"]` 必须能还原成 `甲 & 乙`。
AUTHOR_JOIN = " & "


def author_of(track: dict) -> str:
    """一条曲目的**署名整串**（= 成品文件名里 `作者` 那一段）。

    优先 `author`（老写法/规范化后的结果）；只有 `authors` 时按 `AUTHOR_JOIN` 拼 ——
    与 `packs.AUTHOR_JOIN` 同一口径（两份实现必须一致，否则助手找不到文件）。
    """
    single = (track.get("author") or "").strip()
    if single:
        return single
    return AUTHOR_JOIN.join(str(name).strip() for name in (track.get("authors") or []))


def _read_authors(entry: dict, track: dict, where: str) -> None:
    """`author`（整串）或 `authors`（数组）：两种写法都认，**不能同时写**（D135）。

    `authors` 会被规范化成"数组 + 整串"两份：数组给显示排序用，整串是成品文件名那一位。
    两种写法在磁盘上**完全等价** ⇒ 把一个老条目改成数组不需要重抓/重裁音频。
    `author = "乙 & 甲"` 这种整串**不拆**：人名里也可能有 `&`，猜分隔符会拆错。
    """
    single = entry.get("author")
    many = entry.get("authors")
    if single is not None and many is not None:
        raise SystemExit(f"{where}：author 与 authors 只能写一个（author 是整串、authors 是数组）")
    if many is not None:
        if not isinstance(many, list) or not many:
            raise SystemExit(f"{where}：authors 必须是非空字符串数组，例如 authors = [\"甲\", \"乙\"]")
        cleaned = [str(name).strip() for name in many]
        if not all(cleaned):
            raise SystemExit(f"{where}：authors 里不能有空字符串")
        track["authors"] = cleaned
        track["author"] = AUTHOR_JOIN.join(cleaned)
    elif single:
        track["author"] = single


def _read_audio_keys(entry: dict, track: dict, where: str) -> None:
    """`source` / `start_time` / `stop_time`：解析 + 就地校验（构建期就能发现写错）。"""
    source = (entry.get("source") or "").strip()
    if source:
        if not source.lower().startswith(("http://", "https://")):
            raise SystemExit(f"{where}：source 必须是 http(s) 链接，收到 {source!r}")
        track["source"] = source
    for field in ("start_time", "stop_time"):
        value = entry.get(field)
        if value in (None, ""):
            continue
        if not isinstance(value, str):
            raise SystemExit(f"{where}：{field} 必须是字符串（HH:MM:SS.mmm），收到 {value!r}")
        try:
            parse_time(value)
        except ValueError as error:
            raise SystemExit(f"{where}：{field} {error}") from None
        track[field] = value
    try:
        trim_seconds(track)          # 只给一侧也合法；两侧都给时校验先后
    except ValueError as error:
        raise SystemExit(f"{where}：{error}") from None


def music_entry(track: dict) -> list:
    """一条曲目 → 运行时 ``music`` 条目：``[专辑, 曲名, 附加信息]`` + 可选作者（第 4 位）+ 可选多作者（第 5 位）。

    形状**必须与主仓库 ``tmc.build._pack_music`` 逐字一致**（D94/D135/D145）：应用侧把源给的这份
    "曲目条目"按 (专辑, 曲名) 与源清单里的地址配起来，两边形状一旦漂移就会"看得见、点不响"。
    任何一侧改了这里，另一侧的 `tools/tests` 里那份**共享测试向量**会红。
    """
    entry: list = [track["album"], track["title"], track["extra"]]
    if track.get("author"):
        entry.append(track["author"])       # 可选第 4 位：署名整串（成品文件名那一段）
    if track.get("authors"):
        entry.append(track["authors"])      # 可选第 5 位：多作者数组（与第 4 位同源，D135）
    return entry


def pack_snapshot(albums: list[dict], tracks: list[dict],
                  cards: dict[str, list[str]] | None = None) -> dict[str, list[dict]]:
    """曲包真源 → 源清单里的「包数据」段（``{"albums": […], "characters": […]}``，主仓库 D145）。

    这是 C 路线的数据侧一半：源在自己的 ``manifest.json`` 里多带一段"这个包有哪些曲目"，
    应用拿它 + 自己的身份表（``data/characters/*.toml`` 的原曲数据集）在运行时拼出音MAD 数据集 ——
    于是**加曲目 / 改裁切 / 换音频只动本仓库 + 铺源**，主仓库连 pin 都不用动。

    只给"角色 → 曲目"，**不复制身份**（``name`` / ``order`` / ``searchNames`` 的真源仍是主仓库，契约 §5 S1）：
    应用启动时本来就把原曲数据集取全了，身份去那里取。将来真要"音MAD 自有身份"（S2）时，
    角色条目可以**可选**地自带 ``name`` / ``order`` / ``searchNames``（应用侧已经接收这三个字段）。

    输入就是 :func:`load_packs` 已经会返回的那几样：``albums`` 是包自带专辑，``tracks`` 是全部曲目，
    ``cards`` 是 ``{角色 key: [卡面文件名]}``。纯函数：不读盘、不改入参。
    """
    music: dict[str, list[list]] = {}
    for track in tracks:
        music.setdefault(track["character"], []).append(music_entry(track))
    faces = cards or {}
    characters: list[dict] = []
    for key, entries in music.items():
        record: dict = {"key": key, "music": entries}
        face = faces.get(key)
        if face:
            record["card"] = list(face)     # 音MAD 侧自己的卡面覆盖（有才覆盖，D137）
        characters.append(record)
    return {"albums": [dict(album) for album in albums], "characters": characters}


#: `repo_snapshot()` 的缓存：键 = (TOML 个数, 最新 mtime_ns)。只留最新一批，不会长成泄漏。
_SNAPSHOT_CACHE: dict[tuple[int, int], dict] = {}


def repo_snapshot() -> dict[str, list[dict]] | None:
    """**本仓库** `packs/` 现在的「包数据」段；没有曲包（或目录是空的）⇒ ``None``。

    工具按 ``__file__`` 定位仓库根 ⇒ "在哪份里跑就带哪份的曲目表"：在主仓库 submodule 里跑就是 pin 的
    那份，在独立克隆里跑就是克隆里那份（硬规矩见主仓库 D145）。
    **"往 TOML 里加一首立刻生效"这条性质不变**：缓存键是"TOML 个数 + 最新 mtime"——
    内容一动键就变，下一次调用照样现解析；变的只是"同一批文件被连续问很多次"（助手每来一个
    `/manifest.json` 都要算一次）不再把 121 份 TOML 反复解析（实测 6.1 ms → 0.25 ms）。
    返回**深拷贝**：调用方拿去并进 manifest，不该共享同一份可变对象。
    """
    if not available():
        return None
    files = sorted(repo.packs_dir().rglob("*.toml"))
    key = (len(files), max((path.stat().st_mtime_ns for path in files), default=0))
    hit = _SNAPSHOT_CACHE.get(key)
    if hit is None:
        _packs, albums, tracks, cards = load_packs()
        hit = pack_snapshot(albums, tracks, cards)
        _SNAPSHOT_CACHE.clear()
        _SNAPSHOT_CACHE[key] = hit
    return copy.deepcopy(hit)


def pack_snapshot_of(manifest: dict) -> dict[str, list[dict]] | None:
    """清单 → 它带的「包数据」段；**老清单没有这两个键** ⇒ ``None``（调用方按"不带"处理）。

    两个键要么都在、要么都不在：只有一半的清单是坏数据，这里按"不带"处理（应用侧同样严格校验，
    形状不对就整段不用、走自带那份兜底 —— 绝不半信半疑地用）。
    """
    albums, characters = manifest.get("albums"), manifest.get("characters")
    if not albums or not characters:
        return None
    return {"albums": albums, "characters": characters}


def loudness_path(pack_id: str) -> pathlib.Path | None:
    """该曲包**本源**的响度表路径：源注册表里的可选 `loudness` 键（相对仓库根）。

    每个源自己的表由自己的工具生成（契约见主仓库 `docs/packs-audio-v1.md`；主仓库按源的
    `loudnessUrl` 取表）。注册表没写这个键 ⇒ `None`（不生成）。
    """
    path = repo.find_source_registry(pack_id)
    if path is None:
        return None
    entries = tomllib.loads(path.read_text(encoding="utf-8")).get("source", [])
    for entry in entries:
        if entry.get("loudness"):
            return repo.DATA / entry["loudness"]
    return None


def content_revision(path) -> str:
    """文件的**内容**哈希（sha1 前 16 位）—— 归档侧用的版本号口径（D149）。

    为什么归档不再用 mtime（`media_revision` 那套）：**CI 要能重打归档**。媒体来自上一份归档，
    而 tar 里的 mtime 是归零的（可复现），照 mtime 算出来的版本号既与本机打包的对不上、
    也不随"内容变了但大小没变"而动。改成内容哈希之后：同一份音频在**任何机器**上算出来都一样 ⇒
    本机 `pnpm media:pack` 与 CI 的重打产出**逐字节相同**的清单（有测试钉着）。

    代价：要读文件内容。370 MB 的 sha1 大约 1 秒级，而"重新打包"本来就是秒级操作 —— 值当。
    （**本机助手**仍用 mtime：它每次请求现算，没必要为缓存键去哈希整个曲库。）
    """
    digest = hashlib.sha1()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()[:16]


def revisions_of(entries) -> str:
    """`[(清单里的名字, 内容哈希), …]` → **整表**版本号（按名字排序后哈希"名字 + 内容版本"，可复现）。

    内容哈希由**调用方**给：`stage_media.pack` / `repack` 本来就在算逐曲版本号，这里再读一遍文件
    等于把同一批 330 MB 音频 sha1 两遍（实测约 0.37 秒/次）。
    """
    digest = hashlib.sha1()
    for name, content in sorted(entries, key=lambda item: item[0]):
        digest.update(f"{name}\t{content}\n".encode())
    return digest.hexdigest()[:16]


def media_revision(entries) -> str:
    """`[(清单里的名字, 文件路径), …]` → **数据版本号**（16 位十六进制，sha1 截断）。

    ⚠️ **这不是遗留物**（别当成 D149 的旧口径删掉）：归档侧确实换成了 :func:`content_revision`，
    但**本机曲库助手**（`local_source.build_manifest`，主仓库 `pnpm local` 那条路）仍用它 ——
    它每次请求现算，没必要为缓存键去哈希整个曲库。

    前端把它拼进媒体地址（`?v=<revision>`，主仓库 D144）：**版本一变 = URL 一变** ⇒ 浏览器与
    CDN 都不能拿旧的顶。缓存键因此跟"音频本身"走，而不是跟"链接"走。

    输入只取**文件名 + 字节数 + mtime**：改一个字节、重裁一次、加一首、删一首，版本都会变；
    而"文件没动、只是重新打一次包"版本不变 ✓（同样的输入必然同样的输出，可复现）。
    **故意不读文件内容** —— 86 首要哈希 370 MB，而"重新打包"是个几秒级操作，不值当。
    """
    digest = hashlib.sha1()
    for name, path in sorted(entries, key=lambda item: item[0]):
        stat = pathlib.Path(path).stat()        # 助手那边给的是 str（os.path.join），两边都收
        digest.update(f"{name}\t{stat.st_size}\t{stat.st_mtime_ns}\n".encode())
    return digest.hexdigest()[:16]
