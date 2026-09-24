# 东方歌牌 · 音MAD 曲包数据（otomads）

音MAD 曲包的**真源 + 工具**。主仓库
[`Dustymind/touhou-music-cards-reconstructed`](https://github.com/Dustymind/touhou-music-cards-reconstructed)
把它作为 **git submodule** 挂在 `data/otomads/`，并在主仓库里生成 `public/data/otomads/*.json`（生成物随主仓库提交）。

本仓库**可以独立使用**：自带 uv 工程，能起完整本地音乐源、抓取/裁剪音频、量响度、录入曲目 —— 不需要主仓库在场。

## 布局

```
characters.toml           # 允许使用的角色清单（key / name / order；主仓库 `pnpm data:roster` 生成）
packs/otomads.toml        # 曲包清单：只放 [pack] 与 [[album]]
packs/otomads/<角色 key>.toml   # 一角色一份：顶层 key + 若干 [[track]]
sources/otomads.toml      # 音源注册表：本模式唯一来源 = 本地曲库助手；声明 loudness 表路径
loudness/otomads.json     # 本源自己的响度均衡表（键 = 文件名 stem）
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
- 音频与卡面素材都**不在本仓库**：`kind = "local"`，由本机曲库助手从 `<曲库>` 提供。
- `sources/otomads.toml` 的 `table_url` 必须是**真相对路径**（`manifest.json`，**不带前导 `/`**）：
  带前导 `/` 是域名根绝对路径，站点部署在子目录（GitHub Pages 项目页）时会打到根上 → 404 →
  音MAD 所有曲目解析不出地址。主仓库 `tmc.build` / `tmc.validate` 会守这条（主仓库决策 D131）。

## 加一首曲目

```bash
uv run --project tools python -m otomads.parse_ingest_rows rows.txt      # ① 解析 → tools/ingest_rows_<日期>.json
uv run --project tools python -m otomads.ingest_pack \
    --pack otomads --rows tools/ingest_rows_<日期>.json                  # ② 按角色追加（幂等）
uv run --project tools python -m otomads.fetch_audio                     # ③ 抓取/裁剪 + 刷新 loudness/otomads.json
```

② 只追加、不改写已有内容，同 `(专辑, 曲名)` 幂等跳过。写完在**本仓库**提交、打新 tag；
主仓库 `cd data/otomads && git checkout <新 tag>`，再 `pnpm data:build && pnpm data:validate`。

## 版本与消费

- 主仓库把 submodule **pin 到 tag**（当前 `th09.5-260924`），不用分支跟踪。
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
当前规模：**86 首 / 35 个角色**，其中 84 首带 `source`、16 首带裁剪区间。

## 响度表与曲目的对应关系（自查）

`measure_loudness` 与 `fetch_audio` 写完表后会**自查一次**并打印提示（只提示、不报错）：

- `⚠️ 响度表缺 N/M 首`：曲目里本该有增益、表里却没有 —— 音频不在这个曲库，或还没抓（**合法状态**）；
- `⚠️ 响度表里有 N 个键对不上任何曲目`：改了作者/曲名后没重跑，或**拿别的曲库量过**（表被覆盖）。

第二条正是踩过的坑：拿别的曲库跑 `fetch_audio` 会按那个曲库重写 `loudness/otomads.json`，
增益全错却是**悄无声息**的（听感差别，不是报错）。键的口径 = 成品文件名的 **stem**
（`作者 - 标题`，**不带扩展名**；即 `packformat.audio_stem`，与前端 `gainKeyOf` 一致）。
