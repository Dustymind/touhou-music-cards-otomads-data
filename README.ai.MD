# 东方谐频拾遗 · 音MAD 曲包数据（otomads）

音MAD 曲包的**真源 + 工具**。主仓库
[`Dustymind/touhou-music-cards-reconstructed`](https://github.com/Dustymind/touhou-music-cards-reconstructed)
把它作为 **git submodule** 挂在 `data/otomads/`，并在主仓库里生成 `public/data/otomads/*.json`（生成物随主仓库提交）。

本仓库**可以独立使用**：自带 uv 工程，能起完整本地音乐源、抓取/裁剪音频、量响度、录入曲目 —— 不需要主仓库在场。

## 布局

```
characters.toml           # 允许使用的角色清单（key / name / order；主仓库 `pnpm data:roster` 生成）
packs/otomads.toml        # 曲包清单：只放 [pack] 与 [[album]]
packs/otomads/<角色 key>.toml   # 一角色一份：顶层 key + 若干 [[track]]（还没有曲目的角色是**骨架**，见下）
sources/otomads.toml      # 音源注册表：本模式唯一来源 = 本地曲库助手；声明 loudness 表路径
loudness/otomads.json     # 本源自己的响度均衡表（键 = 文件名 stem；**跟着源部署**，见「静态部署」）
local-source.toml.example # 独立跑用的配置示例（复制成 local-source.toml）
tools/                    # 自带 uv 工程：助手 / 格式层 / 抓取 / 响度 / 录入（见 tools/README.md）
```

## 独立跑（不依赖主仓库）

```bash
uv sync --project tools                                   # 建 tools/.venv（运行时依赖只有 yt-dlp）
cp local-source.toml.example local-source.toml            # 曲库默认指向本仓库 .music/

uv run --project tools python -m otomads.local_source     # 完整本地源 → http://127.0.0.1:8011/manifest.json
uv run --project tools python -m otomads.fetch_audio --dry-run
uv run --project tools python -m otomads.fetch_audio --jobs 4   # 抓取/裁剪（并发 4；1 = 串行）
uv run --project tools python -m otomads.measure_loudness
uv run --project tools pytest
```

曲库结构：`<root>/<专辑>/<曲目>.mp3`（默认 `<root>` = 本仓库 `.music/`；`.music/`、`local-source.toml` 都 gitignore）。
主仓库的 `pnpm local` / `pnpm audio:fetch` 只是把 `--config` 指回主仓库那份配置的**路径包装**，不是 Python 依赖。

**抓取是并发的**（`--jobs`，默认 4）：瓶颈**全在网络**（bilibili 的 playurl 往返 + 音频本体），
本地那点活可忽略（实测约 0.25 秒/首，86 首合计约 23 秒），所以并发重叠的是网络等待。
`--jobs` 给太大可能撞 bilibili 的 412 风控，先用默认值试。
（yt-dlp 自己的 `--concurrent-fragments` 对这里**无效**：它只并行 HLS/DASH 的**分片**，
而 bilibili 的音频是单个文件直链。详见 `tools/README.md` 的"两条实现纪律"与主仓库 D132。）

## 口径（硬规矩）

- 清单里**不许**写 `[[track]]`；`[[track]]` 里**不许**写 `character` —— 角色由文件的 `key` 决定，
  且文件名必须等于 `key`（写错一个 key 会让曲目静默错挂）。
- 角色 key 必须在 `characters.toml` 清单里。清单由主仓库 `pnpm data:roster` 生成；**原曲没有的角色**
  （音MAD 自有身份）手动追加后会保留 —— 那是将来 S2 的落脚点（主仓库契约 `docs/otomads-separation-v1.md` §5）。
- **骨架文件**：真源里有、这里还没有曲目的角色，由主仓库 `pnpm data:scaffold` 预置一份骨架
  （顶层只有 `key`，`name`/`order` 以注释带在文件头，另附注释掉的 `[[track]]` 示例）。骨架**不含任何
  `[[track]]`** ⇒ 不进曲目集 ⇒ 对 `contentHash` 与生成物**完全惰性**；命令**幂等**且**绝不覆盖**
  已填过的文件（D137）。
  **为什么不能把角色提前写进 `characters.toml`**：那份清单是**派生**的（= 曲包引用到的角色 + 手工追加的
  原曲没有的角色），把还没有曲目的角色塞进去，主仓库 `tmc.validate` 会判「不再被曲包引用」而报错，
  `pnpm data:roster` 还会把它删掉。
- 卡面：角色文件里可选的 `card = [...]` 覆盖该角色在音MAD 侧的卡面（缺省沿用共享身份），
  配套主仓库 `data/card-sets.toml` 里 `id = "otomads"` 那套 `local_only` 图集。
  **素材不入库**，由用户自己放进主仓库的 `public/cards-otomads/`。
- 音频键（可选）：`source` / `start_time` / `stop_time`。运行时不进 `characters.json`，但会进
  `contentHash` —— 两端音频口径不同会在联机握手期被拒（主仓库契约 `docs/packs-audio-v1.md` §6）。
- 作者：`author = "甲 & 乙"`（**整串**，老写法，原样保留、不按 `&` 拆）或
  **`authors = ["甲", "乙"]`**（多作者，主仓库决策 D135；两者只能写一个）。数组会被规范化成
  `author = "甲 & 乙"` —— **两种写法在磁盘上完全等价**（成品文件名 `作者 - 标题.mp3` 是 manifest
  匹配键与响度表键，不能变），所以把一个老条目改成数组**不用重抓/重裁音频**。
  多作者只影响**显示**：主仓库播放页与关于页按"英文/拼音首字母"排序后并列。
  `python -m otomads.ingest_pack` 也认：录入行里把那一项写成数组（`"author": ["甲", "乙"]`）即落盘成 `authors`。
- 音频与卡面素材都**不在本仓库**：素材由**源**提供（默认是项目 CDN，见下一条）。
- `sources/otomads.toml` 的 `table_url` 有两种合法形态（主仓库 `tmc.build` / `tmc.validate` 会守，D131）：
  **绝对 URL**（本仓库**默认**用的就是这种：`https://otomads-cdn.tsukinomiyako-mangesui.top/manifest.json`）
  或**真相对路径**（`manifest.json`，**不带前导 `/`**，同源取表）。带前导 `/` 是域名根绝对路径，
  站点部署在子目录（GitHub Pages 项目页）时会打到根上 → 404 → 音MAD 所有曲目解析不出地址。
  CDN 那份 manifest 里的曲目地址是**相对路径**，由前端按 **manifest 所在的那一层**解析（主仓库 D141）
  ⇒ 换域名/换子路径都不用改素材；响度表由 manifest 的 `loudness` 键声明（D139），同样相对 manifest。
  `kind = "local"` 是**故意保留的**：设置页「本地曲库地址」与 `?localmusic=` 只覆盖 kind=local 的源 ——
  本机开发就是靠它把默认源指回自己的曲库（`?localmusic=127.0.0.1:8011`，或在设置页填地址再点「应用」）。

## 加一首曲目

```bash
uv run --project tools python -m otomads.parse_ingest_rows rows.txt      # ① 解析 → tools/ingest_rows_<日期>.json
uv run --project tools python -m otomads.ingest_pack \
    --pack otomads --rows tools/ingest_rows_<日期>.json                  # ② 按角色追加（幂等）
uv run --project tools python -m otomads.fetch_audio                     # ③ 抓取/裁剪 + 刷新 loudness/otomads.json
```

② 只追加、不改写已有内容，同 `(专辑, 曲名)` 幂等跳过；角色还没有文件时它**新建**一份（同款表头）。
**手写**也完全可以：先跑一次主仓库的 `pnpm data:scaffold` 拿到骨架，照里面的注释示例填 `[[track]]`
（骨架里那份示例是**占位**，填真值再取消注释）。写完在**本仓库**提交、打新 tag；
主仓库 `cd data/otomads && git checkout <新 tag>`，再 `pnpm data:build && pnpm data:validate`。

## 静态部署：把素材铺进 dist（主仓库 D138）

应用本身是**纯静态**可部署的，唯一缺的是音MAD 的素材 —— 音频（86 首 / 约 324 MB）与音MAD 卡面
**都不进任何仓库**。本仓库的工具把这件事做成"打一次归档 + 构建时铺一次"：

```bash
# ① 打一个可发布的归档（布局 = 部署根）
uv run --project tools python -m otomads.stage_media pack \
    --library <曲库> --cards <卡面目录> --out otomads-media.tar.gz
#    → 归档内容：manifest.json + media/otomads/*.mp3 + loudness/otomads.json（+ 可选 cards-otomads/*）。
#      manifest 里还带**本包自己的曲目表**（`albums` / `characters`，主仓库 D145）。
#      发布成 Release 资产，或直接解到静态站根目录。

# ② 构建时铺进 dist（从归档拉，或直接从本地曲库铺）
uv run --project tools python -m otomads.stage_media stage --archive <URL|路径> --out dist
uv run --project tools python -m otomads.stage_media stage --from <曲库> --out dist
```

主仓库有配套包装：`pnpm media:pack` / `pnpm media:stage` / `OTOMADS_MEDIA_URL=<URL> pnpm media:pull`。
**部署方式**（D148 起：**CF Pages 的 Git 集成自己构建**，本仓库只管打包与发布）：

```bash
# ① 打包：**只能在本地**（manifest 里每首的 `revision` 是本机文件的名字+大小+mtime，D144；曲库不在仓库里）
pnpm media:pack                      # 在主仓库跑（曲库在那台机器上）
# ② 发布归档：发到**本仓库**的 Release（tag `media`，公开仓库 ⇒ 匿名可下）
gh release upload media otomads-media.tar.gz --clobber \
    -R Dustymind/touhou-music-cards-otomads-data
# ③ 触发构建：push 到 `main` 会自动构建；**只换了音频**（git 无变化）时补一下：
gh workflow run trigger-cdn.yml -R Dustymind/touhou-music-cards-otomads-data
```

**构建在 Cloudflare 那边跑**（Pages 项目连的是本仓库、生产分支 `main`），命令就一条
`python3 tools/build_cdn_site.py`（见 `tools/build_cdn_site.py` 的 docstring）：取归档 → 拿本仓库的
`packs/` 对照自检（**不过就不铺**，线上保持原样）→ 解到 `dist/` → 打印摘要。要点：

- **素材不在 git 里**（单文件 >100 MB GitHub 直接拒）⇒ 构建必须去下 Release 里的归档；公开仓库
  ⇒ **CF 那边不需要任何令牌**；
- **manifest 不在构建里重生成**：逐曲版本号是打包那台机器的 mtime 指纹，容器里没有那些文件 ⇒ 原样铺；
- **只换音频的更新推不动构建**（git 没变化）⇒ 用 `trigger-cdn`（POST 一次 Deploy Hook）；若没配那个
  secret，也可以去 CF 面板点 **Retry deployment**。

自托管的人直接下归档，**不用令牌**：

```bash
gh release download media --pattern otomads-media.tar.gz -R Dustymind/touhou-music-cards-otomads-data
# 或者：
curl -LO https://github.com/Dustymind/touhou-music-cards-otomads-data/releases/download/media/otomads-media.tar.gz
```

本仓库另外两个工作流：`tests`（push/PR 跑 135 条 pytest）、`deploy-cdn`（**wrangler 直传的旧路**，
D147 留下的回滚手段：CF 那条验证绿了再删）。主仓库的 Pages 工作流**不碰素材**（D138 的取舍没变），
而且现在**暂时停用**了（应用由 Vercel 部署，见主仓库 D146）。
**线上那一份**铺在 `otomads-cdn.tsukinomiyako-mangesui.top`（Cloudflare，CORS `*`、支持 Range）——
`sources/otomads.toml` 的默认 `table_url` 就指向它（主仓库 D141）⇒ 应用与静态站都不必自带素材。
换宿主/换路径时：重铺一份 + 改 `table_url`（绝对地址即可，manifest 里的相对地址会自动跟着走）。

**口径**（改这里之前先读）：

* **曲目表也跟着源走**（主仓库 D145）：助手与 `stage_media` 写出的 manifest 里带 `albums` + `characters`
  —— "这个包有哪些曲目、什么歌名/作者/附加信息、卡面覆盖是什么"，形状与主仓库构建出来的自带数据**逐字同形**
  （`packformat.music_entry` 对 `tmc.build._pack_music`，两侧测试里放的是**同一份测试向量**）。
  应用侧严格校验、拿它代替随前端部署的那份 —— 于是**加曲目 / 改裁切 / 换音频只动本仓库 + 铺源**，
  主仓库连 pin 都不用动。**清单行 = 曲库里的文件**（地址），**快照 = 曲包 TOML 声明**（曲目）：
  曲库里多一个没写进曲包的 mp3，行数会多、但那一首选不到（曲目表以曲包为准）。
  不传这个参数（老调用方）时输出与改前逐字一致。
* `manifest.json` 的媒体地址默认写**相对**（`media/otomads/<文件>.mp3`）⇒ 归档与**部署基地址无关**：
  域名根与子目录（GitHub Pages 项目页）用**同一份归档**；要绝对地址就 `--base https://host/sub/`。
  前端按 **manifest 所在的那一层**解析这些相对地址（主仓库 D141）⇒ 同一份归档既能铺在**应用同源**下，
  也能铺在**另一个域名**（CDN / R2）上，后者正是注册表默认源现在的形态。
* **响度表跟着源走**（主仓库 D139）：注册表声明的表会被打进归档的 `loudness/otomads.json`，并在 manifest 里用
  `"loudness": "loudness/otomads.json"` 声明（路径**相对 manifest 自身**）。前端**优先**按它取，没声明才回落到
  应用侧那份（`public/data/<模式>/loudness/`）—— 所以同一个源换宿主（R2 / 别的域名 / 独立静态站）不用改应用。
  `stage` 会顺手自查覆盖：**缺** = 那首还没量过（合法，增益按 1）、**多余** = 改名后的残留（都只提示不报错）。
* 曲目名 = **磁盘文件名的 stem**（`作者 - 标题`），文件名**不能改** —— 它是 manifest 的匹配键，
  也是响度表的键（`packformat.audio_stem`）。
* 归档是**不可信输入**（可能来自网络）：`stage` 会拒绝绝对路径、`..`、符号/硬链接（有测试盯着）。

`stage` 结束会**自检**（manifest 行数、专辑、每一行的文件真的在磁盘上），不一致就退出码 1。
它**只铺素材、不构建应用**，所以顺序永远是「先 `pnpm build`，再铺素材」——
**重新构建会清空 `dist/`**（vite 默认 `emptyOutDir`），素材要重铺。归档**可复现**：
同一份曲库打两次逐字节相同。

## 版本与消费

- 主仓库把 submodule **pin 到 tag**（当前 `th09.5-260925`），不用分支跟踪。
- 升级流程：本仓库改数据 → 提交 → `git tag <新 tag>` → 推送；主仓库切到该 tag →
  `pnpm data:build`（生成 `public/data/otomads/*.json`，并把本仓库的 `loudness/otomads.json`
  拷成 `public/data/otomads/loudness/otomads.json`）→ 提交 gitlink 与生成物。
- submodule 在主仓库里**可选**：不初始化它，主仓库仍能 build/check/test（用已提交的生成物），
  只是无法重新生成音MAD 数据集、也没有本地源工具。

## 沿革

本仓库的提交历史是从主仓库按路径重写搬来的（`git filter-repo`，13 条，2026-09-17 → 2026-09-23），
之后在这里独立提交。工具（`tools/`）与 `characters.toml` / `loudness/` 于 2026-09-24 从主仓库迁入
（tag `th09.5-260924`）。

## 来源

曲目归属沿用改版仓库（v2 工作区）的角色归属；`source` 行尾注释即来源（多数是 B 站）。
当前规模：**86 首 / 35 个角色**（86 首全部带 `source`、16 首带裁剪区间）；
角色文件共 **121 份** = 35 份有曲目 + **86 份骨架**（D137，等待手工补全）。

裁剪走"**解码后精确切 + 重编码**"（主仓库 D142，2026-09-25）：起点与时长按**采样点**对齐，
不再落到 mp3 帧边界、也修掉了"输入定位让解码器冷启动 ⇒ 头一帧解不出来"。
代价是二次有损（`.raw` 本身已是 mp3）与 ≈ 0.4–0.6 秒/首的编码开销，**只对带区间的 16 首**付。
口径、实测与"为什么不是换个编码器就完事"见主仓库 `docs/packs-audio-v1.md` §5。

**manifest 带媒体版本号**（主仓库 D144，2026-09-25）：助手与 `stage_media` 写出的清单，
顶层有 `revision`、每行第 4 位是**该曲目自己的**版本号（`packformat.media_revision`：文件名+大小+mtime）。
前端把它拼成 `?v=` 追加到媒体地址 —— 音频变了但链接没变时，URL 会跟着变，于是浏览器/CDN 不会拿旧的顶
（CDN 给 `.mp3` 发的是 `max-age=14400`）。**逐曲**那一位是关键：只让变过的那几首换 URL，
不会让整包 321 MB 全部重下。行形状前三位与原来逐字一致，第 4 位是可选的。

**曲目表跟着源走**（主仓库 D145，2026-09-25）：manifest 多两个**顶层**键 ——
`albums`（包自带专辑）与 `characters`（`{key, music, card?}`；`music` 条目形状与主仓库
`tmc.build._pack_music` 逐字一致，可选地还能带 `name`/`order`/`searchNames` 供 S2 用）。
助手**每次请求现读** `packs/`（35 个 TOML，几毫秒）⇒ 往 TOML 里加一首立刻生效、不必重启；
`stage_media.pack` / `stage`（含 `--base` 重烘）同样带上，只是**归档可复现**这一点不受影响。
老清单没有这两个键时应用照旧走自带那份兜底 —— 向前兼容。

**一条 `source` = 一首曲目**（主仓库 D143，2026-09-25）：抓取传 `noplaylist`，所以指向 bilibili
**多 P 视频**时**默认取 p1**；要别的 P 就把 `?p=N` 写进 `source`（由 yt-dlp 按参数解析，工具不改写 URL）。
解析出多个条目会**直接报错**。状态里记了 `fetch`（抓取口径），**改了抓取实现要把它 +1** ——
不然链接没变、`outHash` 也对得上，幂等检查会把该重下的全部跳过。

## 响度表与曲目的对应关系（自查）

`measure_loudness` 与 `fetch_audio` 写完表后会**自查一次**并打印提示（只提示、不报错）：

- `⚠️ 响度表缺 N/M 首`：曲目里本该有增益、表里却没有 —— 音频不在这个曲库，或还没抓（**合法状态**）；
- `⚠️ 响度表里有 N 个键对不上任何曲目`：改了作者/曲名后没重跑，或**拿别的曲库量过**（表被覆盖）。

第二条正是踩过的坑：拿别的曲库跑 `fetch_audio` 会按那个曲库重写 `loudness/otomads.json`，
增益全错却是**悄无声息**的（听感差别，不是报错）。键的口径 = 成品文件名的 **stem**
（`作者 - 标题`，**不带扩展名**；即 `packformat.audio_stem`，与前端 `gainKeyOf` 一致）。
