"""逐条封面（每条 `[[track]]` 里自己的 `cover`）与 `otomads.fetch_covers` 的测试。

契约（D153 + 裸原图修订）：**一条曲目一份封面**，写在**它自己那条** `[[track]]` 里；
值是**一条非空 https 直链** —— 接口给的**未加工原图**（裸 `data.pic`，只把 `http://` 换成 https），
**不带任何图床后缀 / 尺寸参数**（早先的 `@703w_1000h_1c.webp` 与"原图 + 16:9 / 4:3 两张源分辨率裁切"
的表都作废了；裁切与缩放由**前端**按用户选的卡面做）。严格读法下**一个角色要么每条都写、
要么一条都不写**（半有半无会让 `covers[i]` 与 `music[i]` 静默错位）；
`pack_snapshot()` 交出去的是"与 `music` 同序的 `covers`"，**没有** `coversByRatio` 了。

这里**完全不联网**：`request_pic`（接口）与 `backoff`（退避睡觉）一律 monkeypatch 掉，
连"真跑一次 CLI"的用例也走假网络 —— 用例不许依赖 bilibili 可达、也不许因为风控而红。
本模块**一张图都不下载**（尺寸是前端的事），所以没有"假图床"那一层替身了。
"""
from __future__ import annotations

import json
import pathlib
import tomllib

import pytest

from otomads import fetch_covers, packformat as packs

#: 接口给的**原图**直链（`http://` 那条正好练"换成 https"）—— 工具写下去的**就是它**，一个字不加
PIC_A = "https://i0.hdslb.com/bfs/archive/BV1kw411q7S8.jpg"
PIC_B = "https://i0.hdslb.com/bfs/archive/BV1GD4y1m7JY.jpg"
#: 人工覆写用的直链（与"假接口"给出的一定不同 ⇒ 一眼看得出工具动没动它）
HAND = "https://hand.example/mine.jpg"
#: 已经作废的**三帧表**（旧形状）：`--force` 要能把它整条换成裸原图
LEGACY_TABLE = ('cover = { original = "%s", "16x9" = "%s@1920w_1080h_1c.webp", '
                '"4x3" = "%s@1600w_1200h_1c.webp" }' % (HAND, HAND, HAND))

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

#: D153 之前的形状：顶层 `cover = [...]` 数组（靠**位置**与下面的 `[[track]]` 对应）。
#: 值故意留着早就作废的旧后缀：迁移**只搬位置、不改内容**（形状换代是 `--force` 的事）。
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


def stub_network(monkeypatch, *, fail: frozenset[str] = frozenset()) -> list[str]:
    """顶掉**接口**那一层 HTTP（BV → 假 `pic`），返回"接口收到的 BV"记录。

    本模块只问接口、**不下载任何图片**（尺寸是前端的事）⇒ 只有这一个替身。
    假接口故意回 `http://`：写下去的值必须是 https 版的同一条 URL。
    """
    calls: list[str] = []

    def fake_request(bv: str, timeout: float = fetch_covers.DEFAULT_TIMEOUT) -> str:
        calls.append(bv)
        if bv in fail:
            raise RuntimeError("HTTP Error 403: Forbidden（风控）")
        return f"http://i0.hdslb.com/bfs/archive/{bv}.jpg"

    monkeypatch.setattr(fetch_covers, "request_pic", fake_request)
    return calls


def run(tmp_path: pathlib.Path, *extra: str) -> int:
    """跑一次 CLI（缓存固定落在 tmp 里，别碰真仓库根那份）。"""
    return fetch_covers.main(["--pack", "demo", "--cache", str(tmp_path / "covers.jsonl"), *extra])


def covers_of(path: pathlib.Path) -> list:
    """角色文件里**逐条**的 `cover`（没有这条键的曲目给 `None`）—— 检查形状用。"""
    return [entry.get("cover") for entry in tomllib.loads(path.read_text(encoding="utf-8"))["track"]]


def covers_of_text(text: str) -> list:
    """同一件事，但输入是文本（纯函数的用例不落盘）。"""
    return [entry.get("cover") for entry in tomllib.loads(text)["track"]]


def cover_line(url: str) -> str:
    """工具该写出来的那一行（值 + 渲染交给纯函数，用例只钉位置与周围字节）。"""
    return f"cover = {packs.toml_str(url)}"


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
    """写下去的就是**接口给的那条原图 URL**：只把 `http://` 换成 https，**一个字符都不加**。

    早先的 `@703w_1000h_1c.webp`（先裁后缩）与"原图 + 两帧源分辨率裁切"都作废了：
    卡面的裁切与缩放是**前端**按用户选的那张卡面做的事，数据侧只交未加工的原图。
    """
    assert fetch_covers.original_url(PIC_A.replace("https://", "http://", 1)) == PIC_A
    assert fetch_covers.original_url(PIC_A) == PIC_A
    assert "@" not in fetch_covers.original_url(PIC_A)
    assert fetch_covers.original_url("") == ""


def test_original_url_keeps_a_query_string_the_api_gave():
    """**回归守卫**：接口给的 URL 上有查询串（图床参数之类）也**原样留着** —— 工具不是它的作者，
    只管把 `http://` 换成 https；多删一个字符就可能让前端拉不到图。"""
    pic = "http://i0.hdslb.com/bfs/archive/one.jpg?x-oss-process=image/resize,w_703"

    assert fetch_covers.original_url(pic) == \
        "https://i0.hdslb.com/bfs/archive/one.jpg?x-oss-process=image/resize,w_703"


# ------------------------------------------------------------------ 纯函数：文本级写回

def test_track_spans_ignores_a_commented_out_header():
    """`# [[track]]` 只是注释：认错了就会把封面插到注释后面去（数据文件里到处是这种注释）。"""
    text = '# 说明：[[track]] 下面才是曲目\nkey = "cirno"\n\n[[track]]\nalbum = "demo"\n'
    assert len(fetch_covers.track_spans(text)) == 1


def test_insert_track_cover_writes_one_link_under_source():
    """`source` 的下一行、**一行**一个直链；文件里其它字节一个不动。"""
    block = '[[track]]\nalbum = "demo"\ntitle = "一"\nsource = "https://example.com/a"\n'
    line = cover_line(PIC_A) + "\n"

    after = fetch_covers.insert_track_cover(block, PIC_A)

    assert after == block.replace('source = "https://example.com/a"\n',
                                  'source = "https://example.com/a"\n' + line, 1)
    assert after.count("\n") == block.count("\n") + 1                 # 只多了一行
    assert tomllib.loads(f'key = "x"\n\n{after}')["track"][0]["cover"] == PIC_A


def test_insert_track_cover_keeps_the_next_key_on_its_own_line():
    """**回归守卫**：`source` 下一行就是 `start_time` 时，插进去的 cover 必须自成一行。

    少写那个换行符就会拼成 `cover = "…"start_time = "…"` —— 整个文件直接读不动
    （真数据里 6 首带裁剪区间的曲目全中招，离线演练时才抓到）。
    """
    block = ('[[track]]\nalbum = "demo"\ntitle = "一"\n'
             'source = "https://example.com/a"\nstart_time = "00:00:10.000"\n')

    after = fetch_covers.insert_track_cover(block, PIC_A)

    assert f'{cover_line(PIC_A)}\nstart_time' in after
    assert tomllib.loads(f'key = "x"\n\n{after}')["track"][0]["start_time"] == "00:00:10.000"


def test_insert_track_cover_appends_at_the_end_of_a_block_without_source():
    """没有 `source` 就接在块的末尾：尾部空行留着（否则封面会跟它那条曲目分家）。"""
    block = '[[track]]\nalbum = "demo"\ntitle = "一"\n\n'

    after = fetch_covers.insert_track_cover(block, PIC_A)

    assert after == f'[[track]]\nalbum = "demo"\ntitle = "一"\n{cover_line(PIC_A)}\n\n'


def test_insert_track_cover_follows_the_indentation_of_the_block():
    """缩进跟着文件里的实际写法走（两个空格就两个空格），不硬写一种风格。"""
    block = '  [[track]]\n  album = "demo"\n  source = "https://example.com/a"\n'

    after = fetch_covers.insert_track_cover(block, PIC_A)

    assert f'  source = "https://example.com/a"\n  {cover_line(PIC_A)}\n' in after


def test_set_track_cover_targets_one_track_and_is_idempotent_on_replace():
    """`set_track_cover` 只动第 i 条；`replace=True` 跑两次字节完全相同（幂等）。"""
    once = fetch_covers.set_track_cover(CHARACTER_FILE, 1, PIC_B, replace=True)

    assert covers_of_text(once) == [None, PIC_B]
    assert once == CHARACTER_FILE.replace('source = "https://www.bilibili.com/video/BV1GD4y1m7JY/?p=2"\n',
                                          'source = "https://www.bilibili.com/video/BV1GD4y1m7JY/?p=2"\n'
                                          + cover_line(PIC_B) + "\n", 1)
    assert fetch_covers.set_track_cover(once, 1, PIC_B, replace=True) == once


@pytest.mark.parametrize("value", [
    '[\n  "https://hand.example/x.jpg",\n  "https://hand.example/y.jpg",\n]',   # 旧的多行数组
    '{\n  original = "https://hand.example/x.jpg",\n}',                        # 旧的多行三帧表
    '{\n  original = "https://hand.example/x.jpg",\n  "16x9" = "https://hand.example/y.jpg" }',
])
def test_replace_track_cover_swallows_any_compound_value(value):
    """整条换掉 —— 留半个 `]` / `}` 在文件里就是读不动的 TOML（两种复合值都踩过）。

    这两个形状都是**旧数据**（真仓库里那 74 条三帧表 + 106 条带旧后缀的直链就是它们）：
    `--force` 必须能把它们整条吃干净、换成一个裸原图。
    """
    block = ('[[track]]\nalbum = "demo"\ntitle = "一"\nsource = "https://example.com/a"\n'
             f'cover = {value}\nstart_time = "00:00:10.000"\n')

    after = fetch_covers.replace_track_cover(block, PIC_A)

    assert "hand.example" not in after and after.count("cover =") == 1
    assert tomllib.loads(f'key = "x"\n\n{after}')["track"][0] == {
        "album": "demo", "title": "一", "source": "https://example.com/a",
        "cover": PIC_A, "start_time": "00:00:10.000"}


def test_insert_track_cover_refuses_to_write_a_second_cover_line():
    """兜底：那一条已经有 cover 了就不许再插一行（插了 TOML 整份读不动）。"""
    block = f'[[track]]\nalbum = "demo"\ncover = "{HAND}"\n'
    with pytest.raises(SystemExit, match="已经有 cover"):
        fetch_covers.insert_track_cover(block, PIC_A)


# ------------------------------------------------------------------ 纯函数：旧形状迁移

def test_migrate_legacy_cover_moves_each_url_into_its_track():
    """第 i 条搬进第 i 条 `[[track]]`（插在 `source` 下一行），顶层那一段连注释一起消失。

    搬过去的值**原样**（还是 `...@703w_1000h_1c.webp` 那种旧后缀）：迁移只搬位置、不改内容 ——
    换成裸原图是 `--force` 的事（默认模式连请求都不发）。
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
    """每条曲目自己那份 cover → `covers[key]` 的顺序 = 文件里的曲目顺序（与 `tracks` 同序）。"""
    two = CHARACTER_FILE.replace(
        'source = "https://www.bilibili.com/video/BV1kw411q7S8/"\n',
        'source = "https://www.bilibili.com/video/BV1kw411q7S8/"\n' + cover_line(PIC_A) + "\n", 1).replace(
        'source = "https://www.bilibili.com/video/BV1GD4y1m7JY/?p=2"\n',
        'source = "https://www.bilibili.com/video/BV1GD4y1m7JY/?p=2"\n' + cover_line(PIC_B) + "\n", 1)
    write_pack(tmp_path, "cirno.toml", two)
    write_pack(tmp_path, "marisa.toml", 'key = "marisa"\n\n[[track]]\nalbum = "demo"\ntitle = "三"\n')
    monkeypatch.setattr(packs.repo, "DATA", tmp_path)

    _packs, _albums, tracks, _cards, covers = packs.load_packs()

    assert covers == {"cirno": [PIC_A, PIC_B]}
    assert [track["title"] for track in tracks if track["character"] == "cirno"] == ["一", "二"]
    assert "marisa" not in covers            # 一条都没写的角色不进表（缺省 = 没有封面）


def test_load_packs_accepts_a_character_with_covers(tmp_path, monkeypatch):
    """手工写的直链照样合法：**一条曲目一条直链**，工具不解释它的内容（只查 https 与非空）。"""
    write_pack(tmp_path, "cirno.toml", HAND_ONE.replace(
        'title = "二"\n', f'title = "二"\ncover = "{PIC_B}"\n', 1))
    monkeypatch.setattr(packs.repo, "DATA", tmp_path)

    _packs, _albums, _tracks, _cards, covers = packs.load_packs()

    assert covers == {"cirno": [HAND, PIC_B]}


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
])
def test_load_packs_rejects_a_cover_that_is_not_a_non_empty_string(tmp_path, monkeypatch, line):
    write_pack(tmp_path, "cirno.toml",
               f'key = "cirno"\n\n[[track]]\nalbum = "demo"\ntitle = "一"\n{line}\n')
    monkeypatch.setattr(packs.repo, "DATA", tmp_path)

    with pytest.raises(SystemExit):
        packs.load_packs()


def test_load_packs_rejects_the_obsolete_frame_table(tmp_path, monkeypatch):
    """**表是硬错误**：`cover = { original = …, "16x9" = …, "4x3" = … }` 已经作废。

    报错要**能照着做**：说清现在只写一条裸原图直链、裁切缩放是前端的事，并指路 `--force`。
    """
    write_pack(tmp_path, "cirno.toml",
               'key = "cirno"\n\n[[track]]\nalbum = "demo"\ntitle = "第一首"\n'
               f'{LEGACY_TABLE}\n')
    monkeypatch.setattr(packs.repo, "DATA", tmp_path)

    with pytest.raises(SystemExit, match="只能是一条直链"):
        packs.load_packs()
    with pytest.raises(SystemExit, match="第一首"):
        packs.load_packs()
    with pytest.raises(SystemExit, match="前端"):
        packs.load_packs()
    with pytest.raises(SystemExit, match="fetch_covers --force"):
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


def test_load_packs_rejects_a_half_covered_character_and_names_the_track(tmp_path, monkeypatch):
    """**全有或全无**：有的有、有的没有 ⇒ 报错点名缺的那一首，并给一条能照做的提示。

    半有半无在运行时的 `covers` 数组里就是**空洞**：应用按同一个下标取图 ⇒ 后面每张都串位，
    而且没有任何东西会报错。这条规矩与写法无关 —— 它管的是 `covers[i]` 能不能对上 `music[i]`。
    """
    write_pack(tmp_path, "cirno.toml", HAND_ONE)      # 第一条有（人工的）、第二条没有
    monkeypatch.setattr(packs.repo, "DATA", tmp_path)

    with pytest.raises(SystemExit, match="全有或全无"):
        packs.load_packs()
    with pytest.raises(SystemExit, match="「二」"):
        packs.load_packs()
    with pytest.raises(SystemExit, match="fetch_covers"):
        packs.load_packs()


def test_tolerant_read_lets_the_repair_tool_in(tmp_path, monkeypatch):
    """宽容读法（只有 `fetch_covers` 用）：半有半无、旧的三帧表、旧形状都读得进来，只是不记 `covers`。

    没有它，工具就卡在"报错让你重跑 fetch_covers、它自己又因为同一条报错起不来"的死循环里
    —— 而真仓库现在正好有 74 条三帧表要它去换掉。
    """
    write_pack(tmp_path, "cirno.toml", HAND_ONE)
    write_pack(tmp_path, "marisa.toml", LEGACY_FILE.replace("cirno", "marisa"))
    write_pack(tmp_path, "sakuya.toml", CHARACTER_FILE.replace("cirno", "sakuya").replace(
        'title = "二"\n', f'title = "二"\n{LEGACY_TABLE}\n', 1))
    monkeypatch.setattr(packs.repo, "DATA", tmp_path)

    _packs, _albums, tracks, _cards, covers = packs.load_packs(validate_covers=False)

    assert len(tracks) == 6 and covers == {}
    # 其它校验一条不少：未知键照样报
    write_pack(tmp_path, "youmu.toml", 'key = "youmu"\n\n[[track]]\nalbum = "demo"\ntitle = "x"\n'
                                       'cover = "https://a/b.jpg"\nstarttime = "00:00:01.000"\n')
    with pytest.raises(SystemExit, match="不认识的键"):
        packs.load_packs(validate_covers=False)


# ------------------------------------------------------------------ CLI 端到端（假网络）

def test_a_new_file_gets_one_bare_original_link_per_track(repo, tmp_path, monkeypatch, capsys):
    """逐条补缺：插在 `source` 下一行，值是**接口给的那条原图直链**（只换 http → https）。"""
    calls = stub_network(monkeypatch)

    assert run(tmp_path) == 0

    after = repo.read_text(encoding="utf-8")
    assert covers_of(repo) == [PIC_A, PIC_B]
    # **逐字对比**：除插进去的那两行，文件其余部分一个字节都没变
    assert after == CHARACTER_FILE.replace(
        'source = "https://www.bilibili.com/video/BV1kw411q7S8/"\n',
        'source = "https://www.bilibili.com/video/BV1kw411q7S8/"\n' + cover_line(PIC_A) + "\n",
        1).replace(
        'source = "https://www.bilibili.com/video/BV1GD4y1m7JY/?p=2"\n',
        'source = "https://www.bilibili.com/video/BV1GD4y1m7JY/?p=2"\n' + cover_line(PIC_B) + "\n",
        1)
    assert "@" not in after                            # 一个图床后缀都不许写进去
    assert sorted(calls) == ["BV1GD4y1m7JY", "BV1kw411q7S8"]          # 每条曲目恰好请求一次
    assert packs.load_packs()[4] == {"cirno": [PIC_A, PIC_B]}         # 严格读法也过得去
    printed = capsys.readouterr().out
    assert "新增 2 条" in printed and "跳过（已有）0" in printed and "失败 0" in printed


def test_the_written_link_is_the_api_url_verbatim(tmp_path, monkeypatch):
    """**回归守卫**（本修订的核心规矩）：工具写下去的就是**接口给的那条 URL 本身** ——
    只把 `http://` 换成 https，**不追加后缀、不加查询参数、不按原图尺寸改写任何东西**。

    用户要的是"默认获取原版无修改封面，不加任何分辨率限制参数"：缩放裁切全由前端做。
    """
    api_pic = "http://i0.hdslb.com/bfs/archive/one.jpg?x-oss-process=image/resize,w_703"
    path = write_pack(tmp_path, "cirno.toml",
                      'key = "cirno"\n\n[[track]]\nalbum = "demo"\ntitle = "一"\n'
                      'source = "https://www.bilibili.com/video/BV1kw411q7S8/"\n')
    monkeypatch.setattr(fetch_covers.paths, "DATA", tmp_path)
    monkeypatch.setattr(fetch_covers, "backoff", lambda _seconds: None)
    monkeypatch.setattr(fetch_covers, "request_pic", lambda _bv, _timeout=None: api_pic)

    assert run(tmp_path) == 0

    written = covers_of(path)[0]
    assert written == "https://i0.hdslb.com/bfs/archive/one.jpg?x-oss-process=image/resize,w_703"
    assert written == fetch_covers.original_url(api_pic)      # 只做 http → https 这一处改动
    assert "@" not in written and "w_" not in written.split("?")[0]


def test_no_image_download_machinery_remains():
    """**回归守卫**：量尺寸那套（下原图 + 解 JPEG/PNG/WebP/GIF 头 + 裁切算术）已经整块删掉。

    留着它就会有人再拿它去拼 `@<W>w_<H>h_1c.webp` —— 而那正是这次要根除的东西。
    """
    for gone in ("request_image", "measure_size", "image_size", "crop_size", "cover_frames",
                 "shaped_cover", "toml_cover", "COVER_FRAMES", "FRAME_RATIOS", "LEGACY_SUFFIX"):
        assert not hasattr(fetch_covers, gone), f"{gone} 应该已经删掉"
    assert not hasattr(packs, "COVER_FRAMES") and not hasattr(packs, "primary_cover")
    assert not hasattr(packs, "covers_by_ratio")


def test_rerun_is_byte_identical_and_sends_no_request(repo, tmp_path, monkeypatch, capsys):
    """幂等：第二次一个字节都不变，而且已有 cover 的一条**连请求都不发**。"""
    stub_network(monkeypatch)
    assert run(tmp_path) == 0
    once = repo.read_text(encoding="utf-8")
    calls = stub_network(monkeypatch)                 # 换一份计数

    assert run(tmp_path) == 0

    assert repo.read_text(encoding="utf-8") == once
    assert calls == []
    assert "跳过（已有）2" in capsys.readouterr().out


def test_existing_cover_is_never_touched_and_never_requested(repo, tmp_path, monkeypatch, capsys):
    """默认**只补没有的**：手工改过的那一条原样留着，工具**不为它发请求**。"""
    repo.write_text(HAND_ONE, encoding="utf-8")
    calls = stub_network(monkeypatch)

    assert run(tmp_path) == 0

    after = repo.read_text(encoding="utf-8")
    assert covers_of(repo) == [HAND, PIC_B]
    assert after == HAND_ONE.replace(
        'source = "https://www.bilibili.com/video/BV1GD4y1m7JY/?p=2"\n',
        f'source = "https://www.bilibili.com/video/BV1GD4y1m7JY/?p=2"\ncover = "{PIC_B}"\n', 1)
    assert calls == ["BV1GD4y1m7JY"]                   # 只补了缺的那一条
    printed = capsys.readouterr().out
    assert "新增 1 条" in printed and "跳过（已有）1" in printed


def test_force_refreshes_every_existing_cover_including_hand_edits(repo, tmp_path, monkeypatch):
    """`--force` = 整包刷新：**已有的每一条**都按当前 source 的 BV 重抓（会盖掉手改）。"""
    repo.write_text(HAND_ONE, encoding="utf-8")
    calls = stub_network(monkeypatch)

    assert run(tmp_path, "--force") == 0

    assert covers_of(repo) == [PIC_A, PIC_B]
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

    assert covers_of(repo) == [PIC_A, PIC_B]
    assert packs.load_packs()[4] == {"cirno": [PIC_A, PIC_B]}


def test_force_heals_the_obsolete_frame_table(repo, tmp_path, monkeypatch):
    """**数据刷新的主线**：旧的三帧表整条换成裸原图（真仓库里 74 条就是这个形状）。

    值里带着 `@1920w_1080h_1c.webp`，工具要把它连表一起吃掉、只留一条裸原图 ——
    留半个 `}` 在文件里就是读不动的 TOML。（TOML 的内联表本来就只能写一行，
    所以这里就按真数据的写法摆一行。）
    """
    repo.write_text(CHARACTER_FILE.replace(
        'source = "https://www.bilibili.com/video/BV1kw411q7S8/"\n',
        'source = "https://www.bilibili.com/video/BV1kw411q7S8/"\n' + LEGACY_TABLE + "\n", 1),
        encoding="utf-8")
    with pytest.raises(SystemExit, match="只能是一条直链"):
        packs.load_packs()                             # 消费方口径：硬失败
    stub_network(monkeypatch)

    assert run(tmp_path, "--force") == 0

    assert covers_of(repo) == [PIC_A, PIC_B]
    assert "@" not in repo.read_text(encoding="utf-8")
    assert packs.load_packs()[4] == {"cirno": [PIC_A, PIC_B]}


def test_force_never_deletes_a_cover_it_cannot_refresh(repo, tmp_path, monkeypatch, capsys):
    """`--force` 也刷不动"没有 BV"的那一条：**已有的 cover 原样留着**（报出来，但绝不删数据）。

    `--force` 的语义是"按当前 source 重抓"，不是"清空重来"：`source` 没了就没得重抓，
    这时把人工挑的那张图删掉是最坏的结果。
    """
    both = HAND_ONE.replace('title = "二"\n', f'title = "二"\ncover = "{PIC_B}"\n', 1).replace(
        'source = "https://www.bilibili.com/video/BV1GD4y1m7JY/?p=2"',
        'source = "https://www.bilibili.com/video/av12345"')
    repo.write_text(both, encoding="utf-8")
    calls = stub_network(monkeypatch)

    assert run(tmp_path, "--force") == 1

    assert covers_of(repo) == [PIC_A, PIC_B]           # 第一条刷新了，第二条原样留着
    assert "hand.example" not in repo.read_text(encoding="utf-8")
    assert calls == ["BV1kw411q7S8"]
    assert packs.load_packs()[4] == {"cirno": [PIC_A, PIC_B]}    # 整只角色仍然合法
    printed = capsys.readouterr().out
    assert "原样留着" in printed and "失败 1" in printed


def test_legacy_file_is_migrated_without_any_request(repo, tmp_path, monkeypatch, capsys):
    """旧形状：顶层数组按顺序搬进各条 `[[track]]`，**一条请求都不发**（值算"已有"）。"""
    repo.write_text(LEGACY_FILE, encoding="utf-8")
    calls = stub_network(monkeypatch)

    assert run(tmp_path) == 0

    assert repo.read_text(encoding="utf-8") == LEGACY_MIGRATED
    assert calls == []
    printed = capsys.readouterr().out
    assert "迁移 1 个角色" in printed and "跳过（已有）2" in printed


def test_a_track_added_after_migration_is_filled_alone(repo, tmp_path, monkeypatch):
    """迁移之后再手工加一首（没写 cover）⇒ 重跑只补那一条；第三次跑字节完全相同。

    这就是 README 里那套流程的形状：**加曲目 → 跑一次 fetch_covers**（旧的几条一个都不动）。
    """
    repo.write_text(LEGACY_FILE, encoding="utf-8")
    calls = stub_network(monkeypatch)
    assert run(tmp_path) == 0
    assert calls == []                                 # 迁移不联网
    assert repo.read_text(encoding="utf-8") == LEGACY_MIGRATED

    repo.write_text(LEGACY_MIGRATED + '\n[[track]]\nalbum = "demo"\ntitle = "三"\n'
                    'source = "https://www.bilibili.com/video/BV1xx411c7mD"\n', encoding="utf-8")
    calls.clear()

    assert run(tmp_path) == 0

    assert covers_of(repo) == ["https://hand.example/one.jpg@703w_1000h_1c.webp",
                               "https://hand.example/two.png@703w_1000h_1c.webp",
                               "https://i0.hdslb.com/bfs/archive/BV1xx411c7mD.jpg"]
    assert calls == ["BV1xx411c7mD"]                   # 只补新加的那一条
    once = repo.read_text(encoding="utf-8")

    assert run(tmp_path) == 0

    assert repo.read_text(encoding="utf-8") == once    # 幂等：一个字节都不变


def test_force_refreshes_the_values_it_just_migrated(repo, tmp_path, monkeypatch):
    """`--force` 也要先迁移：搬进来的值算"已有"，然后按 BV 全部刷新成**裸原图**。"""
    repo.write_text(LEGACY_FILE, encoding="utf-8")
    calls = stub_network(monkeypatch)

    assert run(tmp_path, "--force") == 0

    assert covers_of(repo) == [PIC_A, PIC_B]
    assert "@" not in repo.read_text(encoding="utf-8")
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
    calls = stub_network(monkeypatch)

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
    calls = stub_network(monkeypatch)

    assert run(tmp_path) == 1

    assert repo.read_text(encoding="utf-8") == broken
    assert other.read_text(encoding="utf-8").endswith('source = "https://www.bilibili.com/video/BV1xx411c7mD"\n')
    assert calls == []                                 # 一条请求都没发
    assert not (tmp_path / "covers.jsonl").exists()
    printed = capsys.readouterr().out
    assert "搬不动" in printed and "一个字节都没写" in printed


def test_a_track_without_a_bv_is_skipped_alone(repo, tmp_path, monkeypatch, capsys):
    """缺 BV ⇒ **只**跳过那一条（别的照常写），退出码 1，原因里点名是哪首。"""
    repo.write_text(CHARACTER_FILE.replace(
        'source = "https://www.bilibili.com/video/BV1GD4y1m7JY/?p=2"',
        'source = "https://www.bilibili.com/video/av12345"'), encoding="utf-8")
    calls = stub_network(monkeypatch)

    assert run(tmp_path) == 1

    assert covers_of(repo) == [PIC_A, None]            # 好的那条照常写，坏的那条没动
    assert calls == ["BV1kw411q7S8"]                   # 缺 BV 的那条连请求都不发
    printed = capsys.readouterr().out
    assert "没有 BV 号" in printed and "二" in printed
    assert "新增 1 条" in printed and "失败 1" in printed


def test_a_failed_fetch_does_not_block_the_other_tracks(repo, tmp_path, monkeypatch, capsys):
    """网络失败 ⇒ 那一条不写（下次重跑即可），其它条照常；退出码 1。"""
    calls = stub_network(monkeypatch, fail=frozenset({"BV1GD4y1m7JY"}))

    assert run(tmp_path) == 1

    assert covers_of(repo) == [PIC_A, None]
    assert calls.count("BV1GD4y1m7JY") == fetch_covers.ATTEMPTS        # 退避重试确实试满了
    assert calls.count("BV1kw411q7S8") == 1                            # 成功的那条只请求一次
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

def test_cache_is_jsonl_with_the_bv_and_the_original_pic(repo, tmp_path, monkeypatch):
    """缓存一行 = `{bv, pic}`：pic 就是接口给的**原图直链**（尺寸不再进缓存 —— 没人用了）。"""
    stub_network(monkeypatch)

    assert run(tmp_path) == 0

    records = [json.loads(line) for line in (tmp_path / "covers.jsonl").read_text(
        encoding="utf-8").splitlines()]
    assert {(record["character"], record["title"]) for record in records} == {("cirno", "一"), ("cirno", "二")}
    assert {record["pic"] for record in records} == {PIC_A, PIC_B}
    assert all(set(record) == {"character", "title", "bv", "pic"} for record in records)


def test_a_cache_hit_asks_no_api(repo, tmp_path, monkeypatch):
    """缓存命中：**连接口都不问**（`--force` 也一样）—— 值直接从缓存里的 `pic` 拿来写。"""
    stub_network(monkeypatch)
    assert run(tmp_path) == 0

    calls = stub_network(monkeypatch)                  # 换一份计数
    assert run(tmp_path, "--force") == 0

    assert calls == []
    assert covers_of(repo) == [PIC_A, PIC_B]


def test_cache_misses_when_the_source_bv_changed(repo, tmp_path, monkeypatch):
    """换了 `source` 就是换了视频 ⇒ 缓存必须算未命中（否则 `--force` 也刷不掉一张错封面）。"""
    calls = stub_network(monkeypatch)
    assert run(tmp_path) == 0

    repo.write_text(CHARACTER_FILE.replace("BV1kw411q7S8", "BV1zz411q7S9"), encoding="utf-8")
    calls.clear()
    assert run(tmp_path, "--force") == 0

    assert calls == ["BV1zz411q7S9"]                     # 只重抓换过的那一条
    assert covers_of(repo)[0] == "https://i0.hdslb.com/bfs/archive/BV1zz411q7S9.jpg"


def test_a_legacy_cache_line_is_read_as_a_bare_original(repo, tmp_path, monkeypatch):
    """**旧缓存行**（只有 `url` = 原图 + 老后缀）：剥掉 `@` 之后那一段当原图用 ⇒ 连接口都不用问。

    这就是这次数据刷新的路径：真仓库那份缓存里 191 行全是旧行 + 新行混着，`--force` 靠它
    把 106 条 `@703w_1000h_1c.webp` 换成裸原图，**一张图都不用重下**（本来也不下图了）。
    """
    (tmp_path / "covers.jsonl").write_text("\n".join(json.dumps(record, ensure_ascii=False) for record in [
        {"character": "cirno", "title": "一", "bv": "BV1kw411q7S8",
         "url": PIC_A + "@703w_1000h_1c.webp"},
        {"character": "cirno", "title": "二", "bv": "BV1GD4y1m7JY", "url": PIC_B},
    ]) + "\n", encoding="utf-8")
    calls = stub_network(monkeypatch)

    assert run(tmp_path, "--force") == 0

    assert covers_of(repo) == [PIC_A, PIC_B]
    assert calls == []                                 # 接口一次都没问
    assert "@" not in repo.read_text(encoding="utf-8")


def test_read_cache_survives_a_half_written_line(tmp_path):
    """JSONL 是"可续跑"的文件：最后一行写了一半也不该让整个缓存作废。"""
    cache = tmp_path / "covers.jsonl"
    cache.write_text(json.dumps({"character": "cirno", "title": "一", "bv": "BVx", "pic": PIC_A},
                                ensure_ascii=False) + "\n" + '{"character": "cirno", "tit',
                     encoding="utf-8")

    assert fetch_covers.read_cache(cache) == {("cirno", "一"): {"bv": "BVx", "pic": PIC_A}}


def test_read_cache_keeps_the_last_line_for_the_same_track(tmp_path):
    """同一条曲目重抓过 ⇒ 后写的那行赢（JSONL 只追加，靠顺序定新旧）。"""
    cache = tmp_path / "covers.jsonl"
    cache.write_text("\n".join(json.dumps(record, ensure_ascii=False) for record in [
        {"character": "cirno", "title": "一", "bv": "BVx", "pic": PIC_A},
        {"character": "cirno", "title": "一", "bv": "BVx", "pic": PIC_B, "width": 1920, "height": 1200},
    ]) + "\n", encoding="utf-8")

    assert fetch_covers.read_cache(cache) == {("cirno", "一"): {"bv": "BVx", "pic": PIC_B}}


def test_cached_pic_is_reused_without_touching_the_network(repo, tmp_path, monkeypatch):
    """缓存里的原图直接拿来写（`--dry-run` 不写缓存，但读缓存照旧）。"""
    (tmp_path / "covers.jsonl").write_text("\n".join(json.dumps(record, ensure_ascii=False) for record in [
        {"character": "cirno", "title": "一", "bv": "BV1kw411q7S8", "pic": PIC_A},
        {"character": "cirno", "title": "二", "bv": "BV1GD4y1m7JY", "pic": PIC_B,
         "width": 800, "height": 600},
    ]) + "\n", encoding="utf-8")
    calls = stub_network(monkeypatch)

    assert run(tmp_path) == 0

    assert covers_of(repo) == [PIC_A, PIC_B]
    assert calls == []


# ------------------------------------------------------------------ 真源 TOML → 快照形状

def test_snapshot_from_the_real_toml_carries_one_link_per_track(repo, tmp_path, monkeypatch):
    """端到端：真源 TOML → `load_packs` → `pack_snapshot` 的封面数组。

    ``covers`` 每条曲目**一个**直链（裸原图）、与 `music` **同序**；形状能进 `json.dumps`
    （源要把它塞进 `manifest.json`）。**没有** `coversByRatio` —— 按帧拆数组这件事整个删掉了，
    裁切缩放是前端拿到原图之后的事。
    """
    stub_network(monkeypatch)
    assert run(tmp_path) == 0

    _packs, albums, tracks, cards, covers = packs.load_packs()
    snapshot = packs.pack_snapshot(albums, tracks, cards, covers)

    assert snapshot["characters"] == [{
        "key": "cirno",
        "music": [["demo", "一", "角色曲", "甲"], ["demo", "二", "道中曲", "乙"]],
        "covers": [PIC_A, PIC_B],
    }]
    json.dumps(snapshot)                                  # 真能进 manifest.json
    assert "coversByRatio" not in json.dumps(snapshot)
    assert all("@" not in url for url in snapshot["characters"][0]["covers"])


def test_snapshot_omits_the_covers_key_for_a_character_without_covers():
    """没写 cover 的角色不带这个键（"有才覆盖"，与 `card` 同一口径）。"""
    tracks = [{"character": "cirno", "album": "demo", "title": "一", "extra": "角色曲"}]
    snapshot = packs.pack_snapshot([], tracks, None, {"other": [HAND]})

    assert "covers" not in snapshot["characters"][0]
    assert "coversByRatio" not in snapshot["characters"][0]


def test_repo_snapshot_carries_covers_through_its_cache(repo, tmp_path, monkeypatch):
    """`repo_snapshot()`（带缓存的那条路）也要带上封面 —— 缓存键机制不受影响。"""
    stub_network(monkeypatch)
    assert run(tmp_path) == 0
    monkeypatch.setattr(packs, "_SNAPSHOT_CACHE", {})      # 别让别的用例（或上一次）的缓存顶上

    first = packs.repo_snapshot()
    second = packs.repo_snapshot()                         # 第二次走缓存：没改文件 ⇒ 内容一样

    assert first == second
    assert first["characters"][0]["covers"] == [PIC_A, PIC_B]
    assert "coversByRatio" not in first["characters"][0]
    assert len(packs._SNAPSHOT_CACHE) == 1                 # 缓存只留最新一批（不长成泄漏）
