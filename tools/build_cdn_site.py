#!/usr/bin/env python3
"""把**已发布**的素材归档铺成站点产物 —— Cloudflare Pages 的构建入口（D148）。

CF Pages 的 Git 集成（连本仓库、生产分支 `main`）在每次 push 后跑这一条：

    python3 tools/build_cdn_site.py

它做四件事（顺序即"不会铺半成品"的顺序）：

1. **取归档**：默认取本仓库 Release（tag `media`）里那份 —— 素材不在 git 里（单文件 >100 MB
   GitHub 直接拒），所以构建必须去下它。公开仓库 ⇒ **不需要任何令牌**。
   换地址/换来源用环境变量 `OTOMADS_MEDIA_URL`（`file://` 也认，本地演练用得上）。
2. **自检**：`stage_media.review_archive` —— 清单五键、每一行都有对应文件，并对照**本仓库的 `packs/`**
   （归档少了 = 改完 packs 忘了重打包；只警告不拦）。**不通过就不铺**（退出码 1 ⇒ CF 那次构建失败，
   线上保持原样 —— 这是"部署有守卫"的全部意义）。
3. **解到输出目录**（默认 `dist/`，CF 面板里的 Build output directory 就是它）：走
   `stage_media.extract`（按**不可信输入**处理：拒绝对路径 / `..` / 链接）。
4. **打印摘要**：行数 / 角色数 / 曲目条目数 / 顶层 `revision` —— CF 构建日志里一眼能对。

⚠️ **manifest 不在构建里重新生成**：每首曲目的 `revision` 是**打包那台机器**上文件的名字+大小+mtime
（D144），构建容器里没有那些文件（归档里 mtime 已归零）⇒ 只能**原样铺**归档里那份。

本地演练（不碰线上）：

    OTOMADS_MEDIA_URL=file://$PWD/otomads-media.tar.gz OTOMADS_OUT=dist \\
        python3 tools/build_cdn_site.py
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import shutil
import sys
import urllib.request
from datetime import datetime, timezone

#: 让 `import otomads` 生效：这个脚本在 `tools/` 下，包在 `tools/src/`（CF 的构建镜像里没有 uv，
#: 也不装依赖 —— 工具是**纯标准库**，所以只补一条 sys.path 就够）
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "src"))

from otomads import stage_media  # noqa: E402

#: 默认的归档地址：本仓库 Release 的 `media` 通道（发布时用 `--clobber` 原地换）
DEFAULT_URL = ("https://github.com/Dustymind/touhou-music-cards-otomads-data"
               "/releases/download/media/otomads-media.tar.gz")

#: 站点自己的响应头，写进构建产物（Workers 静态资源认 `_headers`，放在**资源目录里**、不会被当资源发出去）。
#:
#: **必须写**：Workers 静态资源默认**不带任何 CORS 头**，而应用是在别的源上用 `fetch()` 读这份 manifest 的
#: ⇒ 少了 `Access-Control-Allow-Origin` 就会被浏览器挡掉，表现成"源状态一直 error、曲目表永远走兜底"
#: （换到新家但等于没改）。老 Pages 项目自带这个头，搬过来时丢了 —— 2026-09-25 实测踩到。
#: 顺带把缓存策略显式钉住（值取自老站实测：清单/响度表每次校验、媒体 4 小时），不再依赖 zone 规则。
HEADERS_FILE = """\
# 素材站的响应头（由 tools/build_cdn_site.py 生成；Workers 静态资源会解析它、不会把它当资源发出去）
#
# 少了 CORS 这一条，应用（在别的源上）fetch 这份 manifest 会被浏览器挡掉 —— 换到新家也等于没改。
/*
  Access-Control-Allow-Origin: *
  Access-Control-Expose-Headers: Content-Range, Content-Length, Accept-Ranges
  X-Content-Type-Options: nosniff

# 清单与响度表：每次校验。它们是"当前状态"，被压住就等于线上没更新（D144 的口径）
/manifest.json
  Cache-Control: public, max-age=0, must-revalidate

/loudness/*
  Cache-Control: public, max-age=0, must-revalidate

# 媒体：**现在由 media-worker.mjs 自己设这个头**（`/media/*` 先过脚本，而 `_headers` 不作用于
# Worker 生成的响应）；这一条是"万一脚本没生效"的兜底，也当自文档留着。
/media/*
  Cache-Control: public, max-age=14400, must-revalidate
"""

#: `_headers` 在部署根里的固定名字（Workers / Pages 都认这个名字）
HEADERS_NAME = "_headers"

#: 这次构建的标记：`curl <站点>/build-info.json` 一眼看出"线上是哪一次构建、用的哪份归档"。
#: 为什么需要它：Workers 静态资源的缓存键**不含查询串**，`?ci=` 绕不过去 —— 判断"新构建上没上"
#: 只能靠一个**新路径**（这个文件之前不存在 ⇒ 第一次请求必然回源）。不是机密，只是自述。
BUILD_INFO_NAME = "build-info.json"


def fetch(url: str, target: pathlib.Path) -> pathlib.Path:
    """取归档到 `target`（认 `file://` 与 http(s)，后者跟随重定向）。"""
    print(f"⬇️  取归档：{url}")
    if url.startswith("file://"):
        shutil.copyfile(url[len("file://"):], target)
    else:
        urllib.request.urlretrieve(url, target)
    print(f"    落盘 {target}（{target.stat().st_size} B）")
    return target


def main() -> int:
    url = os.environ.get("OTOMADS_MEDIA_URL", DEFAULT_URL)
    archive = pathlib.Path(os.environ.get("OTOMADS_ARCHIVE", "otomads-media.tar.gz"))
    out = pathlib.Path(os.environ.get("OTOMADS_OUT", "dist"))

    fetch(url, archive)

    problems, warnings = stage_media.review_archive(archive)
    for warning in warnings:
        print(f"⚠️  {warning}")
    if problems:
        print("❌ 归档自检没过，**不铺**（线上保持原样）：")
        for problem in problems[:10]:
            print(f"  - {problem}")
        if len(problems) > 10:
            print(f"  …还有 {len(problems) - 10} 条")
        return 1

    if out.exists():
        shutil.rmtree(out)                      # 构建目录要干净：上一次的残留不能混进来
    stage_media.extract(archive, out)
    (out / HEADERS_NAME).write_text(HEADERS_FILE, encoding="utf-8")
    print(f"    写 {out}/{HEADERS_NAME}：CORS `*` + 清单/响度表每次校验 + 媒体 4 小时")
    info = {
        "builtAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "archive": url,
        "archiveSha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "commit": os.environ.get("WORKERS_CI_COMMIT_SHA", os.environ.get("GITHUB_SHA", "")),
        "branch": os.environ.get("WORKERS_CI_BRANCH", os.environ.get("GITHUB_REF_NAME", "")),
    }
    (out / BUILD_INFO_NAME).write_text(json.dumps(info, ensure_ascii=False, indent=1) + "\n",
                                      encoding="utf-8")
    print(f"    写 {out}/{BUILD_INFO_NAME}：{info['commit'][:12] or '(无 commit 信息)'} @ {info['builtAt']}")

    manifest = stage_media.manifest_of(archive)
    entries = sum(len(character["music"]) for character in manifest["characters"])
    print(f"✅ 铺好 {out.resolve()}：{len(manifest['tracks'])} 行地址 / "
          f"{len(manifest['characters'])} 个角色 / {entries} 条曲目条目 / "
          f"顶层 revision {manifest.get('revision')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
