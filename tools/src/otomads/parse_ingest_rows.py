"""把"待录入音MAD"的原始行解析成 downloader 用的 JSON。

支持的两种写法（自动识别）：

1. **新格式（推荐，2026-09 起）**：四个字段各用反引号包起来，顺序固定 ——
   `` `链接` `标题` `作者` `角色` ``
   标题/作者里出现逗号、空格、括号都不会切错 ✓
2. 旧格式：`链接，标题，作者，角色`（半角/全角逗号），**标题里不能有逗号** ——
   解析时从两端取（最后一段=角色、倒数第二段=作者、中间全部=标题）以尽量兼容 ✓

用法：
    uv run python -m otomads.parse_ingest_rows rows.txt      # 写出 tools/ingest_rows_<日期>.json
    uv run python -m otomads.parse_ingest_rows - < rows.txt  # 从标准输入读
"""
from __future__ import annotations
import datetime, json, pathlib, re, sys, tomllib

from . import paths


# 中文名 → 本项目角色 key（key 是罗马字，见 public/data/characters.json）
ALIAS = {
    "琪露诺": "cirno", "蕾蒂·霍瓦特洛克": "letty-whiterock", "蕾蒂": "letty-whiterock",
    "橙": "chen", "爱丽丝·玛格特洛依德": "alice-margatroid", "爱丽丝": "alice-margatroid",
    "莉莉白": "lily-white", "普莉兹姆利巴三姐妹": "prismriver-sisters",
    "魂魄妖梦": "konpaku-youmu", "西行寺幽幽子": "saigyouji-yuyuko",
    "八云蓝": "yakumo-ran", "八云紫": "yakumo-yukari", "雾雨魔理沙": "kirisame-marisa",
    "博丽灵梦": "hakurei-reimu", "露米娅": "rumia", "大妖精": "daiyousei",
    "小恶魔": "koakuma", "帕秋莉": "patchouli-knowledge", "十六夜咲夜": "izayoi-sakuya",
    "蕾米莉亚": "remilia-scarlet", "芙兰朵露": "flandr e-scarlet".replace(" ", ""),
    "伊吹萃香": "ibuki-suika", "莉格露·奈特巴格": "wriggle-nightbug", "莉格露": "wriggle-nightbug",
    "米斯蒂娅·萝蕾拉": "mystia-lorelei", "米斯蒂娅": "mystia-lorelei",
    "上白泽慧音": "kamishirasawa-keine", "因幡　てゐ": "inaba-tewi", "因幡てゐ": "inaba-tewi",
    "因幡帝": "inaba-tewi", "铃仙·U·因幡": "reisen-udongein-inaba", "铃仙": "reisen-udongein-inaba",
    "八意永琳": "yagokoro-eirin", "蓬莱山辉夜": "houraisan-kaguya", "藤原妹红": "fujiwara-no-mokou",
    "八坂神奈子": "yasaka-kanako",
    "梅蒂欣·梅兰可莉": "medicine-melancholy", "梅蒂欣": "medicine-melancholy",
    "因幡天为": "inaba-tewi", "射命丸文": "shameimaru-aya", "风见幽香": "kazami-yuuka",
    "小野塚小町": "onozuka-komachi", "四季映姬·夜摩仙那度": "shiki-eiki-yamazanadu",
}


def parse_line(line: str) -> tuple[str, str, str, str] | None:
    """一行 → (bv, 标题, 作者, 角色中文)。解析不出返回 None。"""
    if "`" in line:                                  # 新格式：按反引号取字段
        parts = [p.strip() for p in re.findall(r"`([^`]*)`", line)]
        if len(parts) >= 4:
            link, title, author, role = parts[0], parts[1], parts[2], parts[3]
        elif len(parts) == 3:                        # 允许不写角色：link/title/author
            link, title, author = parts
            role = ""
        else:
            return None
    else:                                            # 旧格式：两端取
        parts = [p.strip() for p in re.split(r"[，,]", line) if p.strip()]
        if len(parts) < 4:
            return None
        link, role, author = parts[0], parts[-1], parts[-2]
        title = "，".join(parts[1:-2]) if len(parts) > 4 else parts[1]
    bv = re.search(r"(BV[0-9A-Za-z]+)", link)
    if not bv or not title:
        return None
    return bv.group(1), title, author, role


def main() -> int:
    source = sys.argv[1] if len(sys.argv) > 1 else "-"
    text = sys.stdin.read() if source == "-" else pathlib.Path(source).read_text(encoding="utf-8")
    roster = tomllib.loads(paths.characters_file().read_text(encoding="utf-8"))
    keys = {c["key"] for c in roster.get("character", [])}

    rows, problems = [], []
    for line in (l.strip() for l in text.splitlines()):
        if not line or line.startswith("#"):
            continue
        parsed = parse_line(line)
        if parsed is None:
            problems.append(("解析失败", line[:60])); continue
        bv, title, author, role = parsed
        key = ALIAS.get(role.strip())
        if key is None and role.strip() in keys:
            key = role.strip()
        if key is None or key not in keys:
            problems.append(("角色未映射", f"{role!r}（标题 {title[:20]}）")); continue
        if not author:
            problems.append(("作者为空", title[:40])); continue
        rows.append({"bv": bv, "title": title, "author": author, "character": key, "role": role.strip()})

    stamp = datetime.date.today().isoformat()
    out = paths.ROOT / "tools" / f"ingest_rows_{stamp}.json"
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"解析 {len(rows)} 条，异常 {len(problems)} 条 → {paths.shown(out)}")
    for kind, detail in problems:
        print(f"  ⚠️ {kind}: {detail}")
    for r in rows:
        print(f"  {r['bv']:<14} {r['character']:<22} {r['author'][:24]:<26} {r['title'][:36]}")
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
