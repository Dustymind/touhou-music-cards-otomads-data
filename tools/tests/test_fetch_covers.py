"""逐条封面直链（每条 `[[track]]` 里自己的 `cover`）与 `otomads.fetch_covers` 的测试。

契约（D153 修订）：**一条曲目一张 B 站封面直链**，写在**它自己那条** `[[track]]` 里；
严格读法下**一个角色要么每条都写、要么一条都不写**（半有半无 ⇒ 报错），
而 `pack_snapshot()` 交出去的仍是"与 `music` 同序的 `covers` 数组"（**线上形状没变**）。

这里**完全不联网**：`request_pic`（HTTP 那一层）与 `backoff`（退避睡觉）一律 monkeypatch 掉，
连"真跑一次 CLI"的用例也走假网络 —— 用例不许依赖 bilibili 可达、也不许因为风控而红。
"""
from __future__ import annotations

import json
import pathlib
import tomllib

import pytest

from otomads import fetch_covers, packformat as packs

#: 一张"接口返回的原图"（`http://` 那条正好练"换成 https"）与它该有的封面直链
COVER_PIC = "https://i0.hdslb.com/bfs/archive/88ad053c21de0ce0eab53e56561c2c6dadc79e36.jpg"
COVER_A = "https://i0.hdslb.com/bfs/archive/BV1kw411q7S8.jpg" + fetch_covers.COVER_SUFFIX
COVER_B = "https://i0.hdslb.com/bfs/archive/BV1GD4y1m7JY.jpg" + fetch_covers.COVER_SUFFIX
#: 人工覆写用的直链（与"假图床"给出的一定不同 ⇒ 一眼看得出工具动没动它）
HAND = "https://hand.example/mine.jpg@703w_1000h_1c.webp"

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
#: 第 i 条原样进了第 i 条 `[[track]]`，其它字节一个没动
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
    """顶掉 `request_pic`（HTTP 那一层）：BV → 确定性的假图，记录每个被请求的 BV。"""
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


# ------------------------------------------------------------------ 纯函数：提 BV / 拼直链

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


def test_cover_url_upgrades_http_and_appends_the_default_suffix():
    """B 站给的 `pic` 常是 `http://`：站点是 https ⇒ 必须换，否则是混合内容、图被浏览器拦掉。"""
    assert fetch_covers.cover_url(COVER_PIC.replace("https://", "http://", 1)) == COVER_PIC + \
        fetch_covers.COVER_SUFFIX
    assert fetch_covers.cover_url(COVER_PIC) == COVER_PIC + fetch_covers.COVER_SUFFIX
    # 后缀已经在了（人工塞的直链）就不重复拼：幂等
    assert fetch_covers.cover_url(HAND) == HAND
    assert fetch_covers.cover_url("") == ""


# ------------------------------------------------------------------ 纯函数：文本级写回

def test_track_spans_ignores_a_commented_out_header():
    """`# [[track]]` 只是注释：认错了就会把封面插到注释后面去（数据文件里到处是这种注释）。"""
    text = '# 说明：[[track]] 下面才是曲目\nkey = "cirno"\n\n[[track]]\nalbum = "demo"\n'
    assert len(fetch_covers.track_spans(text)) == 1


def test_insert_track_cover_goes_under_source_and_changes_nothing_else():
    block = '[[track]]\nalbum = "demo"\ntitle = "一"\nsource = "https://example.com/a"\n'

    after = fetch_covers.insert_track_cover(block, COVER_A)

    assert after == block.replace('source = "https://example.com/a"\n',
                                  f'source = "https://example.com/a"\ncover = "{COVER_A}"\n', 1)


def test_insert_track_cover_keeps_the_next_key_on_its_own_line():
    """**回归守卫**：`source` 下一行就是 `start_time` 时，插进去的 cover 必须自成一行。

    少写那个换行符就会拼成 `cover = "…"start_time = "…"` —— 整个文件直接读不动
    （真数据里 6 首带裁剪区间的曲目全中招，离线演练时才抓到）。
    """
    block = ('[[track]]\nalbum = "demo"\ntitle = "一"\n'
             'source = "https://example.com/a"\nstart_time = "00:00:10.000"\n')

    after = fetch_covers.insert_track_cover(block, COVER_A)

    assert f'cover = "{COVER_A}"\nstart_time' in after
    assert tomllib.loads(f'key = "x"\n\n{after}')["track"][0]["start_time"] == "00:00:10.000"


def test_insert_track_cover_appends_at_the_end_of_a_block_without_source():
    """没有 `source` 就接在块的末尾：尾部空行留着（否则封面会跟它那条曲目分家）。"""
    block = '[[track]]\nalbum = "demo"\ntitle = "一"\n\n'

    after = fetch_covers.insert_track_cover(block, COVER_A)

    assert after == f'[[track]]\nalbum = "demo"\ntitle = "一"\ncover = "{COVER_A}"\n\n'


def test_insert_track_cover_follows_the_indentation_of_the_block():
    """缩进跟着文件里的实际写法走（两个空格就两个空格），不硬写一种风格。"""
    block = '  [[track]]\n  album = "demo"\n  source = "https://example.com/a"\n'

    after = fetch_covers.insert_track_cover(block, COVER_A)

    assert f'  source = "https://example.com/a"\n  cover = "{COVER_A}"\n' in after


def test_set_track_cover_targets_one_track_and_is_idempotent_on_replace(tmp_path):
    """`set_track_cover` 只动第 i 条；`replace=True` 跑两次字节完全相同（幂等）。"""
    once = fetch_covers.set_track_cover(CHARACTER_FILE, 1, COVER_B, replace=True)

    assert covers_of_text(once) == [None, COVER_B]
    assert once == CHARACTER_FILE.replace('source = "https://www.bilibili.com/video/BV1GD4y1m7JY/?p=2"\n',
                                          f'source = "https://www.bilibili.com/video/BV1GD4y1m7JY/?p=2"\n'
                                          f'cover = "{COVER_B}"\n', 1)
    assert fetch_covers.set_track_cover(once, 1, COVER_B, replace=True) == once


def covers_of_text(text: str) -> list:
    return [entry.get("cover") for entry in tomllib.loads(text)["track"]]


def test_replace_track_cover_swallows_a_hand_written_multi_line_array():
    """手写的多行数组也要被整条换掉 —— 留半个 `]` 在文件里就是读不动的 TOML。"""
    block = ('[[track]]\nalbum = "demo"\ntitle = "一"\nsource = "https://example.com/a"\n'
             'cover = [\n  "https://hand.example/x.jpg",\n  "https://hand.example/y.jpg",\n]\n'
             'start_time = "00:00:10.000"\n')

    after = fetch_covers.replace_track_cover(block, COVER_A)

    assert "hand.example" not in after and after.count("cover =") == 1
    assert tomllib.loads(f'key = "x"\n\n{after}')["track"][0] == {
        "album": "demo", "title": "一", "source": "https://example.com/a",
        "cover": COVER_A, "start_time": "00:00:10.000"}


def test_insert_track_cover_refuses_to_write_a_second_cover_line():
    """兜底：那一条已经有 cover 了就不许再插一行（插了 TOML 整份读不动）。"""
    block = f'[[track]]\nalbum = "demo"\ncover = "{HAND}"\n'
    with pytest.raises(SystemExit, match="已经有 cover"):
        fetch_covers.insert_track_cover(block, COVER_A)


# ------------------------------------------------------------------ 纯函数：旧形状迁移

def test_migrate_legacy_cover_moves_each_url_into_its_track():
    """第 i 条搬进第 i 条 `[[track]]`（插在 `source` 下一行），顶层那一段连注释一起消失。"""
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
    write_pack(tmp_path, "cirno.toml", HAND_ONE.replace(
        'title = "二"\n', f'title = "二"\ncover = "{COVER_B}"\n', 1))
    write_pack(tmp_path, "marisa.toml", 'key = "marisa"\n\n[[track]]\nalbum = "demo"\ntitle = "三"\n')
    monkeypatch.setattr(packs.repo, "DATA", tmp_path)

    _packs, _albums, tracks, _cards, covers = packs.load_packs()

    assert covers == {"cirno": [HAND, COVER_B]}
    assert [track["title"] for track in tracks if track["character"] == "cirno"] == ["一", "二"]
    assert "marisa" not in covers            # 一条都没写的角色不进表（缺省 = 没有封面）


def test_load_packs_accepts_a_character_with_no_covers_at_all(tmp_path, monkeypatch):
    write_pack(tmp_path, "cirno.toml", CHARACTER_FILE)
    monkeypatch.setattr(packs.repo, "DATA", tmp_path)

    _packs, _albums, tracks, _cards, covers = packs.load_packs()

    assert len(tracks) == 2 and covers == {}


@pytest.mark.parametrize("line", [
    'cover = []',                                    # 数组 = 还以为是"一首一封面"那种形状
    'cover = ""',                                    # 空串 = 写了一半
    'cover = "   "',                                 # 全是空白同理
])
def test_load_packs_rejects_a_cover_that_is_not_a_non_empty_string(tmp_path, monkeypatch, line):
    write_pack(tmp_path, "cirno.toml",
               f'key = "cirno"\n\n[[track]]\nalbum = "demo"\ntitle = "一"\n{line}\n')
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


def test_tolerant_read_lets_the_repair_tool_in(tmp_path, monkeypatch):
    """宽容读法（只有 `fetch_covers` 用）：半有半无、旧形状都读得进来，只是不记 `covers`。

    没有它，工具就卡在"报错让你重跑 fetch_covers、它自己又因为同一条报错起不来"的死循环里。
    """
    write_pack(tmp_path, "cirno.toml", HAND_ONE)
    write_pack(tmp_path, "marisa.toml", LEGACY_FILE.replace("cirno", "marisa"))
    monkeypatch.setattr(packs.repo, "DATA", tmp_path)

    _packs, _albums, tracks, _cards, covers = packs.load_packs(validate_covers=False)

    assert len(tracks) == 4 and covers == {}
    # 其它校验一条不少：未知键照样报
    write_pack(tmp_path, "sakuya.toml", 'key = "sakuya"\n\n[[track]]\nalbum = "demo"\ntitle = "x"\n'
                                        'cover = "https://a/b.jpg"\nstarttime = "00:00:01.000"\n')
    with pytest.raises(SystemExit, match="不认识的键"):
        packs.load_packs(validate_covers=False)


# ------------------------------------------------------------------ CLI 端到端（假网络）

def test_a_new_file_gets_one_cover_per_track_under_source(repo, tmp_path, monkeypatch, capsys):
    """逐条补缺：插在 `source` 下一行，值来自假图床（`http://` → https + 裁切后缀）。"""
    calls = stub_network(monkeypatch)

    assert run(tmp_path) == 0

    after = repo.read_text(encoding="utf-8")
    assert covers_of(repo) == [COVER_A, COVER_B]
    # **逐字对比**：除插进去的那两行，文件其余部分一个字节都没变
    assert after == CHARACTER_FILE.replace(
        'source = "https://www.bilibili.com/video/BV1kw411q7S8/"\n',
        f'source = "https://www.bilibili.com/video/BV1kw411q7S8/"\ncover = "{COVER_A}"\n', 1).replace(
        'source = "https://www.bilibili.com/video/BV1GD4y1m7JY/?p=2"\n',
        f'source = "https://www.bilibili.com/video/BV1GD4y1m7JY/?p=2"\ncover = "{COVER_B}"\n', 1)
    assert sorted(calls) == ["BV1GD4y1m7JY", "BV1kw411q7S8"]          # 每条曲目恰好请求一次
    printed = capsys.readouterr().out
    assert "新增 2 条" in printed and "跳过（已有）0" in printed and "失败 0" in printed


def test_rerun_is_byte_identical_and_sends_no_request(repo, tmp_path, monkeypatch, capsys):
    """幂等：第二次一个字节都不变，而且已有 cover 的一条**连请求都不发**。"""
    stub_network(monkeypatch)
    assert run(tmp_path) == 0
    once = repo.read_text(encoding="utf-8")
    calls = stub_network(monkeypatch)                 # 换一份计数（顺带清掉"假图床"的记录）

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
    assert covers_of(repo) == [HAND, COVER_B]          # 人工那份一个字没动
    assert after == HAND_ONE.replace(
        'source = "https://www.bilibili.com/video/BV1GD4y1m7JY/?p=2"\n',
        f'source = "https://www.bilibili.com/video/BV1GD4y1m7JY/?p=2"\ncover = "{COVER_B}"\n', 1)
    assert calls == ["BV1GD4y1m7JY"]                   # 只补了缺的那一条
    printed = capsys.readouterr().out
    assert "新增 1 条" in printed and "跳过（已有）1" in printed


def test_force_refreshes_every_existing_cover_including_hand_edits(repo, tmp_path, monkeypatch):
    """`--force` = 整包刷新：**已有的每一条**都按当前 source 的 BV 重抓（会盖掉手改）。"""
    repo.write_text(HAND_ONE, encoding="utf-8")
    calls = stub_network(monkeypatch)

    assert run(tmp_path, "--force") == 0

    assert covers_of(repo) == [COVER_A, COVER_B]
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

    assert covers_of(repo) == [COVER_A, COVER_B]
    assert packs.load_packs()[4] == {"cirno": [COVER_A, COVER_B]}


def test_force_never_deletes_a_cover_it_cannot_refresh(repo, tmp_path, monkeypatch, capsys):
    """`--force` 也刷不动"没有 BV"的那一条：**已有的 cover 原样留着**（报出来，但绝不删数据）。

    `--force` 的语义是"按当前 source 重抓"，不是"清空重来"：`source` 没了就没得重抓，
    这时把人工挑的那张图删掉是最坏的结果。
    """
    both = HAND_ONE.replace('title = "二"\n', f'title = "二"\ncover = "{COVER_B}"\n', 1).replace(
        'source = "https://www.bilibili.com/video/BV1GD4y1m7JY/?p=2"',
        'source = "https://www.bilibili.com/video/av12345"')
    repo.write_text(both, encoding="utf-8")
    calls = stub_network(monkeypatch)

    assert run(tmp_path, "--force") == 1

    assert covers_of(repo) == [COVER_A, COVER_B]       # 第一条刷了；第二条（没 BV）一个字没动
    assert calls == ["BV1kw411q7S8"]
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
                               "https://i0.hdslb.com/bfs/archive/BV1xx411c7mD.jpg"
                               + fetch_covers.COVER_SUFFIX]
    assert calls == ["BV1xx411c7mD"]                   # 只补新加的那一条
    once = repo.read_text(encoding="utf-8")

    assert run(tmp_path) == 0

    assert repo.read_text(encoding="utf-8") == once    # 幂等：一个字节都不变


def test_force_refreshes_the_values_it_just_migrated(repo, tmp_path, monkeypatch):
    """`--force` 也要先迁移：搬进来的值算"已有"，然后按 BV 全部刷新。"""
    repo.write_text(LEGACY_FILE, encoding="utf-8")
    calls = stub_network(monkeypatch)

    assert run(tmp_path, "--force") == 0

    assert covers_of(repo) == [COVER_A, COVER_B]
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

    assert covers_of(repo) == [COVER_A, None]          # 好的那条照常写，坏的那条没动
    assert calls == ["BV1kw411q7S8"]                   # 缺 BV 的那条连请求都不发
    printed = capsys.readouterr().out
    assert "没有 BV 号" in printed and "二" in printed
    assert "新增 1 条" in printed and "失败 1" in printed


def test_a_failed_fetch_does_not_block_the_other_tracks(repo, tmp_path, monkeypatch, capsys):
    """网络失败 ⇒ 那一条不写（下次重跑即可），其它条照常；退出码 1。"""
    calls = stub_network(monkeypatch, fail=frozenset({"BV1GD4y1m7JY"}))

    assert run(tmp_path) == 1

    assert covers_of(repo) == [COVER_A, None]
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

def test_cache_is_jsonl_and_a_hit_avoids_the_request(repo, tmp_path, monkeypatch):
    cache = tmp_path / "covers.jsonl"
    calls = stub_network(monkeypatch)

    assert run(tmp_path) == 0
    records = [json.loads(line) for line in cache.read_text(encoding="utf-8").splitlines()]
    assert {(record["character"], record["title"]) for record in records} == {("cirno", "一"), ("cirno", "二")}
    assert all(record["url"].startswith("https://") for record in records)
    assert len(calls) == 2

    calls.clear()                                        # `--force` 会重写文件，但请求应当全是缓存命中
    assert run(tmp_path, "--force") == 0
    assert calls == []


def test_cache_misses_when_the_source_bv_changed(repo, tmp_path, monkeypatch):
    """换了 `source` 就是换了视频 ⇒ 缓存必须算未命中（否则 `--force` 也刷不掉一张错封面）。"""
    calls = stub_network(monkeypatch)
    assert run(tmp_path) == 0

    repo.write_text(CHARACTER_FILE.replace("BV1kw411q7S8", "BV1zz411q7S9"), encoding="utf-8")
    calls.clear()
    assert run(tmp_path, "--force") == 0

    assert calls == ["BV1zz411q7S9"]                     # 只重抓换过的那一条
    assert covers_of(repo)[0] == "https://i0.hdslb.com/bfs/archive/BV1zz411q7S9.jpg" + \
        fetch_covers.COVER_SUFFIX


def test_read_cache_survives_a_half_written_line(tmp_path):
    """JSONL 是"可续跑"的文件：最后一行写了一半也不该让整个缓存作废。"""
    cache = tmp_path / "covers.jsonl"
    cache.write_text(json.dumps({"character": "cirno", "title": "一", "bv": "BVx", "url": COVER_A},
                                ensure_ascii=False) + "\n" + '{"character": "cirno", "tit',
                     encoding="utf-8")

    assert fetch_covers.read_cache(cache) == {("cirno", "一"): ("BVx", COVER_A)}


def test_cached_url_is_reused_without_touching_the_network(repo, tmp_path, monkeypatch):
    """缓存里的直链直接拿来写文件（`--dry-run` 不写缓存，但读缓存照旧）。"""
    (tmp_path / "covers.jsonl").write_text("\n".join(json.dumps(record, ensure_ascii=False) for record in [
        {"character": "cirno", "title": "一", "bv": "BV1kw411q7S8", "url": COVER_PIC},
        {"character": "cirno", "title": "二", "bv": "BV1GD4y1m7JY", "url": COVER_B},
    ]) + "\n", encoding="utf-8")
    calls = stub_network(monkeypatch)

    assert run(tmp_path) == 0

    assert covers_of(repo) == [COVER_PIC, COVER_B]     # 缓存里怎么写的就怎么写进去
    assert calls == []


# ------------------------------------------------------------------ 真源 TOML → 快照形状

def test_snapshot_from_the_real_toml_carries_covers(repo, tmp_path, monkeypatch):
    """端到端：真源 TOML（逐条 cover）→ `load_packs` → `pack_snapshot` 的 **JSON 形状没变**。

    `covers` 与 `music` **一一对应**（同一角色、同一顺序），应用侧就是按这个下标取图的；
    形状能进 `json.dumps`（源要把它塞进 `manifest.json`）。
    """
    stub_network(monkeypatch)
    assert run(tmp_path) == 0

    _packs, albums, tracks, cards, covers = packs.load_packs()
    snapshot = packs.pack_snapshot(albums, tracks, cards, covers)

    assert snapshot["characters"] == [{
        "key": "cirno",
        "music": [["demo", "一", "角色曲", "甲"], ["demo", "二", "道中曲", "乙"]],
        "covers": [COVER_A, COVER_B],
    }]
    json.dumps(snapshot)                                  # 真能进 manifest.json
    # 没写 cover 的角色不带这个键（"有才覆盖"，与 card 同一口径）
    assert "covers" not in packs.pack_snapshot(albums, tracks, None, {"other": ["https://x/y.jpg"]})[
        "characters"][0]


def test_repo_snapshot_carries_covers_through_its_cache(repo, tmp_path, monkeypatch):
    """`repo_snapshot()`（带缓存的那条路）也要带上 covers —— 缓存键机制不受影响。"""
    stub_network(monkeypatch)
    assert run(tmp_path) == 0
    monkeypatch.setattr(packs, "_SNAPSHOT_CACHE", {})      # 别让别的用例（或上一次）的缓存顶上

    first = packs.repo_snapshot()
    second = packs.repo_snapshot()                         # 第二次走缓存：没改文件 ⇒ 内容一样

    assert first == second
    assert first["characters"][0]["covers"] == covers_of(repo)
    assert len(packs._SNAPSHOT_CACHE) == 1                 # 缓存只留最新一批（不长成泄漏）
