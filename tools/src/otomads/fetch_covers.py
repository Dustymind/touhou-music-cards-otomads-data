"""抓**每条曲目**的 B 站封面直链，写进**它自己那条** ``[[track]]`` 里（一首一组，D153 + 裸原图修订）。

用法（在数据仓库根）::

    uv run python -m otomads.fetch_covers --dry-run   # 先看计划，不落盘（缓存也不写）
    uv run python -m otomads.fetch_covers             # 逐条补缺：**已经有 cover 的一条都不动**
    uv run python -m otomads.fetch_covers --force     # 整包刷新：按当前 source 的 BV 重抓每一条

**为什么写在曲目自己那一条里**：这份曲目表在**运行时**由源自己的 ``manifest.json`` 交给应用
（`packformat.pack_snapshot` → ``characters`` 段）⇒ 封面必须跟着**曲目**走。D153 之前的写法是
角色文件顶层一个 ``cover = [...]`` 数组、靠**位置**与 ``[[track]]`` 对应：加/删一首曲目就让整个
数组与曲目错位（少一项 = 后面每张图都串到下一首歌上，而且**不报错**）。写进条目里之后，
"哪张图配哪首歌"是结构上的事实，工具也就能**逐条**补缺 —— 手改的与工具补的天然共存，
不再需要"整只角色要么全跳、要么全刷新"。

数据形状（`packformat` 会硬校验）::

    [[track]]
    album = "otomads"
    author = "きゅうみぅ"
    title = "东方原曲大致最开始的音的おてんば娘"
    extra = "角色曲"
    source = "https://www.bilibili.com/video/BV1kb411U789/"     # 可选：抓取用
    cover = "https://i0.hdslb.com/bfs/archive/88ad….jpg"        # 可选：封面

``cover`` 是**一条**直链，值就是接口给的**未加工原图**（裸 ``data.pic``，只把 ``http://`` 换成
https）：**一个后缀都不加、不拼任何图床参数、也完全不看图片尺寸**。卡面的裁切与缩放全在**前端**
按用户选的那张卡面做 —— 数据侧只回答"原图在哪"。

⚠️ 两个已经作废、别再写回来的形状（改这段之前先读）：

1. ``@703w_1000h_1c.webp`` 这类图床后缀（早先"先裁后缩"的默认后缀，**旧缓存行**里还认得它）；
2. ``cover = { original = …, "16x9" = …, "4x3" = … }`` 那张**逐帧表**（曾经按原图的实际像素算出
   16:9 / 4:3 的两条居中裁切直链）—— 表现在是**硬错误**，跑一次 ``--force`` 就会整包换成裸原图。

⚠️ 两条实测踩出来的坑：

1. **请求头不许带 `Origin`**：带了 B 站风控直接回 403 + 一段 HTML（不是 JSON）⇒ 解析必炸。
   只带 ``User-Agent``（浏览器 UA）与 ``Referer: https://www.bilibili.com/`` 就够。
2. **接口给的 ``data.pic`` 常常是 ``http://``** —— 站点是 https，混内容会被浏览器拦掉，一律换 https。
   **这是唯一允许的改动**：其余一个字符都不动（不追加后缀、不加查询参数、不改路径）。

写回是**文本级**的（值的渲染走 ``packformat.toml_str``）：只在**那一条** ``[[track]]`` 块里插一行 / 换一行
（插在 ``source`` 的**下一行**；没有 ``source`` 就接在块的末尾；缩进跟着块里现有的键走），
文件里其它**一个字节都不动** —— 人工写的注释、字段顺序、行尾都保住。默认**只补没有的**：
已经有 ``cover`` 的那一条**连请求都不发**（那是"源内覆写"的人工入口，手工改过的一项原样留着）；
要按当前 ``source`` 整包重抓，得显式 ``--force``（**会盖掉手改**，也顺手把旧后缀 / 旧表统一成裸原图）。

**旧形状会自动迁移**：文件顶层还是 ``cover = [...]`` 数组时（D153 之前的写法），先把它第 i 条
搬进第 i 条 ``[[track]]``，再删掉顶层那一段（连同本工具生成的两行标记注释）—— **不联网、幂等**，
默认模式与 ``--force`` 都先做这一步（搬过来的值算"已有" ⇒ 默认模式不动它，``--force`` 才刷新）。
条数与曲目数**对不上**就报错、且**一个字节都不动**：位置对不上时"第 i 条给谁"没有正确答案，
猜错就是整包图错位还不报错。迁移发生在 `load_packs` **之前** —— 顶层 ``cover`` 现在是被明确拦下的
废弃形状，不先搬走就读不出曲目（这也是 `load_packs(validate_covers=False)` 放行它的原因）。

缓存：``.covers-cache.jsonl``（JSON Lines，一行一条，可续跑；已 gitignore）。命中
``(角色 key, 曲名)`` 且 BV 号也一致就不再请求。**换了 ``source`` 就是换了视频**，那时旧封面必须
重抓 —— 所以 BV 变了算未命中（否则 ``--force`` 也刷不掉一张错封面）。一行里存的是 ``{bv, pic}``；
更早的行只有 ``url``（值是"原图 + 老后缀"）也照样读得进来：**剥掉 ``@`` 之后那一段**就是原图
⇒ 不必为一个换了形状的缓存把整包封面重抓一遍（尺寸那些多余的键直接忽略）。
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import pathlib
import re
import threading
import time
import tomllib
import urllib.request

from . import packformat, paths

#: B 站"稿件详情"接口：给 BV 号就能拿到 ``data.pic``（封面原图直链）
API_URL = "https://api.bilibili.com/x/web-interface/view"
#: 浏览器 UA（拿默认的 ``Python-urllib/3.x`` 去请求会吃风控）；**不要**加 ``Origin`` 头（见模块 docstring）
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
REFERER = "https://www.bilibili.com/"
#: `source` 里的 BV 号：``BV`` + 10 位（数字/大写/小写）。**只看子串** ⇒ 链接带了 `?p=2`、
#: 带了 `/` 尾巴、或者是短链跳转后的完整地址，都能提出来。
BV_RE = re.compile(r"BV[0-9A-Za-z]{10}")

#: 行首的 ``[[track]]`` 头（里面允许空白：TOML 的 ``[[ track ]]`` 是合法的）。**行首匹配** ⇒
#: 注释里的 ``# [[track]]`` 不算 —— 数据文件里到处是解释性注释，认错了就会往注释后面插封面。
TRACK_HEADER_RE = re.compile(r"(?m)^[ \t]*\[\[[ \t]*track[ \t]*\]\][ \t]*(?:#.*)?$")
#: 行首的 ``cover = …``（注释掉的不算）；顶层那一条与块里的那些共用这一个正则
COVER_LINE_RE = re.compile(r"(?m)^[ \t]*cover[ \t]*=")
#: 行首的 ``source = …``：新封面插在它**下一行**（与人工写的顺序一致，diff 才好看）
SOURCE_LINE_RE = re.compile(r"(?m)^[ \t]*source[ \t]*=")

DEFAULT_JOBS = 2          # B 站有风控：并发给大了就是 412（抓音频那边同样是这个理由）
DEFAULT_TIMEOUT = 20.0
ATTEMPTS = 3              # 失败重试次数（含第一次）—— 偶发超时/风控退避一下就过去了
BACKOFF = 1.5             # 第 N 次失败后睡 N × 1.5 秒（1.5 / 3.0）
CACHE_NAME = ".covers-cache.jsonl"

#: 旧形状（顶层数组）那两行标记注释的特征串：迁移时**连同数组一起删掉**，不留旧注释。
#: 只认这两句 ⇒ 人工写在旁边的备注不会被误吃。
LEGACY_MARKS = ("每首曲目一张 B 站封面直链", "python -m otomads.fetch_covers")

#: 缓存文件的追加锁：并发抓取时多个线程会同时写（一行一条 JSON，追加是原子的，但还是要串起来）
_CACHE_LOCK = threading.Lock()


# ------------------------------------------------------------------ 纯函数（可离线测）

def extract_bv(source: str) -> str | None:
    """``source`` 里的 BV 号；没有返回 ``None``（那条曲目跳过，不写）。"""
    matched = BV_RE.search(source or "")
    return matched.group(0) if matched else None


def original_url(pic: str) -> str:
    """接口给的 ``data.pic`` → **原图**直链：``http://`` 换 https，**其余一个字符都不动**。

    混内容会被浏览器拦掉（第 2 条坑），所以要换；而**不加任何图床后缀 / 查询参数 / 尺寸**是硬规矩
    —— 裁切与缩放是前端按卡面形状做的事，数据侧只交"未经加工的原图在哪"。
    空串原样返回（``request_pic`` 已经保证非空；这里只是别拼出一个光秃秃的东西）。
    """
    url = (pic or "").strip()
    if not url:
        return ""
    if url.startswith("http://"):
        url = "https://" + url[len("http://"):]
    return url


def track_spans(text: str) -> list[tuple[int, int]]:
    """按 ``[[track]]`` 头把文件文本切成每条曲目的块（半开区间 ``[start, end)``）。

    块的范围到**下一个头**之前（最后一条到文件尾）—— 所以块里带着它自己的空行，
    插哪一行由 :func:`insert_track_cover` 决定。
    """
    heads = [found.start() for found in TRACK_HEADER_RE.finditer(text)]
    return [(start, heads[index + 1] if index + 1 < len(heads) else len(text))
            for index, start in enumerate(heads)]


def _indent_at(text: str, index: int) -> str:
    """``index`` 落在哪一行 → 那一行行首的空白（插新行时跟着它走：文件缩进就缩进）。"""
    start = text.rfind("\n", 0, index) + 1
    return re.match(r"[ \t]*", text[start:]).group(0)


def _value_end(text: str, start: int) -> int:
    """``cover =`` 右边那个值的结束位置：数组 / 内联表 → 配平之后；不是 → 行尾（手写坏了也不吃掉后面）。

    两种复合值都要认：``--force`` 要能整条换掉手写的**旧数组**（``cover = [`` … ``]``）**和**
    **旧的三帧表**（``cover = { … }``）—— 只认数组的话，表会被当成"到行尾为止"，
    只要表跨了行就会留下半截 ``}``，整个文件读不动。
    """
    index = start
    while index < len(text) and text[index] in " \t":
        index += 1
    if index < len(text) and text[index] in "[{":
        depth, in_string = 0, False
        while index < len(text):
            char = text[index]
            if in_string:                     # 字符串里的括号不算配平（也别被转义引号骗到）
                if char == "\\":
                    index += 2
                    continue
                if char == '"':
                    in_string = False
            elif char == '"':
                in_string = True
            elif char in "[{":
                depth += 1
            elif char in "]}":
                depth -= 1
                if depth == 0:
                    index += 1
                    break
            index += 1
        else:
            index = len(text)                 # 括号没配平（手写坏了）：吃到文件尾，别再乱猜
    while index < len(text) and text[index] in " \t":
        index += 1                            # 该行剩下的空白（行尾空格）一并吃掉，但不吃换行
    line_end = text.find("\n", index)
    return len(text) if line_end < 0 else line_end


def insert_track_cover(block: str, cover: str) -> str:
    """把 ``cover = "…"`` 插进**一条** ``[[track]]`` 块的文本：``source`` 行的**下一行**。

    没有 ``source`` 就接在块的**有效内容末尾**（尾部空行留在后面 —— 否则封面会与它那条曲目分家，
    diff 里看着像别人的）。缩进跟着 ``source``（没有就跟 ``[[track]]`` 头）那一行走；
    值由 `packformat.toml_str` 渲染成**一行**（直链里真出现 ``"`` 或 ``\\`` 也写不坏）。

    纯函数（不读盘、不写盘）：用例直接拿"改前 / 改后"两份文本对比，钉住"其它一个字节都不变"。
    """
    if COVER_LINE_RE.search(block):
        # 兜底：调用方本来只该在"这一条还没有 cover"时调这里 —— 写第二行会让 TOML 整份读不动
        raise SystemExit("这条 [[track]] 里已经有 cover 了，不能再插一行（插了 TOML 会直接读不动）")
    found = SOURCE_LINE_RE.search(block)
    if found is not None:                     # 插在 `source` 的**下一行**
        indent = _indent_at(block, found.start())
        end = block.find("\n", found.start())
        line = f"{indent}cover = {packformat.toml_str(cover)}"
        if end < 0:                           # source 是块的最后一行且没有换行（文件结尾）
            return f"{block}\n{line}"
        # `source` 那一行的换行符留在原处、插进去的也自带一个 —— 换行总数不变，
        # 原本紧跟 `source` 的那一行（`start_time` / 下一个 `[[track]]` / 空行）一个字节都不动。
        # ⚠️ 少了插进去那个 `\n` 就会把 cover 与下一行的键**拼成一行**（实测：`start_time` 那几首
        # 直接变成读不动的 TOML 了）。
        return f"{block[:end + 1]}{line}\n{block[end + 1:]}"
    body = block.rstrip("\n")                 # 没有 source：接在有效内容末尾，尾部空行留在后面
    indent = re.match(r"[ \t]*", block).group(0)
    return f"{body}\n{indent}cover = {packformat.toml_str(cover)}{block[len(body):]}"


def replace_track_cover(block: str, cover: str) -> str:
    """把块里已有的 ``cover = …`` **整条值**换掉（只有 ``--force`` 走这里）。

    值可能是手写的多行数组（``cover = [`` … ``]``）或旧的多行三帧表 ⇒ 用 :func:`_value_end`
    配平地吃到最后，不会留下半个括号。行首缩进保留，该行**其它部分**（值、行尾空白）会被换掉
    —— 这正是 ``--force`` 说明里那句"**会盖掉手改**"。块里没有 ``cover`` 行时退化成插入。
    """
    found = COVER_LINE_RE.search(block)
    if found is None:
        return insert_track_cover(block, cover)
    indent = _indent_at(block, found.start())
    return (f"{block[:found.start()]}{indent}cover = {packformat.toml_str(cover)}"
            f"{block[_value_end(block, found.end()):]}")


def set_track_cover(text: str, index: int, cover: str, *, replace: bool = False) -> str:
    """把第 ``index`` 条 ``[[track]]``（从 0 数）的 ``cover`` 写成 ``cover``。纯函数。

    * ``replace=False``（默认）**只插** —— 那一条已经有 ``cover`` 时会报错（宁可炸，也不写第二行）；
    * ``replace=True``：把已有的整条换掉（``--force``）。

    ``cover`` 是一条**裸原图直链**（见 :func:`original_url`）。按 :func:`track_spans` 定位，
    所以**行内**的 ``# [[track]]`` 注释、跨行数组都骗不到它。
    """
    spans = track_spans(text)
    if not 0 <= index < len(spans):
        raise SystemExit(f"角色文件里没有第 {index + 1} 条 [[track]]（只有 {len(spans)} 条）—— 不敢乱插")
    start, end = spans[index]
    block = text[start:end]
    moved = replace_track_cover(block, cover) if replace else insert_track_cover(block, cover)
    return f"{text[:start]}{moved}{text[end:]}"


def has_legacy_cover(text: str) -> bool:
    """文件文本里有没有**顶层**（第一个 ``[[track]]`` 之前）的 ``cover = …``。

    D153 之前的形状。注释掉的不算，块里的（每条曲目自己那份）也不算 —— 只认顶层那一条。
    """
    head = TRACK_HEADER_RE.search(text)
    limit = head.start() if head else len(text)
    found = COVER_LINE_RE.search(text)
    return found is not None and found.start() < limit


def drop_legacy_cover(text: str) -> str:
    """删掉顶层那一段 ``cover = [...]``（连同本工具生成的两行标记注释与它前后的空行）。

    只吃**特征串对得上**的注释行（见 `LEGACY_MARKS`）：人工写在旁边的备注原样留着。
    只认第一个 ``[[track]]`` 之前的 ``cover`` ⇒ 块里那些是每条曲目自己的封面，一个都不碰。
    """
    head = TRACK_HEADER_RE.search(text)
    limit = head.start() if head else len(text)
    found = COVER_LINE_RE.search(text)
    if found is None or found.start() >= limit:
        return text
    start = found.start()
    above = text[:start]
    while above.endswith("\n"):                # 往上一行行吃：只吃本工具生成的那两行注释
        line_start = above.rfind("\n", 0, len(above) - 1) + 1
        previous = above[line_start:-1]
        if not previous.startswith("#") or not any(mark in previous for mark in LEGACY_MARKS):
            break
        start, above = line_start, text[:line_start]
    tail = text[_value_end(text, found.end()):].lstrip("\n")
    above_text = text[:start].rstrip("\n")
    lead = "\n\n" if above_text and tail else ("\n" if above_text else "")
    return f"{above_text}{lead}{tail}"


def migrate_legacy_cover(text: str, where: str) -> str:
    """旧形状（顶层 ``cover = [...]`` 数组）→ 新形状（第 i 条搬进第 i 条 ``[[track]]``）。纯函数。

    * **不联网**、**幂等**：已经搬过的文件原样返回（第二次跑一个字节都不变）；
    * 条数与曲目数**对不上**、顶层与条目里的 ``cover`` 同时存在（手工改了一半）、或者曲目与
      文本里数出来的 ``[[track]]`` 头对不上 ⇒ ``SystemExit``：位置对不上时"第 i 条给谁"
      没有正确答案，猜错就是整包图错位**还不报错**。调用方负责"报错就一个字节都不动"。
    """
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise SystemExit(f"{where}: TOML 读不动（{error}）—— 先手工修好它再重跑") from None
    if "cover" not in data:
        return text
    urls = data["cover"]
    entries = data.get("track", [])
    if not isinstance(urls, list) or any(not isinstance(url, str) or not url.strip() for url in urls):
        raise SystemExit(f"{where}: 顶层的 cover 必须是字符串数组（旧形状），收到 {urls!r} —— "
                         f"手工修一下再重跑（这个文件一个字节都没动）")
    if len(urls) != len(entries):
        raise SystemExit(f"{where}: 顶层的 cover 有 {len(urls)} 条，而文件里有 {len(entries)} 首曲目 —— "
                         f"旧形状靠**位置**一一对应，条数对不上就不知道该把哪一条给谁"
                         f"（这个文件一个字节都没动；对不上通常意味着加/删过曲目）")
    mixed = [entry.get("title") for entry in entries if "cover" in entry]
    if mixed:
        raise SystemExit(f"{where}: 顶层 cover 与 [[track]] 里的 cover 同时存在（手工改了一半？"
                         f"「{mixed[0]}」这条已经写在自己的条目里了）—— 先手工删掉一个再重跑"
                         f"（这个文件一个字节都没动）")
    spans = track_spans(text)
    if len(spans) != len(entries):
        raise SystemExit(f"{where}: 解析出 {len(entries)} 条 [[track]]，文本里却数到 {len(spans)} 个头 —— "
                         f"不敢改（这个文件一个字节都没动）")
    moved = text
    for index in reversed(range(len(entries))):   # **倒着**插：前面的块下标才不会失效
        moved = set_track_cover(moved, index, urls[index].strip(), replace=False)
    return drop_legacy_cover(moved)


def _cache_pic(record: dict) -> str:
    """一条缓存行 → **原图直链**。新行直接给 ``pic``；旧行只有 ``url``（= 原图 + 老后缀）⇒ 剥掉。

    旧后缀（``@703w_1000h_1c.webp``）是"先裁后缩"年代的产物，与现在"原图一个字符都不动"是两回事，
    所以旧行只能当"原图"用；剥不出来（没有 ``@``）就原样当原图。
    """
    pic = (record.get("pic") or "").strip()
    if pic:
        return pic
    url = (record.get("url") or "").strip()
    if not url:
        return ""
    return url.split("@", 1)[0] if "@" in url else url


def read_cache(path: pathlib.Path) -> dict[tuple[str, str], dict]:
    """读缓存 JSONL → ``{(角色 key, 曲名): {"bv": str, "pic": str}}``。

    一行坏了只丢那一行（可续跑的文件不该因为最后一行写了一半就整个不可用），其余照用。
    **旧行照样认**（那时只存 ``url`` = 原图 + 老后缀，可能还带 ``width`` / ``height``）：剥掉后缀当
    原图用、尺寸那些多余的键直接忽略 —— 不必为一个换了形状的缓存把整包封面重抓一遍。
    最后一行赢（同一条曲目重抓过就覆盖前面的）。
    """
    hits: dict[tuple[str, str], dict] = {}
    if not path.exists():
        return hits
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
            key = (record["character"], record["title"])
            pic = _cache_pic(record)
            if not pic:
                continue
        except (ValueError, KeyError, TypeError):
            print(f"[ WARN ] 缓存有一行读不动，跳过：{line[:80]}")
            continue
        hits[key] = {"bv": record.get("bv") or "", "pic": pic}
    return hits


def append_cache(path: pathlib.Path, record: dict) -> None:
    """追加一条成功记录（JSON Lines 就是"一行一条、只追加" ⇒ 中断了也能续跑）。"""
    with _CACHE_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


# ------------------------------------------------------------------ 网络

def request_pic(bv: str, timeout: float = DEFAULT_TIMEOUT) -> str:
    """请求一次详情接口 → ``data.pic``（**原样**，没加后缀）。

    只带 UA 与 Referer：``Origin`` 会让风控回 403 HTML（见模块 docstring 第 1 条）。
    """
    request = urllib.request.Request(f"{API_URL}?bvid={bv}",
                                     headers={"User-Agent": USER_AGENT, "Referer": REFERER})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if payload.get("code") != 0:
        raise RuntimeError(f"接口返回 code={payload.get('code')}（{payload.get('message')}）")
    pic = ((payload.get("data") or {}).get("pic") or "").strip()
    if not pic:
        raise RuntimeError("接口没给 data.pic")
    return pic


def backoff(seconds: float) -> None:
    """失败退避（单独一个函数是给用例 monkeypatch 的：否则失败路径的用例真睡 4.5 秒）。"""
    time.sleep(seconds)


def fetch_pic(bv: str, *, timeout: float = DEFAULT_TIMEOUT, attempts: int = ATTEMPTS) -> str:
    """抓一个 BV 的原图直链（带退避重试）；全失败抛 ``RuntimeError``（调用方不写那一条）。"""
    reason = "?"
    for attempt in range(1, attempts + 1):
        try:
            return original_url(request_pic(bv, timeout))
        except Exception as error:            # 超时 / 风控 / JSON 炸 / 接口带 code —— 一律当"这次失败"
            reason = f"{type(error).__name__}: {error}"
            if attempt < attempts:
                backoff(BACKOFF * attempt)
    raise RuntimeError(f"{attempts} 次都失败（{reason}）")


# ------------------------------------------------------------------ 主流程

def tracks_of(tracks: list[dict], pack_id: str) -> dict[str, list[dict]]:
    """全局曲目表 → ``{角色 key: [曲目, …]}``（**顺序 = 文件里的顺序**，`load_packs` 保证）。

    用 dict 而不是分组排序：`load_packs` 是"文件按名排序、文件内保持原顺序"地追加的，
    所以这里既保住文件内的曲目顺序，也保住角色（文件）之间的稳定顺序 ⇒ ``--dry-run`` 的输出可读。
    **顺序还有硬用处**：它就是这个角色文件里 ``[[track]]`` 的下标，写回时按它定位。
    """
    grouped: dict[str, list[dict]] = {}
    for track in tracks:
        if track["pack"] == pack_id:
            grouped.setdefault(track["character"], []).append(track)
    return grouped


def cache_hit(cache: dict[tuple[str, str], dict], key: str, track: dict, bv: str) -> dict | None:
    """缓存命中 → ``{"bv": …, "pic": …}``；未命中返回 ``None``。

    命中要求 ``(角色 key, 曲名)`` **与 BV 都**一致：换了 ``source`` 就是换了视频，旧封面必须重抓
    （不然 ``--force`` 也刷不掉一张错封面）。
    """
    hit = cache.get((key, track["title"]))
    if hit is None or hit["bv"] != bv or not hit["pic"]:
        return None
    return hit


def fetch_all(jobs: list[tuple[str, dict, str]], cache: dict, cache_path: pathlib.Path,
              *, timeout: float, attempts: int, workers: int,
              write_cache: bool) -> list[tuple[str | None, str | None]]:
    """并发抓一批 ``(角色 key, 曲目, BV)`` → **与入参等长**的 ``[(原图直链, 原因), …]``。

    直链是**裸原图**（:func:`original_url` 的产物）；原因非 ``None`` ⇒ 这条硬失败（直链 ``None``），
    调用方不写那一条、把它报出来、退出码也是 1。

    用 ``pool.map``：结果**按提交顺序**回来（不是完成顺序）⇒ 调用方按位置取用，汇总输出因此是确定的
    （不按 `(角色, 曲名)` 建索引：同一角色里出现两首同名曲目时，那种索引会把两张封面悄悄串成一张）。
    缓存命中就不发请求；成功就记一行（下次连接口都不问）—— 失败的下次重跑自然会再试。
    本模块**只问接口、不下载任何图片**（尺寸是前端的事，见模块 docstring）。
    """
    def one(item: tuple[str, dict, str]) -> tuple[str | None, str | None]:
        key, track, bv = item
        hit = cache_hit(cache, key, track, bv)
        if hit is not None:                           # 缓存命中：连接口都不碰
            return hit["pic"], None
        try:
            pic = fetch_pic(bv, timeout=timeout, attempts=attempts)
        except Exception as error:                    # 超时 / 风控 / JSON 炸 —— 这条不写
            return None, f"{error}"
        if write_cache:
            append_cache(cache_path, {"character": key, "title": track["title"],
                                      "bv": bv, "pic": pic})
            cache[(key, track["title"])] = {"bv": bv, "pic": pic}
        return pic, None

    if not jobs:
        return []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        return list(pool.map(one, jobs))


def migrate_pack(pack_dir: pathlib.Path, *, dry_run: bool) -> tuple[list[str], dict[str, str]]:
    """把曲包里还是**旧形状**的角色文件先搬成新形状 → ``(迁移过的角色 key, {角色 key: 原因})``。

    **必须在 `load_packs` 之前跑**：顶层 ``cover`` 现在是被明确拦下的废弃形状（`packformat`），
    不先搬走连曲目都读不出来。``--dry-run`` 只打印、不落盘；搬不动的文件**一个字节都不动**，
    怎么办由调用方决定（本模块的选择是"整轮都不写"，见 `main`）。
    """
    migrated: list[str] = []
    failed: dict[str, str] = {}
    for path in sorted(pack_dir.glob("*.toml")):
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:             # 读目录与读文件之间被删了：当它不存在
            continue
        if not has_legacy_cover(text):
            continue
        try:
            moved = migrate_legacy_cover(text, where=f"{pack_dir.name}/{path.name}")
        except SystemExit as error:
            failed[path.stem] = str(error)
            continue
        migrated.append(path.stem)
        print(f"  · {path.stem}：{'将迁移' if dry_run else '迁移'}顶层 `cover` 数组 → 各条 `[[track]]`")
        if not dry_run:
            path.write_text(moved, encoding="utf-8")
    return migrated, failed


def read_file(path: pathlib.Path) -> str:
    """读角色文件（不在就报错：曲目表说它有曲目，文件却没了 —— 那是数据坏了，别猜）。"""
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise SystemExit(f"[FAILED] 角色文件不在：{paths.shown(path)}"
                         f"（曲目表说这个角色有曲目，但文件没了）") from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="抓每条曲目的 B 站封面直链，写进 packs/<pack>/<角色 key>.toml 的**那一条 [[track]]**")
    parser.add_argument("--pack", default="otomads", help="曲包 id（默认 otomads）")
    parser.add_argument("--dry-run", action="store_true", help="只打印将要写什么，不落盘（缓存也不写）")
    parser.add_argument("--force", action="store_true",
                        help="整包刷新：按当前 source 的 BV 重抓**每一条**已有的 cover（会盖掉手改）")
    parser.add_argument("--cache", type=pathlib.Path, default=paths.ROOT / CACHE_NAME,
                        help=f"缓存文件（JSON Lines，可续跑；默认 {CACHE_NAME}）")
    parser.add_argument("--jobs", type=int, default=DEFAULT_JOBS,
                        help=f"并发（默认 {DEFAULT_JOBS}；B 站有风控，别给大）")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="单次请求超时秒数")
    args = parser.parse_args(argv)

    manifest = paths.find_pack_manifest(args.pack)
    if manifest is None:
        print(f"[FAILED] 找不到曲包清单：packs/{args.pack}.toml")
        return 2
    pack_dir = manifest.parent / args.pack
    print(f"曲包 {args.pack} → {paths.shown(pack_dir)}/"
          f"{' | --dry-run' if args.dry_run else ''}{' | --force' if args.force else ''}")

    # ① **先搬旧形状**（D153 之前的顶层 `cover` 数组）。**必须在 `load_packs` 之前**：那个键现在是
    #    被明确拦下的废弃形状，不先搬走就读不出曲目。搬不动就整轮不写 —— "第 i 条给谁"没有正确答案，
    #    半搬半不搬只会把数据推到一个更难救的状态。
    migrated, stuck = migrate_pack(pack_dir, dry_run=args.dry_run)
    if stuck:
        for key in sorted(stuck):
            print(f"  [ WARN ] {key}：{stuck[key]}")
        print(f"[FAILED] {len(stuck)} 个角色文件的顶层 cover 搬不动（见上）—— 本次**一个字节都没写**，"
              f"先按提示修一下再重跑")
        return 1

    # ② **故意用宽容读法**：某条曲目还没封面 / 手写坏了时，严格读法会直接报错，而本工具正是来修它的
    #    （其余校验一条不少：未知键、作者、时间、布局照样报）。
    _packs, _albums, tracks, _cards, _covers = packformat.load_packs(validate_covers=False)
    grouped = tracks_of(tracks, args.pack)
    cache = read_cache(args.cache)
    print(f"角色 {len(grouped)} 个 / 曲目 {sum(len(entries) for entries in grouped.values())} 条 | "
          f"缓存 {paths.shown(args.cache)}（{len(cache)} 条）")

    # ③ 逐条排班：**已经有 cover 的一条都不动**（连请求都不发 —— 那是"源内覆写"的人工入口）；
    #    `--force` 才按当前 source 重抓每一条。缺 BV 的条目只算它自己失败，别的照常。
    texts = {key: read_file(pack_dir / f"{key}.toml") for key in grouped}
    plan: list[tuple[str, int, dict, str, bool]] = []   # 角色 / 下标 / 曲目 / BV / 本来有没有
    skipped: dict[str, int] = {}
    failures: list[tuple[str, str, str]] = []           # 角色 / 曲名 / 原因（这一条不写）
    for key, entries in grouped.items():
        data = tomllib.loads(texts[key])
        rows = data.get("track", [])
        if len(rows) != len(entries):
            raise SystemExit(f"{key}.toml：解析出 {len(rows)} 条曲目，曲目表里却是 {len(entries)} 条 —— "
                             f"不敢改（写回是按**下标**对上 `[[track]]` 的）")
        legacy = data.get("cover")                      # `--dry-run` 还没落盘时它还在
        existing: list[object] = []
        for index, (row, track) in enumerate(zip(rows, entries)):
            value = row.get("cover")
            if value is None and isinstance(legacy, list) and index < len(legacy):
                # 还没落盘的旧数组：第 i 条就是这一条的（迁移后它会变成 `row["cover"]`）
                value = legacy[index]
            existing.append(value)
        for index, track in enumerate(entries):
            value = existing[index]
            if value is not None and not args.force:
                skipped[key] = skipped.get(key, 0) + 1
                continue
            bv = extract_bv(track.get("source", ""))
            if bv is None:
                failures.append((key, track["title"], "source 里没有 BV 号"
                                 + ("（已有的 cover 原样留着，没换成）" if value is not None else "")))
                continue
            plan.append((key, index, track, bv, value is not None))

    # ④ 并发抓原图直链（缓存命中不发请求；**一张图都不下载** —— 尺寸是前端的事）
    outcomes = fetch_all([(key, track, bv) for key, _index, track, bv, _had in plan],
                         cache, args.cache, timeout=args.timeout, attempts=ATTEMPTS,
                         workers=max(1, args.jobs), write_cache=not args.dry_run)

    # ⑤ 组装 + 写回（文本级：只在**那一条** `[[track]]` 块里动一行，别的字节一个不动）。
    #    `plan` 与 `outcomes` 同序等长（`pool.map` 按提交顺序回来）⇒ 按下标取，别按 (角色, 曲名)
    #    建索引：同一角色里两首同名曲目会把两张封面悄悄串成一张。
    added = refreshed = 0
    per_char = {key: {"新增": 0, "刷新": 0, "跳过": skipped.get(key, 0)} for key in grouped}
    changed: dict[str, str] = {}
    for (key, index, track, _bv, had), (cover, reason) in zip(plan, outcomes):
        if cover is None:
            failures.append((key, track["title"], reason or "抓取失败"))
            continue
        changed[key] = set_track_cover(changed.get(key, texts[key]), index, cover, replace=had)
        per_char[key]["刷新" if had else "新增"] += 1
        if had:
            refreshed += 1
        else:
            added += 1
    if not args.dry_run:
        for key, text in changed.items():
            (pack_dir / f"{key}.toml").write_text(text, encoding="utf-8")

    for key in grouped:
        counts = per_char[key]
        if not any(counts.values()):
            continue
        bits = [f"{name} {counts[name]}" for name in ("新增", "刷新", "跳过") if counts[name]]
        print(f"  · {key}：{' / '.join(bits)}")
    for key, title, reason in failures:
        print(f"  [ WARN ] {key} / {title}：{reason} → 这一条不写（别的照常）")

    print(f"[  OK  ] {'将' if args.dry_run else ''}新增 {added} 条 / 刷新 {refreshed} 条 / "
          f"迁移 {len(migrated)} 个角色 | 跳过（已有）{sum(skipped.values())} | "
          f"失败 {len(failures)}")
    if failures:
        print("[ WARN ] 失败的条目下次重跑即可（缓存里成功的那几首不会重复请求）；"
              "撞上风控（403/412）就把 --jobs 降到 1 再试")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
