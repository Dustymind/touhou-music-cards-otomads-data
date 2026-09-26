"""逐条封面（每条 `[[track]]` 里自己的 `cover`）与 `otomads.fetch_covers` 的测试。

契约（D153 + 卡面三帧修订）：**一条曲目一份封面**，写在**它自己那条** `[[track]]` 里；
形状是**单链接字符串**（应用运行时裁）或**三帧表**（`original` / `16x9` / `4x3`，
后两帧是**源分辨率**的居中裁切直链 —— 尺寸由原图的像素算出来，永不放大）。
严格读法下**一个角色要么每条都写、要么一条都不写**，而且**每条的帧集合必须一样**
（字符串 = 没有帧）；`pack_snapshot()` 交出去的是"与 `music` 同序的 `covers`（每条一个主链接）
+ `coversByRatio`（按帧拆开的数组）"。

这里**完全不联网**：`request_pic`（接口）与 `request_image`（下原图量尺寸）两层 HTTP
连同 `backoff`（退避睡觉）一律 monkeypatch 掉，连"真跑一次 CLI"的用例也走假网络
—— 用例不许依赖 bilibili 可达、也不许因为风控而红。真图那一条单独用 ffmpeg 生成
（没有 ffmpeg 就跳过）。
"""
from __future__ import annotations

import json
import pathlib
import shutil
import struct
import subprocess
import tomllib

import pytest

from otomads import fetch_covers, packformat as packs

#: 接口给的**原图**直链（`http://` 那条正好练"换成 https"）—— 三帧都是它拼出来的
PIC_A = "https://i0.hdslb.com/bfs/archive/BV1kw411q7S8.jpg"
PIC_B = "https://i0.hdslb.com/bfs/archive/BV1GD4y1m7JY.jpg"
#: 假图床报回来的尺寸：1920×1200 ⇒ 16:9 得 1920×1080、4:3 得 1600×1200
SCREEN = (1920, 1200)
FRAMES_A = fetch_covers.cover_frames(PIC_A, *SCREEN)
FRAMES_B = fetch_covers.cover_frames(PIC_B, *SCREEN)
#: 人工覆写用的直链（与"假图床"给出的一定不同 ⇒ 一眼看得出工具动没动它）
HAND = "https://hand.example/mine.jpg"
HAND_TABLE = {"original": "https://hand.example/mine.jpg",
              "16x9": "https://hand.example/mine.jpg@1920w_1080h_1c.webp",
              "4x3": "https://hand.example/mine.jpg@1600w_1200h_1c.webp"}

#: 一份"真源"角色文件：两首曲目，第二首的 source 带 `?p=2`（提 BV 不能只看链接尾巴）
CHARACTER_FILE = '''# 音MAD 曲包（demo）：`cirno` 的曲目。
# 清单与口径见 `packs/demo.toml` 与 `README.ai.MD`。

key = "cirno"

[[track]]
album = "demo"
author = "甲"
title = "一"
extra = "角色曲"
source = "https://www.bilibili.com/video/BV1kw411q7S8/"

[[track]]
album = "demo"
author = "乙"
title = "二"
extra = "道中曲"
source = "https://www.bilibili.com/video/BV1GD4y1m7JY/?p=2"
'''

#: 同一份文件，但第一首**手工**写了 cover（默认模式必须一个字节都不动它，且不为它发请求）
HAND_ONE = CHARACTER_FILE.replace(
    'source = "https://www.bilibili.com/video/BV1kw411q7S8/"\n',
    f'source = "https://www.bilibili.com/video/BV1kw411q7S8/"\ncover = "{HAND}"\n', 1)

#: D153 之前的形状：顶层 `cover = [...]` 数组（靠**位置**与下面的 `[[track]]` 对应）
LEGACY_FILE = '''# 音MAD 曲包（demo）：`cirno` 的曲目。

key = "cirno"

# 每首曲目一张 B 站封面直链（一首一封面，与下面 [[track]] **顺序一一对应**）。
# 由 `python -m otomads.fetch_covers` 生成；**手改这一段就是覆写**，工具默认不再动它（要刷新用 --force）。
cover = [
  "https://hand.example/one.jpg@703w_1000h_1c.webp",
  "https://hand.example/two.png@703w_1000h_1c.webp",
]

[[track]]
album = "demo"
title = "一"
source = "https://www.bilibili.com/video/BV1kw411q7S8/"

[[track]]
album = "demo"
title = "二"
source = "https://www.bilibili.com/video/BV1GD4y1m7JY/?p=2"
'''

#: `LEGACY_FILE` 迁移后**该长成的样子**：顶层那一段（含两行标记注释）没了，
#: 第 i 条原样进了第 i 条 `[[track]]`，其它字节一个没动（值原样搬，不改成新形状）
LEGACY_MIGRATED = '''# 音MAD 曲包（demo）：`cirno` 的曲目。

key = "cirno"

[[track]]
album = "demo"
title = "一"
source = "https://www.bilibili.com/video/BV1kw411q7S8/"
cover = "https://hand.example/one.jpg@703w_1000h_1c.webp"

[[track]]
album = "demo"
title = "二"
source = "https://www.bilibili.com/video/BV1GD4y1m7JY/?p=2"
cover = "https://hand.example/two.png@703w_1000h_1c.webp"
'''


# ------------------------------------------------------------------ 假图（只用 struct 拼头部）

def png_bytes(width: int, height: int) -> bytes:
    """最小 PNG 头：签名 + IHDR（宽高是大端 u32）。"""
    return (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR"
            + struct.pack(">II", width, height) + b"\x08\x06\x00\x00\x00")


def jpeg_bytes(width: int, height: int) -> bytes:
    """最小 JPEG：SOI + APP0(JFIF) + SOF0 + EOI（SOF 段里就是高 / 宽）。"""
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof0 = (b"\xff\xc0" + struct.pack(">H", 17) + b"\x08" + struct.pack(">HH", height, width)
            + b"\x03" + b"\x01\x11\x00" + b"\x02\x11\x01" + b"\x03\x11\x01")
    return b"\xff\xd8" + app0 + sof0 + b"\xff\xd9"


def gif_bytes(width: int, height: int) -> bytes:
    """最小 GIF：签名 + 逻辑屏幕描述符（宽高是小端 u16）。"""
    return b"GIF89a" + struct.pack("<HH", width, height) + b"\x00\x00\x00"


def webp_bytes(width: int, height: int, kind: str = "VP8X") -> bytes:
    """最小 WebP：RIFF 头 + 一个图片块（有损 / 无损 / 扩展三种头都不一样）。"""
    if kind == "VP8X":
        payload = b"\x00\x00\x00\x00" + (width - 1).to_bytes(3, "little") \
            + (height - 1).to_bytes(3, "little")
    elif kind == "VP8L":
        bits = (width - 1) | ((height - 1) << 14)
        payload = b"\x2f" + struct.pack("<I", bits)
    else:                                     # `VP8 `（有损）：帧标签 3 字节 + 起始码 + 各 14 位
        payload = b"\x00\x00\x00" + b"\x9d\x01\x2a" + struct.pack("<HH", width, height)
    fourcc = kind.encode()
    body = fourcc + struct.pack("<I", len(payload)) + payload
    return b"RIFF" + struct.pack("<I", len(body) + 4) + b"WEBP" + body


def write_pack(tmp_path: pathlib.Path, filename: str, body: str) -> pathlib.Path:
    """在临时"数据仓库"里写一个角色文件（清单固定 `packs/demo.toml`），返回它的路径。"""
    packs_dir = tmp_path / "packs"
    (packs_dir / "demo").mkdir(parents=True, exist_ok=True)
    (packs_dir / "demo.toml").write_text('[pack]\nid = "demo"\n', encoding="utf-8")
    path = packs_dir / "demo" / filename
    path.write_text(body, encoding="utf-8")
    return path


@pytest.fixture
def repo(tmp_path, monkeypatch) -> pathlib.Path:
    """临时数据仓库 + 一份两首曲目的角色文件；`paths.DATA` 指过去、退避换成"不睡"。"""
    path = write_pack(tmp_path, "cirno.toml", CHARACTER_FILE)
    monkeypatch.setattr(fetch_covers.paths, "DATA", tmp_path)
    monkeypatch.setattr(fetch_covers, "backoff", lambda _seconds: None)
    return path


def stub_network(monkeypatch, *, fail: frozenset[str] = frozenset(),
                 image_fail: frozenset[str] = frozenset(),
                 size: tuple[int, int] = SCREEN) -> tuple[list[str], list[str]]:
    """顶掉两层 HTTP：接口（BV → 假 `pic`）与图床（原图 → 假 PNG 头）。

    返回 ``(接口收到的 BV, 图床收到的原图 URL)`` 两份记录 —— "缓存命中就不再下载那张图"
    这条得靠第二个列表才验得了（第一版只记了接口，图下没下根本看不出来）。
    """
    calls: list[str] = []
    images: list[str] = []

    def fake_request(bv: str, timeout: float = fetch_covers.DEFAULT_TIMEOUT) -> str:
        calls.append(bv)
        if bv in fail:
            raise RuntimeError("HTTP Error 403: Forbidden（风控）")
        return f"http://i0.hdslb.com/bfs/archive/{bv}.jpg"

    def fake_image(url: str, timeout: float = fetch_covers.DEFAULT_TIMEOUT) -> bytes:
        images.append(url)
        if any(bv in url for bv in image_fail):
            raise RuntimeError("HTTP Error 404: Not Found")
        return png_bytes(*size)

    monkeypatch.setattr(fetch_covers, "request_pic", fake_request)
    monkeypatch.setattr(fetch_covers, "request_image", fake_image)
    return calls, images


def run(tmp_path: pathlib.Path, *extra: str) -> int:
    """跑一次 CLI（缓存固定落在 tmp 里，别碰真仓库根那份）。"""
    return fetch_covers.main(["--pack", "demo", "--cache", str(tmp_path / "covers.jsonl"), *extra])


def covers_of(path: pathlib.Path) -> list:
    """角色文件里**逐条**的 `cover`（没有这条键的曲目给 `None`）—— 检查形状用。"""
    return [entry.get("cover") for entry in tomllib.loads(path.read_text(encoding="utf-8"))["track"]]


def covers_of_text(text: str) -> list:
    return [entry.get("cover") for entry in tomllib.loads(text)["track"]]


def cover_line(frames_or_url) -> str:
    """工具该写出来的那一行（值 + 渲染交给纯函数，用例只钉位置与周围字节）。"""
    return f"cover = {fetch_covers.toml_cover(frames_or_url)}"


# ------------------------------------------------------------------ 纯函数：提 BV / 原图直链

@pytest.mark.parametrize(("source", "expected"), [
    ("https://www.bilibili.com/video/BV1kw411q7S8/", "BV1kw411q7S8"),
    ("https://www.bilibili.com/video/BV1GD4y1m7JY/?p=2", "BV1GD4y1m7JY"),   # 多 P：BV 照样提得出来
    ("BV1kw411q7S8", "BV1kw411q7S8"),                                       # 只写 BV 号
    ("https://www.bilibili.com/video/av12345", None),                       # 老 av 号：提不出来（跳过那条）
    ("https://example.com/a", None),
    ("", None),
    ("BV1kw411q7S8x", "BV1kw411q7S8"),                                      # 恰好 10 位，多的不吃
])
def test_extract_bv(source, expected):
    assert fetch_covers.extract_bv(source) == expected


def test_original_url_upgrades_http_and_adds_no_suffix():
    """`original` 这一帧就是**原图**：只把 `http://` 换成 https，**一个后缀都不加**。

    老版本的 `@703w_1000h_1c.webp`（先裁后缩）已经作废：应用要的是能自己裁的源，
    裁切改成另外两帧、尺寸按原图算（见 `crop_size`）。
    """
    assert fetch_covers.original_url(PIC_A.replace("https://", "http://", 1)) == PIC_A
    assert fetch_covers.original_url(PIC_A) == PIC_A
    assert "@" not in fetch_covers.original_url(PIC_A)
    assert fetch_covers.original_url("") == ""


# ------------------------------------------------------------------ 纯函数：源分辨率裁切

@pytest.mark.parametrize(("size", "ratio", "expected"), [
    ((1920, 1200), (16, 9), (1920, 1080)),      # 比 16:9 高 ⇒ 按宽度对齐
    ((1920, 1200), (4, 3), (1600, 1200)),       # 比 4:3 宽 ⇒ 按高度对齐
    ((1920, 1080), (16, 9), (1920, 1080)),      # **本来就是 16:9**：原样，不裁也不放
    ((1000, 1000), (16, 9), (1000, 562)),       # 正方形：宽度顶满
    ((703, 1000), (16, 9), (703, 395)),         # **比 4:3 还窄**：宽度顶满，绝不放大到 1777
    ((703, 1000), (4, 3), (703, 527)),          #      4:3 同理（不是 1333×1000）
    ((4000, 2250), (16, 9), (4000, 2250)),      # 4K 16:9 原样
])
def test_crop_size_never_upscales(size, ratio, expected):
    """裁切尺寸 = 原图里**装得下**的最大目标比例：两条边都取 min ⇒ 永远不超过原图。"""
    assert fetch_covers.crop_size(*size, *ratio) == expected


def test_crop_size_returns_zero_for_a_degenerate_original():
    """小到裁不出这个比例时返回 0（调用方当"量不出来"处理 —— 后缀里不能出现 `0w`）。"""
    assert fetch_covers.crop_size(10, 10, 16, 9) == (10, 5)
    assert fetch_covers.crop_size(1, 1, 16, 9)[1] == 0


def test_cover_frames_are_source_resolution_crops_of_the_original():
    """三帧：裸原图 + 两个**源分辨率**的裁切直链（后缀语法与老版本同一个，尺寸换了）。"""
    frames = fetch_covers.cover_frames(PIC_A, 1920, 1200)

    assert frames == {
        "original": PIC_A,
        "16x9": PIC_A + "@1920w_1080h_1c.webp",
        "4x3": PIC_A + "@1600w_1200h_1c.webp",
    }
    assert list(frames) == list(fetch_covers.COVER_FRAMES)      # 键序固定（写回与快照都靠它）


def test_cover_frames_upgrades_http_before_cropping():
    """`http://` 的原图：三帧都得是 https（混内容会被浏览器拦掉），别只换 `original` 那一帧。"""
    frames = fetch_covers.cover_frames(PIC_A.replace("https://", "http://", 1), 800, 600)

    assert frames["original"] == PIC_A
    assert frames["16x9"] == PIC_A + "@800w_450h_1c.webp"
    assert all(url.startswith("https://") for url in frames.values())


def test_cover_frames_refuses_a_size_that_crops_to_nothing():
    with pytest.raises(ValueError, match="裁不出"):
        fetch_covers.cover_frames(PIC_A, 1, 1)


# ------------------------------------------------------------------ 纯函数：TOML 值与形状

def test_toml_cover_renders_one_inline_table_line():
    """表写成**一行**内联表：`16x9` / `4x3` 是引号键（数字开头），`original` 是裸键。"""
    line = fetch_covers.toml_cover(FRAMES_A)

    assert "\n" not in line
    assert line == (f'{{ original = "{PIC_A}", '
                    f'"16x9" = "{PIC_A}@1920w_1080h_1c.webp", '
                    f'"4x3" = "{PIC_A}@1600w_1200h_1c.webp" }}')
    assert tomllib.loads(f"cover = {line}")["cover"] == FRAMES_A      # 真能被 TOML 读回来


def test_toml_cover_renders_a_single_frame_table_and_a_plain_string():
    assert fetch_covers.toml_cover({"original": PIC_A}) == f'{{ original = "{PIC_A}" }}'
    assert fetch_covers.toml_cover(HAND) == f'"{HAND}"'
    # 直链里真出现引号也写不坏（转义走 ingest_pack.toml_str）
    assert tomllib.loads(f"cover = {fetch_covers.toml_cover('https://x/a\"b.jpg')}")["cover"] \
        == 'https://x/a"b.jpg'


@pytest.mark.parametrize(("shape", "expected"), [
    (None, FRAMES_A),                                    # 这个角色一条封面都没有 ⇒ 完整三帧表
    ("https://hand.example/x.jpg", PIC_A),               # 已有的是字符串 ⇒ 新补的也写字符串
    ({"original": "https://hand.example/x.jpg"}, {"original": PIC_A}),        # 手写的单帧表跟着它
    ({"16x9": "https://hand.example/x.jpg@1w_1h_1c.webp"},
     {"16x9": PIC_A + "@1920w_1080h_1c.webp"}),
    (["http://broken"], FRAMES_A),                       # 认不出来的形状（宽容读法才放得进来）⇒ 完整表
])
def test_shaped_cover_follows_the_characters_existing_shape(shape, expected):
    """**一个角色里只能有一种写法、同一套帧** ⇒ 新写的必须长得像留下的那几条。"""
    assert fetch_covers.shaped_cover(FRAMES_A, shape) == expected


# ------------------------------------------------------------------ 图片尺寸（只用标准库 struct）

@pytest.mark.parametrize(("data", "expected"), [
    (png_bytes(1920, 1200), (1920, 1200)),
    (jpeg_bytes(1920, 1200), (1920, 1200)),          # SOF0 在 APP0 后面：不扫段就找不到
    (gif_bytes(703, 1000), (703, 1000)),
    (webp_bytes(1920, 1200, "VP8 "), (1920, 1200)),
    (webp_bytes(1920, 1200, "VP8L"), (1920, 1200)),
    (webp_bytes(1920, 1200, "VP8X"), (1920, 1200)),
    (jpeg_bytes(1, 1), (1, 1)),                      # 极端值也能读（不放大是裁切那边的事）
])
def test_image_size_reads_every_supported_header(data, expected):
    assert fetch_covers.image_size(data) == expected


@pytest.mark.parametrize("data", [
    b"",
    b"not an image at all",
    b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR",                    # PNG 头被截断
    b"\xff\xd8" + b"\xff\xc0" + struct.pack(">H", 17),               # JPEG 的 SOF 被截断
    b"\xff\xd8\xff\xd9",                                             # 只有 SOI + EOI
    b"RIFF\x00\x00\x00\x00WEBPX",                                    # 不认识的 WebP 块
    b"RIFF\x00\x00\x00\x00WEBPVP8 ",                                 # WebP 头被截断
])
def test_image_size_refuses_what_it_cannot_read(data):
    """认不出来就报 —— **绝不猜**一个尺寸（猜错 = 图被拉变形，而且没人会报错）。"""
    with pytest.raises(ValueError):
        fetch_covers.image_size(data)


@pytest.mark.skipif(shutil.which("ffmpeg") is None,
                    reason="需要真的 ffmpeg 生成真图 —— 这一条**故意不做替身**（替身测不出解析对不对）")
@pytest.mark.parametrize(("ext", "expected"), [("png", SCREEN), ("jpg", SCREEN),
                                               ("webp", SCREEN), ("gif", (703, 1000))])
def test_image_size_agrees_with_ffmpeg(tmp_path, ext, expected):
    """真图（ffmpeg 生成）+ 真解析：手写头部那条只能证明"代码按我以为的格式写"。

    GIF 里 `SCREEN` 那种 1920×1200 会被 ffmpeg 警告"尺寸不能被 2 整除"，所以 GIF 用 703×1000。
    """
    path = tmp_path / f"pic.{ext}"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
                    "-i", f"color=c=red:s={expected[0]}x{expected[1]}:d=1", "-frames:v", "1",
                    str(path)], check=True)

    assert fetch_covers.image_size(path.read_bytes()) == expected


# ------------------------------------------------------------------ 纯函数：文本级写回

def test_track_spans_ignores_a_commented_out_header():
    """`# [[track]]` 只是注释：认错了就会把封面插到注释后面去（数据文件里到处是这种注释）。"""
    text = '# 说明：[[track]] 下面才是曲目\nkey = "cirno"\n\n[[track]]\nalbum = "demo"\n'
    assert len(fetch_covers.track_spans(text)) == 1


def test_insert_track_cover_writes_one_inline_table_under_source():
    """新形状：`source` 的下一行、**一行**内联表；文件里其它字节一个不动。"""
    block = '[[track]]\nalbum = "demo"\ntitle = "一"\nsource = "https://example.com/a"\n'
    line = cover_line(FRAMES_A) + "\n"

    after = fetch_covers.insert_track_cover(block, FRAMES_A)

    assert after == block.replace('source = "https://example.com/a"\n',
                                  'source = "https://example.com/a"\n' + line, 1)
    assert after.count("\n") == block.count("\n") + 1                 # 只多了一行
    assert covers_of_text(f'key = "x"\n\n{after}') == [FRAMES_A]      # 真能被 TOML 读回来


def test_insert_track_cover_keeps_a_string_cover_a_plain_string():
    """回退 / 跟形状：字符串封面还是老写法（一行、一个值），不会莫名变成表。"""
    block = '[[track]]\nalbum = "demo"\nsource = "https://example.com/a"\n'

    after = fetch_covers.insert_track_cover(block, HAND)

    assert after == block.replace('source = "https://example.com/a"\n',
                                  f'source = "https://example.com/a"\ncover = "{HAND}"\n', 1)


def test_insert_track_cover_keeps_the_next_key_on_its_own_line():
    """**回归守卫**：`source` 下一行就是 `start_time` 时，插进去的 cover 必须自成一行。

    少写那个换行符就会拼成 `cover = "…"start_time = "…"` —— 整个文件直接读不动
    （真数据里 6 首带裁剪区间的曲目全中招，离线演练时才抓到）。
    """
    block = ('[[track]]\nalbum = "demo"\ntitle = "一"\n'
             'source = "https://example.com/a"\nstart_time = "00:00:10.000"\n')

    after = fetch_covers.insert_track_cover(block, FRAMES_A)

    assert f'{cover_line(FRAMES_A)}\nstart_time' in after
    assert tomllib.loads(f'key = "x"\n\n{after}')["track"][0]["start_time"] == "00:00:10.000"


def test_insert_track_cover_appends_at_the_end_of_a_block_without_source():
    """没有 `source` 就接在块的末尾：尾部空行留着（否则封面会跟它那条曲目分家）。"""
    block = '[[track]]\nalbum = "demo"\ntitle = "一"\n\n'

    after = fetch_covers.insert_track_cover(block, FRAMES_A)

    assert after == f'[[track]]\nalbum = "demo"\ntitle = "一"\n{cover_line(FRAMES_A)}\n\n'


def test_insert_track_cover_follows_the_indentation_of_the_block():
    """缩进跟着文件里的实际写法走（两个空格就两个空格），不硬写一种风格。"""
    block = '  [[track]]\n  album = "demo"\n  source = "https://example.com/a"\n'

    after = fetch_covers.insert_track_cover(block, FRAMES_A)

    assert f'  source = "https://example.com/a"\n  {cover_line(FRAMES_A)}\n' in after


def test_set_track_cover_targets_one_track_and_is_idempotent_on_replace():
    """`set_track_cover` 只动第 i 条；`replace=True` 跑两次字节完全相同（幂等）。"""
    once = fetch_covers.set_track_cover(CHARACTER_FILE, 1, FRAMES_B, replace=True)

    assert covers_of_text(once) == [None, FRAMES_B]
    assert once == CHARACTER_FILE.replace('source = "https://www.bilibili.com/video/BV1GD4y1m7JY/?p=2"\n',
                                          'source = "https://www.bilibili.com/video/BV1GD4y1m7JY/?p=2"\n'
                                          + cover_line(FRAMES_B) + "\n", 1)
    assert fetch_covers.set_track_cover(once, 1, FRAMES_B, replace=True) == once


@pytest.mark.parametrize("value", [
    '[\n  "https://hand.example/x.jpg",\n  "https://hand.example/y.jpg",\n]',   # 旧的多行数组
    '{\n  original = "https://hand.example/x.jpg",\n}',                        # 跨行的内联表
    '{\n  original = "https://hand.example/x.jpg",\n  "16x9" = "https://hand.example/y.jpg" }',
])
def test_replace_track_cover_swallows_any_compound_value(value):
    """整条换掉 —— 留半个 `]` / `}` 在文件里就是读不动的 TOML（两种复合值都踩过）。"""
    block = ('[[track]]\nalbum = "demo"\ntitle = "一"\nsource = "https://example.com/a"\n'
             f'cover = {value}\nstart_time = "00:00:10.000"\n')

    after = fetch_covers.replace_track_cover(block, FRAMES_A)

    assert "hand.example" not in after and after.count("cover =") == 1
    assert tomllib.loads(f'key = "x"\n\n{after}')["track"][0] == {
        "album": "demo", "title": "一", "source": "https://example.com/a",
        "cover": FRAMES_A, "start_time": "00:00:10.000"}


def test_insert_track_cover_refuses_to_write_a_second_cover_line():
    """兜底：那一条已经有 cover 了就不许再插一行（插了 TOML 整份读不动）。"""
    block = f'[[track]]\nalbum = "demo"\ncover = "{HAND}"\n'
    with pytest.raises(SystemExit, match="已经有 cover"):
        fetch_covers.insert_track_cover(block, FRAMES_A)


# ------------------------------------------------------------------ 纯函数：旧形状迁移

def test_migrate_legacy_cover_moves_each_url_into_its_track():
    """第 i 条搬进第 i 条 `[[track]]`（插在 `source` 下一行），顶层那一段连注释一起消失。

    搬过去的值**原样**（还是 `...@703w_1000h_1c.webp` 那种旧后缀）：迁移只搬位置、不改内容 ——
    形状换代是 `--force` 的事（默认模式连请求都不发）。
    """
    assert fetch_covers.migrate_legacy_cover(LEGACY_FILE, "demo/cirno.toml") == LEGACY_MIGRATED


def test_migrate_legacy_cover_is_idempotent():
    """再跑一次字节相同（已经搬过的文件原样返回）—— 迁移本身**不联网**。"""
    once = fetch_covers.migrate_legacy_cover(LEGACY_FILE, "demo/cirno.toml")

    assert fetch_covers.migrate_legacy_cover(once, "demo/cirno.toml") == once
    assert fetch_covers.has_legacy_cover(once) is False


def test_migrate_legacy_cover_refuses_a_length_mismatch():
    """条数对不上 ⇒ 报错：位置对不上时"第 i 条给谁"没有正确答案，猜错就是整包图错位还不报错。"""
    broken = LEGACY_FILE.replace('''[[track]]
album = "demo"
title = "二"
source = "https://www.bilibili.com/video/BV1GD4y1m7JY/?p=2"
''', "")
    with pytest.raises(SystemExit, match="2 条.*1 首曲目"):
        fetch_covers.migrate_legacy_cover(broken, "demo/cirno.toml")


def test_migrate_legacy_cover_refuses_a_half_migrated_file():
    """顶层数组与条目里的 cover 同时存在（手工改了一半）⇒ 报错，别猜哪个是对的。"""
    half = LEGACY_FILE.replace('title = "一"\n', f'title = "一"\ncover = "{HAND}"\n', 1)
    with pytest.raises(SystemExit, match="同时存在"):
        fetch_covers.migrate_legacy_cover(half, "demo/cirno.toml")


def test_drop_legacy_cover_keeps_a_hand_written_note():
    """人工写在数组上面的备注**不许被吃掉**（只吃本工具生成的那两行标记注释）。"""
    noted = LEGACY_FILE.replace("# 每首曲目一张", "# 人工备注：这两张我自己挑的\n# 每首曲目一张", 1)

    after = fetch_covers.drop_legacy_cover(noted)

    assert "# 人工备注：这两张我自己挑的" in after
    assert "hand.example/one.jpg" not in after
    assert fetch_covers.has_legacy_cover(after) is False


# ------------------------------------------------------------------ 形状校验（load_packs）

def test_load_packs_rejects_the_legacy_top_level_array_with_a_way_out(tmp_path, monkeypatch):
    """旧形状**不许**退化成"不认识的键 ['cover']"：报错要说清怎么迁移（照做就能过）。"""
    write_pack(tmp_path, "cirno.toml", LEGACY_FILE)
    monkeypatch.setattr(packs.repo, "DATA", tmp_path)

    with pytest.raises(SystemExit, match="顶层的 `cover` 数组已废弃"):
        packs.load_packs()
    with pytest.raises(SystemExit, match="fetch_covers"):
        packs.load_packs()


def test_load_packs_reads_covers_in_track_order(tmp_path, monkeypatch):
    """每条曲目自己那份 cover → `covers[key]` 的顺序 = 文件里的曲目顺序（与 `tracks` 同序）。

    表与字符串**不能混**，所以这里两条都是表，但帧集合不同 ⇒ 也不行：第二条只给 `original`
    是"帧写一半"，会被专门那条校验拦下 —— 这是刻意的（见下面那条用例）。
    """
    two = CHARACTER_FILE.replace(
        'source = "https://www.bilibili.com/video/BV1kw411q7S8/"\n',
        'source = "https://www.bilibili.com/video/BV1kw411q7S8/"\n'
        + cover_line(FRAMES_A) + "\n", 1).replace(
        'source = "https://www.bilibili.com/video/BV1GD4y1m7JY/?p=2"\n',
        'source = "https://www.bilibili.com/video/BV1GD4y1m7JY/?p=2"\n'
        + cover_line(FRAMES_B) + "\n", 1)
    write_pack(tmp_path, "cirno.toml", two)
    write_pack(tmp_path, "marisa.toml", 'key = "marisa"\n\n[[track]]\nalbum = "demo"\ntitle = "三"\n')
    monkeypatch.setattr(packs.repo, "DATA", tmp_path)

    _packs, _albums, tracks, _cards, covers = packs.load_packs()

    assert covers == {"cirno": [FRAMES_A, FRAMES_B]}
    assert [track["title"] for track in tracks if track["character"] == "cirno"] == ["一", "二"]
    assert "marisa" not in covers            # 一条都没写的角色不进表（缺省 = 没有封面）


def test_load_packs_accepts_a_character_with_string_covers(tmp_path, monkeypatch):
    """全字符串（上一版工具留下的形状 / 量不到尺寸时的兜底）照样合法：应用运行时自己裁。"""
    write_pack(tmp_path, "cirno.toml", HAND_ONE.replace(
        'title = "二"\n', f'title = "二"\ncover = "{PIC_B}"\n', 1))
    monkeypatch.setattr(packs.repo, "DATA", tmp_path)

    _packs, _albums, _tracks, _cards, covers = packs.load_packs()

    assert covers == {"cirno": [HAND, PIC_B]}


def test_load_packs_accepts_a_single_frame_table(tmp_path, monkeypatch):
    """只写一帧的表也合法（**每条都一样**就行）：应用缺的那两帧自己裁。"""
    one = {"original": PIC_A}
    write_pack(tmp_path, "cirno.toml", CHARACTER_FILE.replace(
        'source = "https://www.bilibili.com/video/BV1kw411q7S8/"\n',
        'source = "https://www.bilibili.com/video/BV1kw411q7S8/"\n' + cover_line(one) + "\n", 1)
        .replace('source = "https://www.bilibili.com/video/BV1GD4y1m7JY/?p=2"\n',
                 'source = "https://www.bilibili.com/video/BV1GD4y1m7JY/?p=2"\n'
                 + cover_line({"original": PIC_B}) + "\n", 1))
    monkeypatch.setattr(packs.repo, "DATA", tmp_path)

    assert packs.load_packs()[4] == {"cirno": [one, {"original": PIC_B}]}


def test_load_packs_accepts_a_character_with_no_covers_at_all(tmp_path, monkeypatch):
    write_pack(tmp_path, "cirno.toml", CHARACTER_FILE)
    monkeypatch.setattr(packs.repo, "DATA", tmp_path)

    _packs, _albums, tracks, _cards, covers = packs.load_packs()

    assert len(tracks) == 2 and covers == {}


@pytest.mark.parametrize("line", [
    'cover = []',                                    # 数组 = 还以为是"一首一封面"那种形状
    'cover = ""',                                    # 空串 = 写了一半
    'cover = "   "',                                 # 全是空白同理
    'cover = 3',                                     # 数字：形状都不对
    'cover = {}',                                    # 空表：一帧都没有
])
def test_load_packs_rejects_a_cover_that_is_not_a_non_empty_value(tmp_path, monkeypatch, line):
    write_pack(tmp_path, "cirno.toml",
               f'key = "cirno"\n\n[[track]]\nalbum = "demo"\ntitle = "一"\n{line}\n')
    monkeypatch.setattr(packs.repo, "DATA", tmp_path)

    with pytest.raises(SystemExit):
        packs.load_packs()


def test_load_packs_rejects_an_unknown_frame_key(tmp_path, monkeypatch):
    """写错的帧名必须报：静默忽略 = 运行时少一帧，光看数据文件根本看不出来。"""
    write_pack(tmp_path, "cirno.toml",
               'key = "cirno"\n\n[[track]]\nalbum = "demo"\ntitle = "第一首"\n'
               f'cover = {{ original = "{PIC_A}", "16x10" = "{PIC_A}" }}\n')
    monkeypatch.setattr(packs.repo, "DATA", tmp_path)

    with pytest.raises(SystemExit, match="不认识的帧"):
        packs.load_packs()
    with pytest.raises(SystemExit, match="16x10"):
        packs.load_packs()


@pytest.mark.parametrize("value", ['""', '"   "', "3", "[\"https://x/y.jpg\"]"])
def test_load_packs_rejects_an_empty_or_non_string_frame_value(tmp_path, monkeypatch, value):
    write_pack(tmp_path, "cirno.toml",
               'key = "cirno"\n\n[[track]]\nalbum = "demo"\ntitle = "一"\n'
               f'cover = {{ original = {value} }}\n')
    monkeypatch.setattr(packs.repo, "DATA", tmp_path)

    with pytest.raises(SystemExit, match="非空字符串"):
        packs.load_packs()


def test_load_packs_rejects_a_cover_that_is_not_https(tmp_path, monkeypatch):
    """`http://` 不算绝对 https URL（混内容会被拦）；错误信息要**说清**怎么改、且带曲名。"""
    write_pack(tmp_path, "cirno.toml",
               'key = "cirno"\n\n[[track]]\nalbum = "demo"\ntitle = "第一首"\n'
               'cover = "http://i0.hdslb.com/a.jpg"\n')
    monkeypatch.setattr(packs.repo, "DATA", tmp_path)

    with pytest.raises(SystemExit, match="https:// 开头"):
        packs.load_packs()
    with pytest.raises(SystemExit, match="第一首"):
        packs.load_packs()
    with pytest.raises(SystemExit, match="http:// 要换成 https://"):
        packs.load_packs()


def test_load_packs_rejects_an_http_frame_inside_a_table(tmp_path, monkeypatch):
    """表里的每一帧都得单独过 https 那一关（别只查 `original`）。"""
    write_pack(tmp_path, "cirno.toml",
               'key = "cirno"\n\n[[track]]\nalbum = "demo"\ntitle = "一"\n'
               f'cover = {{ original = "{PIC_A}", "16x9" = "http://i0.hdslb.com/a.jpg" }}\n')
    monkeypatch.setattr(packs.repo, "DATA", tmp_path)

    with pytest.raises(SystemExit, match="https:// 开头"):
        packs.load_packs()


def test_load_packs_rejects_a_half_covered_character_and_names_the_track(tmp_path, monkeypatch):
    """**全有或全无**：有的有、有的没有 ⇒ 报错点名缺的那一首，并给一条能照做的提示。

    半有半无在运行时的 `covers` 数组里就是**空洞**：应用按同一个下标取图 ⇒ 后面每张都串位，
    而且没有任何东西会报错。
    """
    write_pack(tmp_path, "cirno.toml", HAND_ONE)      # 第一条有（人工的）、第二条没有
    monkeypatch.setattr(packs.repo, "DATA", tmp_path)

    with pytest.raises(SystemExit, match="全有或全无"):
        packs.load_packs()
    with pytest.raises(SystemExit, match="「二」"):
        packs.load_packs()
    with pytest.raises(SystemExit, match="fetch_covers"):
        packs.load_packs()


def test_load_packs_rejects_a_mixed_string_and_table_in_one_character(tmp_path, monkeypatch):
    """字符串（= 没有帧）与表混用 ⇒ 报错：`coversByRatio` 会与 `covers` 静默错位。"""
    mixed = HAND_ONE.replace('title = "二"\n', 'title = "二"\n' + cover_line(FRAMES_B) + "\n", 1)
    write_pack(tmp_path, "cirno.toml", mixed)
    monkeypatch.setattr(packs.repo, "DATA", tmp_path)

    with pytest.raises(SystemExit, match="混用"):
        packs.load_packs()
    with pytest.raises(SystemExit, match="「二」"):
        packs.load_packs()
    with pytest.raises(SystemExit, match="--force"):
        packs.load_packs()


def test_load_packs_rejects_a_frame_only_some_tracks_have(tmp_path, monkeypatch):
    """**同一条曲目的表少一帧**（别人都是三帧）⇒ 报错：那个帧的数组就是空洞。

    这是"帧写一半"最隐蔽的形态：值都是对的，只有一条少了一帧。
    """
    partial = {"original": PIC_B, "16x9": PIC_B + "@1920w_1080h_1c.webp"}
    text = CHARACTER_FILE.replace(
        'source = "https://www.bilibili.com/video/BV1kw411q7S8/"\n',
        'source = "https://www.bilibili.com/video/BV1kw411q7S8/"\n' + cover_line(FRAMES_A) + "\n", 1)
    text = text.replace('title = "二"\n', 'title = "二"\n' + cover_line(partial) + "\n", 1)
    write_pack(tmp_path, "cirno.toml", text)
    monkeypatch.setattr(packs.repo, "DATA", tmp_path)

    with pytest.raises(SystemExit, match="同一套帧"):
        packs.load_packs()
    with pytest.raises(SystemExit, match="帧集合不一样"):
        packs.load_packs()
    with pytest.raises(SystemExit, match="「二」"):
        packs.load_packs()


def test_tolerant_read_lets_the_repair_tool_in(tmp_path, monkeypatch):
    """宽容读法（只有 `fetch_covers` 用）：半有半无、混用、旧形状都读得进来，只是不记 `covers`。

    没有它，工具就卡在"报错让你重跑 fetch_covers、它自己又因为同一条报错起不来"的死循环里。
    """
    write_pack(tmp_path, "cirno.toml", HAND_ONE)
    write_pack(tmp_path, "marisa.toml", LEGACY_FILE.replace("cirno", "marisa"))
    write_pack(tmp_path, "sakuya.toml", CHARACTER_FILE.replace("cirno", "sakuya").replace(
        'title = "二"\n', 'title = "二"\n' + cover_line(FRAMES_B) + "\n", 1))
    monkeypatch.setattr(packs.repo, "DATA", tmp_path)

    _packs, _albums, tracks, _cards, covers = packs.load_packs(validate_covers=False)

    assert len(tracks) == 6 and covers == {}
    # 其它校验一条不少：未知键照样报
    write_pack(tmp_path, "youmu.toml", 'key = "youmu"\n\n[[track]]\nalbum = "demo"\ntitle = "x"\n'
                                       'cover = "https://a/b.jpg"\nstarttime = "00:00:01.000"\n')
    with pytest.raises(SystemExit, match="不认识的键"):
        packs.load_packs(validate_covers=False)


# ------------------------------------------------------------------ CLI 端到端（假网络）

def test_a_new_file_gets_a_three_frame_table_per_track(repo, tmp_path, monkeypatch, capsys):
    """逐条补缺：插在 `source` 下一行，值是**三帧表**（原图 + 两个源分辨率裁切）。"""
    calls, images = stub_network(monkeypatch)

    assert run(tmp_path) == 0

    after = repo.read_text(encoding="utf-8")
    assert covers_of(repo) == [FRAMES_A, FRAMES_B]
    # **逐字对比**：除插进去的那两行，文件其余部分一个字节都没变
    assert after == CHARACTER_FILE.replace(
        'source = "https://www.bilibili.com/video/BV1kw411q7S8/"\n',
        'source = "https://www.bilibili.com/video/BV1kw411q7S8/"\n' + cover_line(FRAMES_A) + "\n",
        1).replace(
        'source = "https://www.bilibili.com/video/BV1GD4y1m7JY/?p=2"\n',
        'source = "https://www.bilibili.com/video/BV1GD4y1m7JY/?p=2"\n' + cover_line(FRAMES_B) + "\n",
        1)
    assert sorted(calls) == ["BV1GD4y1m7JY", "BV1kw411q7S8"]          # 每条曲目恰好请求一次
    assert sorted(images) == sorted([PIC_A, PIC_B])                            # 原图各下一张（量尺寸）
    assert packs.load_packs()[4] == {"cirno": [FRAMES_A, FRAMES_B]}    # 严格读法也过得去
    printed = capsys.readouterr().out
    assert "新增 2 条" in printed and "跳过（已有）0" in printed and "失败 0" in printed


def test_rerun_is_byte_identical_and_sends_no_request(repo, tmp_path, monkeypatch, capsys):
    """幂等：第二次一个字节都不变，而且已有 cover 的一条**连请求都不发**。"""
    stub_network(monkeypatch)
    assert run(tmp_path) == 0
    once = repo.read_text(encoding="utf-8")
    calls, images = stub_network(monkeypatch)         # 换一份计数（顺带清掉"假图床"的记录）

    assert run(tmp_path) == 0

    assert repo.read_text(encoding="utf-8") == once
    assert calls == [] and images == []
    assert "跳过（已有）2" in capsys.readouterr().out


def test_existing_cover_is_never_touched_and_never_requested(repo, tmp_path, monkeypatch, capsys):
    """默认**只补没有的**：手工改过的那一条原样留着，工具**不为它发请求**。"""
    repo.write_text(HAND_ONE, encoding="utf-8")
    calls, images = stub_network(monkeypatch)

    assert run(tmp_path) == 0

    after = repo.read_text(encoding="utf-8")
    # 第一条是**手写的字符串** ⇒ 新补的第二条也写字符串（一个角色里不许两种写法混用）
    assert covers_of(repo) == [HAND, PIC_B]
    assert after == HAND_ONE.replace(
        'source = "https://www.bilibili.com/video/BV1GD4y1m7JY/?p=2"\n',
        f'source = "https://www.bilibili.com/video/BV1GD4y1m7JY/?p=2"\ncover = "{PIC_B}"\n', 1)
    assert calls == ["BV1GD4y1m7JY"]                   # 只补了缺的那一条
    assert images == [PIC_B]
    printed = capsys.readouterr().out
    assert "新增 1 条" in printed and "跳过（已有）1" in printed


def test_a_new_track_follows_the_existing_frame_set(repo, tmp_path, monkeypatch):
    """**跟形状**：已经写了单帧表的角色，新补的那条也只写那一帧（否则整只角色就非法了）。"""
    repo.write_text(CHARACTER_FILE.replace(
        'source = "https://www.bilibili.com/video/BV1kw411q7S8/"\n',
        'source = "https://www.bilibili.com/video/BV1kw411q7S8/"\n' + cover_line(
            {"original": HAND}) + "\n", 1), encoding="utf-8")
    stub_network(monkeypatch)

    assert run(tmp_path) == 0

    assert covers_of(repo) == [{"original": HAND}, {"original": PIC_B}]
    assert packs.load_packs()[4] == {"cirno": [{"original": HAND}, {"original": PIC_B}]}


def test_force_refreshes_every_existing_cover_including_hand_edits(repo, tmp_path, monkeypatch):
    """`--force` = 整包刷新：**已有的每一条**都按当前 source 的 BV 重抓（会盖掉手改）。"""
    repo.write_text(HAND_ONE, encoding="utf-8")
    calls, _images = stub_network(monkeypatch)

    assert run(tmp_path, "--force") == 0

    assert covers_of(repo) == [FRAMES_A, FRAMES_B]
    assert HAND not in repo.read_text(encoding="utf-8")
    assert sorted(calls) == ["BV1GD4y1m7JY", "BV1kw411q7S8"]           # 人工那条也重抓了


def test_force_heals_a_hand_written_broken_cover(repo, tmp_path, monkeypatch):
    """**回归守卫**：手写坏的 cover（`http://`、或者干脆是数组）也要能被 `--force` 修好。

    严格读法会把这种文件判死，而工具正是来修它的 ⇒ 它必须走宽容读法才起得来。
    """
    repo.write_text(CHARACTER_FILE.replace(
        'source = "https://www.bilibili.com/video/BV1kw411q7S8/"\n',
        'source = "https://www.bilibili.com/video/BV1kw411q7S8/"\n'
        'cover = ["http://i0.hdslb.com/hand.jpg"]\n', 1), encoding="utf-8")
    with pytest.raises(SystemExit):
        packs.load_packs()                             # 消费方口径：硬失败
    stub_network(monkeypatch)

    assert run(tmp_path, "--force") == 0

    assert covers_of(repo) == [FRAMES_A, FRAMES_B]
    assert packs.load_packs()[4] == {"cirno": [FRAMES_A, FRAMES_B]}


def test_force_never_deletes_a_cover_it_cannot_refresh(repo, tmp_path, monkeypatch, capsys):
    """`--force` 也刷不动"没有 BV"的那一条：**已有的 cover 原样留着**（报出来，但绝不删数据）。

    `--force` 的语义是"按当前 source 重抓"，不是"清空重来"：`source` 没了就没得重抓，
    这时把人工挑的那张图删掉是最坏的结果。

    留下的那条是**字符串** ⇒ 这一轮新写的那条也得跟着写字符串（一个角色里两种写法混用是
    硬错误），所以刷新出来的那条这次只拿到裸原图、没长成三帧表 —— 工具会把这件事说出来。
    """
    both = HAND_ONE.replace('title = "二"\n', f'title = "二"\ncover = "{PIC_B}"\n', 1).replace(
        'source = "https://www.bilibili.com/video/BV1GD4y1m7JY/?p=2"',
        'source = "https://www.bilibili.com/video/av12345"')
    repo.write_text(both, encoding="utf-8")
    calls, _images = stub_network(monkeypatch)

    assert run(tmp_path, "--force") == 1

    assert covers_of(repo) == [PIC_A, PIC_B]           # 第一条刷新了（跟着留下的字符串形状）
    assert "hand.example" not in repo.read_text(encoding="utf-8")
    assert calls == ["BV1kw411q7S8"]
    assert packs.load_packs()[4] == {"cirno": [PIC_A, PIC_B]}    # 整只角色仍是一种写法 ⇒ 合法
    printed = capsys.readouterr().out
    assert "原样留着" in printed and "失败 1" in printed
    assert "跟着同角色已有的" in printed and "--force" in printed


def test_a_failed_measurement_falls_back_to_a_single_string_link(repo, tmp_path, monkeypatch, capsys):
    """**量不到尺寸 ⇒ 回退成单链接字符串**（裸原图，应用运行时裁），并逐条报出原因、退出码 1。

    宁可少两帧，也不猜一个会把图拉变形的尺寸；接口那次请求是成功的 ⇒ 缓存里只记原图
    （尺寸给 null），下次重跑只补量尺寸、不再问接口（见下一条）。
    """
    calls, images = stub_network(monkeypatch, image_fail=frozenset({PIC_B}))

    assert run(tmp_path) == 1

    assert covers_of(repo) == [FRAMES_A, PIC_B]        # 好的是表，坏的是裸原图字符串
    printed = capsys.readouterr().out
    assert "量不到原图尺寸" in printed and "已回退成单链接字符串" in printed and "回退 1" in printed
    assert calls.count("BV1GD4y1m7JY") == 1            # 接口只问了一次（成功）
    assert images.count(PIC_B) == fetch_covers.ATTEMPTS   # 图床那边退避重试试满了
    records = {record["title"]: record for record in
               (json.loads(line) for line in (tmp_path / "covers.jsonl").read_text(
                   encoding="utf-8").splitlines())}
    assert set(records) == {"一", "二"}
    assert (records["二"]["pic"], records["二"]["width"], records["二"]["height"]) \
        == (PIC_B, None, None)                          # 原图记下了、尺寸还没有


def test_a_fallback_is_healed_on_the_next_run_without_asking_the_api_again(repo, tmp_path, monkeypatch):
    """图床恢复后重跑：**接口一次都不问**（缓存里有原图），只补量尺寸，三帧表长回来。"""
    stub_network(monkeypatch, image_fail=frozenset({PIC_B}))
    assert run(tmp_path) == 1
    calls, images = stub_network(monkeypatch)

    assert run(tmp_path, "--force") == 0

    assert covers_of(repo) == [FRAMES_A, FRAMES_B]      # 第一条缓存命中，第二条补上三帧
    assert calls == []                                 # 接口一次都没问
    assert images == [PIC_B]                            # 只有缺尺寸的那张图重下一次


def test_legacy_file_is_migrated_without_any_request(repo, tmp_path, monkeypatch, capsys):
    """旧形状：顶层数组按顺序搬进各条 `[[track]]`，**一条请求都不发**（值算"已有"）。"""
    repo.write_text(LEGACY_FILE, encoding="utf-8")
    calls, images = stub_network(monkeypatch)

    assert run(tmp_path) == 0

    assert repo.read_text(encoding="utf-8") == LEGACY_MIGRATED
    assert calls == [] and images == []
    printed = capsys.readouterr().out
    assert "迁移 1 个角色" in printed and "跳过（已有）2" in printed


def test_a_track_added_after_migration_is_filled_alone(repo, tmp_path, monkeypatch):
    """迁移之后再手工加一首（没写 cover）⇒ 重跑只补那一条；第三次跑字节完全相同。

    这就是 README 里那套流程的形状：**加曲目 → 跑一次 fetch_covers**（旧的几条一个都不动）。
    旧那两条是**字符串**（迁移搬过来的旧后缀）⇒ 新补的这条也写字符串，整只角色保持合法。
    """
    repo.write_text(LEGACY_FILE, encoding="utf-8")
    calls, images = stub_network(monkeypatch)
    assert run(tmp_path) == 0
    assert calls == [] and images == []                # 迁移不联网
    assert repo.read_text(encoding="utf-8") == LEGACY_MIGRATED

    repo.write_text(LEGACY_MIGRATED + '\n[[track]]\nalbum = "demo"\ntitle = "三"\n'
                    'source = "https://www.bilibili.com/video/BV1xx411c7mD"\n', encoding="utf-8")
    calls.clear()
    images.clear()

    assert run(tmp_path) == 0

    assert covers_of(repo) == ["https://hand.example/one.jpg@703w_1000h_1c.webp",
                               "https://hand.example/two.png@703w_1000h_1c.webp",
                               "https://i0.hdslb.com/bfs/archive/BV1xx411c7mD.jpg"]
    assert calls == ["BV1xx411c7mD"]                   # 只补新加的那一条
    once = repo.read_text(encoding="utf-8")

    assert run(tmp_path) == 0

    assert repo.read_text(encoding="utf-8") == once    # 幂等：一个字节都不变


def test_force_refreshes_the_values_it_just_migrated(repo, tmp_path, monkeypatch):
    """`--force` 也要先迁移：搬进来的值算"已有"，然后按 BV 全部刷新成三帧表。"""
    repo.write_text(LEGACY_FILE, encoding="utf-8")
    calls, _images = stub_network(monkeypatch)

    assert run(tmp_path, "--force") == 0

    assert covers_of(repo) == [FRAMES_A, FRAMES_B]
    assert "hand.example" not in repo.read_text(encoding="utf-8")
    assert sorted(calls) == ["BV1GD4y1m7JY", "BV1kw411q7S8"]


def test_dry_run_writes_nothing_not_even_the_cache(repo, tmp_path, monkeypatch, capsys):
    """`--dry-run`：只打印计划 —— 连缓存都不落盘（"不落盘"就是字面意思）。"""
    stub_network(monkeypatch)

    assert run(tmp_path, "--dry-run") == 0

    assert repo.read_text(encoding="utf-8") == CHARACTER_FILE
    assert not (tmp_path / "covers.jsonl").exists()
    printed = capsys.readouterr().out
    assert "新增 2 条" in printed and "✅ 将新增 2 条" in printed


def test_dry_run_migrates_nothing_on_disk(repo, tmp_path, monkeypatch, capsys):
    """`--dry-run` 遇到旧形状：只报"将迁移"，文件与缓存一个字节都不动。"""
    repo.write_text(LEGACY_FILE, encoding="utf-8")
    calls, _images = stub_network(monkeypatch)

    assert run(tmp_path, "--dry-run") == 0

    assert repo.read_text(encoding="utf-8") == LEGACY_FILE
    assert not (tmp_path / "covers.jsonl").exists()
    assert calls == []
    printed = capsys.readouterr().out
    assert "将迁移" in printed and "迁移 1 个角色" in printed and "跳过（已有）2" in printed


def test_migration_failure_writes_nothing_at_all(repo, tmp_path, monkeypatch, capsys):
    """迁移搬不动（条数对不上）⇒ 报错、退出码 1，而且**这一轮一个字节都不写**。

    半搬半不搬会把数据推到一个更难救的状态（一半文件新形状、一半旧形状），所以宁可整轮不写。
    """
    broken = LEGACY_FILE.replace('''[[track]]
album = "demo"
title = "二"
source = "https://www.bilibili.com/video/BV1GD4y1m7JY/?p=2"
''', "")
    repo.write_text(broken, encoding="utf-8")
    other = write_pack(tmp_path, "marisa.toml", 'key = "marisa"\n\n[[track]]\nalbum = "demo"\n'
                                                'title = "三"\nsource = "https://www.bilibili.com/video/BV1xx411c7mD"\n')
    calls, images = stub_network(monkeypatch)

    assert run(tmp_path) == 1

    assert repo.read_text(encoding="utf-8") == broken
    assert other.read_text(encoding="utf-8").endswith('source = "https://www.bilibili.com/video/BV1xx411c7mD"\n')
    assert calls == [] and images == []                # 一条请求都没发
    assert not (tmp_path / "covers.jsonl").exists()
    printed = capsys.readouterr().out
    assert "搬不动" in printed and "一个字节都没写" in printed


def test_a_track_without_a_bv_is_skipped_alone(repo, tmp_path, monkeypatch, capsys):
    """缺 BV ⇒ **只**跳过那一条（别的照常写），退出码 1，原因里点名是哪首。"""
    repo.write_text(CHARACTER_FILE.replace(
        'source = "https://www.bilibili.com/video/BV1GD4y1m7JY/?p=2"',
        'source = "https://www.bilibili.com/video/av12345"'), encoding="utf-8")
    calls, _images = stub_network(monkeypatch)

    assert run(tmp_path) == 1

    assert covers_of(repo) == [FRAMES_A, None]         # 好的那条照常写，坏的那条没动
    assert calls == ["BV1kw411q7S8"]                   # 缺 BV 的那条连请求都不发
    printed = capsys.readouterr().out
    assert "没有 BV 号" in printed and "二" in printed
    assert "新增 1 条" in printed and "失败 1" in printed


def test_a_failed_fetch_does_not_block_the_other_tracks(repo, tmp_path, monkeypatch, capsys):
    """网络失败 ⇒ 那一条不写（下次重跑即可），其它条照常；退出码 1。"""
    calls, images = stub_network(monkeypatch, fail=frozenset({"BV1GD4y1m7JY"}))

    assert run(tmp_path) == 1

    assert covers_of(repo) == [FRAMES_A, None]
    assert calls.count("BV1GD4y1m7JY") == fetch_covers.ATTEMPTS        # 退避重试确实试满了
    assert calls.count("BV1kw411q7S8") == 1                            # 成功的那条只请求一次
    assert images == [PIC_A]                                           # 失败那条连图都没下
    printed = capsys.readouterr().out
    assert "这一条不写（别的照常）" in printed and "失败 1" in printed


def test_failed_fetch_retries_with_backoff(repo, tmp_path, monkeypatch):
    """退避重试的间隔是 1.5 × 第几次（别把 B 站当无限重试的靶子）。"""
    slept: list[float] = []
    monkeypatch.setattr(fetch_covers, "backoff", slept.append)
    stub_network(monkeypatch, fail=frozenset({"BV1kw411q7S8", "BV1GD4y1m7JY"}))

    assert run(tmp_path) == 1

    assert sorted(slept) == sorted([fetch_covers.BACKOFF, fetch_covers.BACKOFF * 2] * 2)


def test_a_character_without_tracks_is_left_alone(tmp_path, monkeypatch, capsys):
    """骨架文件（没有曲目）不碰：它连一条 `[[track]]` 都没有，没什么可补的。"""
    repo = write_pack(tmp_path, "cirno.toml", CHARACTER_FILE)
    skeleton = write_pack(tmp_path, "marisa.toml", 'key = "marisa"\n')
    monkeypatch.setattr(fetch_covers.paths, "DATA", tmp_path)
    monkeypatch.setattr(fetch_covers, "backoff", lambda _seconds: None)
    stub_network(monkeypatch)

    assert run(tmp_path) == 0

    assert skeleton.read_text(encoding="utf-8") == 'key = "marisa"\n'
    assert "marisa" not in capsys.readouterr().out


# ------------------------------------------------------------------ 缓存（JSON Lines）

def test_cache_is_jsonl_with_the_pic_and_both_dimensions(repo, tmp_path, monkeypatch):
    """缓存一行 = 原图 + 尺寸 + BV：尺寸只对那张原图成立，所以三者绑在一条记录里。"""
    _calls, images = stub_network(monkeypatch)

    assert run(tmp_path) == 0

    records = [json.loads(line) for line in (tmp_path / "covers.jsonl").read_text(
        encoding="utf-8").splitlines()]
    assert {(record["character"], record["title"]) for record in records} == {("cirno", "一"), ("cirno", "二")}
    assert all(record["pic"].startswith("https://") for record in records)
    assert {(record["width"], record["height"]) for record in records} == {SCREEN}
    assert len(images) == 2


def test_a_cache_hit_downloads_no_image_and_asks_no_api(repo, tmp_path, monkeypatch):
    """缓存命中：**两层网络都不碰**（接口不问、原图不再下一次）—— `--force` 也一样。"""
    stub_network(monkeypatch)
    assert run(tmp_path) == 0

    calls, images = stub_network(monkeypatch)          # 换一份计数
    assert run(tmp_path, "--force") == 0

    assert calls == [] and images == []
    assert covers_of(repo) == [FRAMES_A, FRAMES_B]     # 值从缓存里的 pic + 尺寸重算


def test_cache_misses_when_the_source_bv_changed(repo, tmp_path, monkeypatch):
    """换了 `source` 就是换了视频 ⇒ 缓存必须算未命中（否则 `--force` 也刷不掉一张错封面）。"""
    calls, images = stub_network(monkeypatch)
    assert run(tmp_path) == 0

    repo.write_text(CHARACTER_FILE.replace("BV1kw411q7S8", "BV1zz411q7S9"), encoding="utf-8")
    calls.clear()
    images.clear()
    assert run(tmp_path, "--force") == 0

    assert calls == ["BV1zz411q7S9"]                     # 只重抓换过的那一条
    assert images == ["https://i0.hdslb.com/bfs/archive/BV1zz411q7S9.jpg"]   # 尺寸也重量了
    assert covers_of(repo)[0] == fetch_covers.cover_frames(
        "https://i0.hdslb.com/bfs/archive/BV1zz411q7S9.jpg", *SCREEN)


def test_an_old_cache_line_only_gets_its_image_measured_again(repo, tmp_path, monkeypatch):
    """**旧缓存行**（只有 `url` = 原图 + 老后缀）：剥掉后缀当原图用 ⇒ 只补量一次尺寸，接口不再问。"""
    (tmp_path / "covers.jsonl").write_text("\n".join(json.dumps(record, ensure_ascii=False) for record in [
        {"character": "cirno", "title": "一", "bv": "BV1kw411q7S8", "url": PIC_A + fetch_covers.LEGACY_SUFFIX},
        {"character": "cirno", "title": "二", "bv": "BV1GD4y1m7JY", "url": PIC_B},
    ]) + "\n", encoding="utf-8")
    calls, images = stub_network(monkeypatch)

    assert run(tmp_path) == 0

    assert covers_of(repo) == [FRAMES_A, FRAMES_B]
    assert calls == []                                 # 接口一次都没问
    assert sorted(images) == sorted([PIC_A, PIC_B])            # 两张原图各量一次（旧行没有尺寸）

    # 量完就补成新行：下一次连图都不下了
    calls, images = stub_network(monkeypatch)
    assert run(tmp_path, "--force") == 0
    assert calls == [] and images == []


def test_read_cache_survives_a_half_written_line(tmp_path):
    """JSONL 是"可续跑"的文件：最后一行写了一半也不该让整个缓存作废。"""
    cache = tmp_path / "covers.jsonl"
    cache.write_text(json.dumps({"character": "cirno", "title": "一", "bv": "BVx",
                                 "pic": PIC_A, "width": 1920, "height": 1200},
                                ensure_ascii=False) + "\n" + '{"character": "cirno", "tit',
                     encoding="utf-8")

    assert fetch_covers.read_cache(cache) == {
        ("cirno", "一"): {"bv": "BVx", "pic": PIC_A, "size": (1920, 1200)}}


def test_read_cache_keeps_the_last_line_for_the_same_track(tmp_path):
    """同一条曲目重抓过 ⇒ 后写的那行赢（JSONL 只追加，靠顺序定新旧）。"""
    cache = tmp_path / "covers.jsonl"
    cache.write_text("\n".join(json.dumps(record, ensure_ascii=False) for record in [
        {"character": "cirno", "title": "一", "bv": "BVx", "pic": PIC_A, "width": 1, "height": 1},
        {"character": "cirno", "title": "一", "bv": "BVx", "pic": PIC_B, "width": 1920, "height": 1200},
    ]) + "\n", encoding="utf-8")

    assert fetch_covers.read_cache(cache) == {
        ("cirno", "一"): {"bv": "BVx", "pic": PIC_B, "size": (1920, 1200)}}


def test_cached_pic_is_reused_without_touching_the_network(repo, tmp_path, monkeypatch):
    """缓存里的原图直接拿来算三帧（`--dry-run` 不写缓存，但读缓存照旧）。"""
    (tmp_path / "covers.jsonl").write_text("\n".join(json.dumps(record, ensure_ascii=False) for record in [
        {"character": "cirno", "title": "一", "bv": "BV1kw411q7S8", "pic": PIC_A, "width": 1920, "height": 1200},
        {"character": "cirno", "title": "二", "bv": "BV1GD4y1m7JY", "pic": PIC_B, "width": 800, "height": 600},
    ]) + "\n", encoding="utf-8")
    calls, images = stub_network(monkeypatch)

    assert run(tmp_path) == 0

    assert covers_of(repo) == [FRAMES_A, fetch_covers.cover_frames(PIC_B, 800, 600)]
    assert calls == [] and images == []


# ------------------------------------------------------------------ 真源 TOML → 快照形状

def test_snapshot_from_the_real_toml_carries_covers_and_covers_by_ratio(repo, tmp_path, monkeypatch):
    """端到端：真源 TOML（三帧表）→ `load_packs` → `pack_snapshot` 的两份封面数组。

    ``covers`` 每条一个**主链接**（这里都是 `original`），``coversByRatio`` 按帧拆开、
    **与 `music` / `covers` 同序**；形状能进 `json.dumps`（源要把它塞进 `manifest.json`）。
    """
    stub_network(monkeypatch)
    assert run(tmp_path) == 0

    _packs, albums, tracks, cards, covers = packs.load_packs()
    snapshot = packs.pack_snapshot(albums, tracks, cards, covers)

    assert snapshot["characters"] == [{
        "key": "cirno",
        "music": [["demo", "一", "角色曲", "甲"], ["demo", "二", "道中曲", "乙"]],
        "covers": [PIC_A, PIC_B],
        "coversByRatio": {
            "original": [PIC_A, PIC_B],
            "16x9": [PIC_A + "@1920w_1080h_1c.webp", PIC_B + "@1920w_1080h_1c.webp"],
            "4x3": [PIC_A + "@1600w_1200h_1c.webp", PIC_B + "@1600w_1200h_1c.webp"],
        },
    }]
    json.dumps(snapshot)                                  # 真能进 manifest.json
    # 键序是确定的（写出来的 manifest 才稳）：original → 16x9 → 4x3
    assert list(snapshot["characters"][0]["coversByRatio"]) == list(packs.COVER_FRAMES)


def test_snapshot_omits_covers_by_ratio_for_string_covers(albums=[]):
    """字符串封面（旧形状 / 回退）⇒ 只有 `covers`，**不带** `coversByRatio`（应用自己裁）。"""
    tracks = [{"character": "cirno", "album": "demo", "title": "一", "extra": "角色曲"}]
    snapshot = packs.pack_snapshot(albums, tracks, None, {"cirno": [HAND]})

    assert snapshot["characters"] == [{"key": "cirno",
                                       "music": [["demo", "一", "角色曲"]],
                                       "covers": [HAND]}]


def test_snapshot_publishes_only_the_frames_the_pack_declares():
    """只写一帧的角色：`coversByRatio` 就只有那一帧（**不补齐**，别凭空造出没写过的 URL）。"""
    tracks = [{"character": "cirno", "album": "demo", "title": "一", "extra": "角色曲"},
              {"character": "cirno", "album": "demo", "title": "二", "extra": "角色曲"}]
    one = {"original": PIC_A}
    two = {"original": PIC_B}
    snapshot = packs.pack_snapshot([], tracks, None, {"cirno": [one, two]})

    assert snapshot["characters"][0]["covers"] == [PIC_A, PIC_B]
    assert snapshot["characters"][0]["coversByRatio"] == {"original": [PIC_A, PIC_B]}


def test_snapshot_primary_link_prefers_original_then_the_crops():
    """主链接的优先级：`original` → `16x9` → `4x3`（原图分辨率最高，应用拿它自己裁最稳）。"""
    assert packs.primary_cover({"16x9": "w", "4x3": "f"}) == "w"
    assert packs.primary_cover({"4x3": "f"}) == "f"
    assert packs.primary_cover({"original": "o", "16x9": "w"}) == "o"
    assert packs.primary_cover(HAND) == HAND


def test_snapshot_never_publishes_a_misaligned_ratio_array():
    """兜底：手工拼的输入里只要有一条缺某个帧，那个帧就**整条不收**（数组少一项就是静默串位）。"""
    tracks = [{"character": "cirno", "album": "demo", "title": "一", "extra": "角色曲"},
              {"character": "cirno", "album": "demo", "title": "二", "extra": "角色曲"}]
    snapshot = packs.pack_snapshot([], tracks, None, {"cirno": [
        {"original": PIC_A, "16x9": PIC_A + "@1w_1h_1c.webp"},
        {"original": PIC_B}]})

    assert snapshot["characters"][0]["coversByRatio"] == {"original": [PIC_A, PIC_B]}


def test_snapshot_omits_the_covers_key_for_a_character_without_covers():
    """没写 cover 的角色不带这两个键（"有才覆盖"，与 `card` 同一口径）。"""
    tracks = [{"character": "cirno", "album": "demo", "title": "一", "extra": "角色曲"}]
    snapshot = packs.pack_snapshot([], tracks, None, {"other": [HAND]})

    assert "covers" not in snapshot["characters"][0]
    assert "coversByRatio" not in snapshot["characters"][0]


def test_repo_snapshot_carries_covers_through_its_cache(repo, tmp_path, monkeypatch):
    """`repo_snapshot()`（带缓存的那条路）也要带上两份封面 —— 缓存键机制不受影响。"""
    stub_network(monkeypatch)
    assert run(tmp_path) == 0
    monkeypatch.setattr(packs, "_SNAPSHOT_CACHE", {})      # 别让别的用例（或上一次）的缓存顶上

    first = packs.repo_snapshot()
    second = packs.repo_snapshot()                         # 第二次走缓存：没改文件 ⇒ 内容一样

    assert first == second
    assert first["characters"][0]["covers"] == [PIC_A, PIC_B]
    assert first["characters"][0]["coversByRatio"]["16x9"] == \
        [PIC_A + "@1920w_1080h_1c.webp", PIC_B + "@1920w_1080h_1c.webp"]
    assert len(packs._SNAPSHOT_CACHE) == 1                 # 缓存只留最新一批（不长成泄漏）
