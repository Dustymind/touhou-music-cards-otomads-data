"""把一批 bilibili 音MAD 下到 .music/otomads/（最高音质），文件名 = `作者 - 标题.mp3`。
用法（在数据仓库根）: uv run python -m otomads.ingest_otomads            # 下载全部未下载的
                      uv run python -m otomads.ingest_otomads --dry-run  # 只打印将要做什么
"""
from __future__ import annotations
import argparse, subprocess, sys, pathlib

ROOT = pathlib.Path(__file__).resolve().parents[3]
DEST = ROOT / ".music" / "otomads"

# (BV 号, 标题, 作者, 角色 key)  —— 作者留空表示"尚未确认"，会跳过
ROWS: list[tuple[str, str, str, str]] = [
    ("BV1kw411q7S8", "无何有之棍 ~ Deep Silver", "鞍山侯国玉电乐团", "cirno"),
    ("BV1m8411Z7Tm", "Crystallize Masuo", "循環", "letty-whiterock"),
    ("BV1Ks4y1N74Y", "Crystallize Otto / 结晶化的白银", "鞍山侯国玉电乐团", "letty-whiterock"),
    ("BV1JM411q7HY", "【2023新春宴音MAD单品】远野马娘前线", "丹花伊吹 & 艾了个拉 & 四季映姬78 & thwy & Binkales", "chen"),
    ("BV1Rh4y1p7Zb", "【东方电气棍】掉业棕（Withered Estick）", "鞍山侯国玉电乐团", "chen"),
    ("BV1Rs4y1779Z", "【东方电气棍】鞍山的唢呐师", "鞍山侯国玉电乐团", "alice-margatroid"),
    ("BV1XpW6eXEQT", "【东方馅挂炒饭】暮里的兄贵之都（天空的花之都）", "龙青瑶RyuuSeiyou", "lily-white"),
    ("BV14H4y1p7wF", "【音Mad】疾走全明星あんさんぶる！", "芙兰厨陈YuYue", "prismriver"),
    ("BV1Wp4y1Y7uy", "【东方Project】猫灵乐团~幻影合奏", "川先僧", "prismriver"),
    ("BV1FUFYeJE8e", "【2025连缘羽梦祭单品】连缘妖妖梦~Ancient Temple", "钬__", "konpaku-youmu"),
    ("BV1By4y1C7ga", "【东方Project】广有射怪猫管我鸟事", "川先僧", "konpaku-youmu"),
    ("BV1FX4y1B7E9", "【东方电气棍】幽雅地退役吧，墨染的电棍 ~ Border of Otto", "鞍山侯国玉电乐团", "saigyouji-yuyuko"),
    ("BV19c411A7PA", "【东方电气棍】击败跋扈", "鞍山侯国玉电乐团", "chen"),
    ("BV1Fs4y1v7H3", "【东方电气棍】电棍幻葬 ~ Netto-Fantasy", "鞍山侯国玉电乐团", "yakumo-ran"),
    ("BV1ok4y1777E", "【东方电气棍】吉吉跋扈 ~ Who Kai Da!", "鞍山侯国玉电乐团", "yakumo-ran"),
    ("BV1bP41147U9", "Otto-Fantasia", "鞍山侯国玉电乐团", "yakumo-yukari"),

    # ── 2026-09 第二批（20 条）──
    ("BV1Eb421a7wu", "【东方电气棍】怎么这么蠢的虫月 ~ Mooned Insect", "鞍山侯国玉电乐团", "wriggle-nightbug"),
    ("BV11ubkzEEcg", "蠢々秋月", "芙兰厨陈YuYue", "wriggle-nightbug"),
    ("BV18rPReMEEo", "【原汤化原食】已经只能听见歌声了", "芙兰厨陈YuYue", "mystia-lorelei"),
    ("BV1LUbXzaE2A", "Beat My Old World 【音MAD|个人全明星】", "霜幻不容筱 & Shui__Huo & 逢封fn_eg", "kamishirasawa-keine"),
    ("BV15s411S7Uw", "【缺明星】少女绮想曲", "FFFanwen", "hakurei-reimu"),
    ("BV1yHQDYHETF", "【东方德夜抄】♿侯国玉的菜盒子　～ Kagome-Kagome♿", "冰の妖睛", "inaba-tewi"),
    ("BV1kq4y1b7sb", "【铁道音MAD】放在降弓用刑处轴温很快就会升高 ~ 狂气的CR（2021东方乘车录单品）", "Satani_ZC & 天空海Skyocean & ItsZTChun & Rendering-Liu & 专治各种乱入", "reisen-udongein-inaba"),
    ("BV1vWBeBEEZa", "【疾走向】今晚七点有狂气之瞳表演", "芙兰厨陈YuYue", "reisen-udongein-inaba"),
    ("BV13W4y1i7mP", "【东方夏银梦】邀请后辈马上袭击～狂气的野兽先辈", "海老ルーミア", "reisen-udongein-inaba"),
    ("BV1AD4y1s72f", "【2022摩多罗国玉祭单品】电棍1969", "锤子先辈", "yagokoro-eirin"),
    ("BV1mhyWBsEc6", "yeah4oyage1029", "myrevolution", "yagokoro-eirin"),
    ("BV1Ck4y1m7qk", "【東方永夜抄】千年幻想郷 ～ History of Thomas", "川先僧", "yagokoro-eirin"),
    ("BV1Yy4y197wQ", "千年マスオ郷　～ History of the Masuo", "マトウダイ", "yagokoro-eirin"),
    ("BV1t1V36EEwc", "【原曲不使用】竹取Fa♂翔", "丶Mikan", "houraisan-kaguya"),
    ("BV1eetSznEPN", "【东方馅挂炒饭】night♂of knight", "van艺复泄", "izayoi-sakuya"),
    ("BV123VV6JEWq", "【东方馅挂炒饭】神圣庄严的古日♂暮里 ~ Suwa Wrestled Gym", "东风股早苗", "yasaka-kanako"),
    ("BV13P4y1z7AV", "【东方新春宴音MAD合作单品⁹】Extend Ash~蓬莱人", "稀神灵梦 & SayoYasuda", "kamishirasawa-keine"),
    ("BV1et411u7FH", "【东方馅挂炒饭】Extend Ash　～ 蓬莱人", "鬼剑士草贝戋神", "kamishirasawa-keine"),
    ("BV14k4y1z7RH", "【東方永夜抄】飘上月球，不死之喵", "川先僧", "fujiwara-no-mokou"),
    ("BV1RK4y1r7wN", "【东方】飘上月球，不死乒乓", "打酱油的小火柴", "fujiwara-no-mokou"),

    # ── 2026-09 第三批（13 条，反引号格式）──
    ("BV1hs411e78v", "【东方馅挂炒饭】宇佐大人的白旗", "青空彼方", "inaba-tewi"),
    ("BV1BT4y1Q7bL", "Masuo大人的白旗", "森林", "inaba-tewi"),
    ("BV1tKVizwEpC", "【东方电气棍】大旋風神哈利路少女", "云生结海lou", "shameimaru-aya"),
    ("BV1Ye411G7gR", "風神魔酢汚", "Rinsaku", "shameimaru-aya"),
    ("BV1BLex6oExB", "【东方食雪汉】**少女", "フランソウ", "shameimaru-aya"),
    ("BV1uM4m1U7hA", "【东方电气棍】今昔幻想乡", "比占庭", "kazami-yuuka"),
    ("BV1wW4y1r7vB", "今昔gay想郷　～ Masuo Land", "みぇち", "kazami-yuuka"),
    ("BV1u34y1z757", "电棍：源流懐♿", "DJGun", "onozuka-komachi"),
    ("BV11SwheoENn", "【东方】厨♂房归航", "龙青瑶RyuuSeiyou", "onozuka-komachi"),
    ("BV1h2421T7PF", "【东方馅挂炒饭】彼岸♂归航", "Deepmau5", "onozuka-komachi"),
    ("BV18b411q7qZ", "【元首】彼岸归元~Magical Führer Tour 2019", "轩缘无痕", "onozuka-komachi"),
    ("BV1fRcJzgE5Q", "【原曲不使用】第六十年的东方审判", "麦恩_exe", "shiki-eiki-yamazanadu"),
    ("BV1ymjTzHEiG", "【东方电气棍】第六十年的职业选手 ～ Pro Player of Sixty Years", "云生结海lou", "shiki-eiki-yamazanadu"),

    # ── 2026-09 第三批追加（1 条）──
    ("BV1sX4y1H7Uf", "剧毒Masuo ～ Forsaken Masuo", "餃子と垂れ", "medicine-melancholy"),

    # ── 2026-09 第三批追加（1 条）──
    ("BV17E411J7e1", "【东方充电男】第六十年的疯子裁判", "鬼剑士草贝戋神", "shiki-eiki-yamazanadu"),

    # ── 2026-09 第四批（10 条，反引号格式）──
    ("BV1Ymt1e1EyJ", "【北京地铁音MAD•东方蓟门桥】来自北京地铁的东方萃梦想！！！", "不可阻挡的TRT", "ibuki-suika"),
    ("BV1554y1G7ef", "【东方】家居萃梦想", "打酱油的小火柴", "ibuki-suika"),
    ("BV1xE411g7U6", "[东方鬼厨音mad]碎碗", "边界之光", "ibuki-suika"),
    ("BV16w4m1v7Md", "【东方萃梦想】碎 月", "芙兰厨陈YuYue", "ibuki-suika"),
    ("BV1PU4y1Z73K", "【東方吔夢想】御伽之国的吔屎警署 ~ Missing Gunpower", "kirby1324", "ibuki-suika"),
    ("BV1uw4m1e7AZ", "【东方馅挂炒饭】新日暮里的地♂牢 ~ Missing Aniki（御伽之国的鬼岛 ~ Missing Power）", "龙青瑶RyuuSeiyou", "ibuki-suika"),
    ("BV1ds4y1J7wK", "Masuo的笔记　～ Mysteriousuo Note", "みぇち", "shameimaru-aya"),
    ("BV1oe4y1C7PR", "【东方雪花帖】傻风之循环 ~ Wind Tangpu", "_萳苝_", "shameimaru-aya"),
    ("BV1VH4y1a71E", "【2024东方新春宴音MAD合作单品】山东之国的不眠夜", "鱼调YuDiao & 天空海Skyocean & DKZ53", "shameimaru-aya"),
    ("BV1YM4y1w7Sd", "对了，一起去回忆京都吧。", "手稲", "shameimaru-aya"),
]

def target(row: tuple[str, str, str, str]) -> pathlib.Path | None:
    bv, title, author, _char = row
    if not author:
        return None
    safe = lambda s: s.replace("/", "／").replace("\\", "＼").strip()
    return DEST / f"{safe(author)} - {safe(title)}.mp3"

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    DEST.mkdir(parents=True, exist_ok=True)
    todo = [(row, target(row)) for row in ROWS]
    for row, path in todo:
        if path is None:
            print(f"跳过（作者待确认）: {row[0]} {row[1]}")
    pending = [(row, path) for row, path in todo if path is not None and not path.exists()]
    print(f"共 {len(ROWS)} 条 → 待下载 {len(pending)} 条")
    for index, (row, path) in enumerate(pending, 1):
        bv, title, _author, _char = row
        print(f"[{index}/{len(pending)}] {bv} → {path.name}")
        if args.dry_run:
            continue
        result = subprocess.run([
            "yt-dlp", "--no-playlist", "-f", "bestaudio/best", "-x",
            "--audio-format", "mp3", "--audio-quality", "0",
            "--no-part", "--quiet", "--no-warnings",
            "-o", str(DEST / f".tmp-{bv}.%(ext)s"),
            f"https://www.bilibili.com/video/{bv}",
        ])
        made = list(DEST.glob(f".tmp-{bv}.*"))
        if result.returncode != 0 or not made:
            print(f"   ✗ 失败（{result.returncode}）")
            for leftover in made:
                leftover.unlink(missing_ok=True)
            continue
        made[0].replace(path)
        print(f"   ✓ {path.stat().st_size // 1024} KB")
    return 0

if __name__ == "__main__":
    sys.exit(main())
