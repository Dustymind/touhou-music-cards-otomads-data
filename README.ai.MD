# 东方谐频拾遗 · 音MAD 曲包数据（otomads）

音MAD 曲包的**真源 + 工具**：曲目表、响度表、音频抓取与裁剪、归档打包。
音频与卡图**不进仓库** —— 素材由"源"提供（默认是下面那个 CDN）。
本仓库自带 uv 工程，**不依赖任何其他仓库**就能起本地源、抓音频、打包、跑测试。

## 改点什么，怎么做

先查表。**前三种不用本机曲库，push 一下就自动上线。**

| 改什么 | 怎么做 | 要曲库吗 |
|---|---|---|
| 曲目表：歌名 / 作者 / 附加信息 / 卡面引用 / 删曲目 | 改 `packs/otomads/<角色 key>.toml` → `git push` | 不要 |
| 响度表 | 改 `loudness/otomads.json`（或本机重算）→ `git push` | 不要 |
| 站点侧：`wrangler.jsonc` / `media-worker.mjs` / `tools/**` / 工作流 | 改 → `git push` | 不要 |
| **新音频 / 重新裁剪** | 抓 → 量响度 → 打包 → **先传归档、再 push**（五步见下） | 要 |
| **新增 / 替换卡图** | 同上一行（卡图只在本地打包时进得去） | 要 |
| 只重裁了音频、曲目表没动 | 上一行的 ①–④，末尾推个空提交或 `gh workflow run publish.yml` | 要 |
| 只想本地听一遍 | 起 `local_source`，页面加 `?localmusic=127.0.0.1:8011` | 要 |

为什么前三种不用本机：站点内容来自 `media` Release 里那份归档。push 之后 CI 跑 `publish` 工作流 ——
第②段拿**上一份归档里的音频** + 仓库现在的 `packs/` 与响度表重打一份清单（内容变了才换归档），
第③段重新构建站点并用 `wrangler` 部署上线。
逐曲版本号是**内容**哈希 ⇒ 本机打的包与 CI 重打的包**逐字节相同**（`tools/tests/test_repack.py` 盯着这条）。

## 新音频：五步（顺序是命门）

```bash
# ① 抓取 + 按 start_time/stop_time 裁剪（原件留在 .raw/，只改区间不会重下；--dry-run 先看计划）
uv run --project tools python -m otomads.fetch_audio --dry-run
uv run --project tools python -m otomads.fetch_audio --jobs 4

# ② 量响度 → 写 loudness/otomads.json
uv run --project tools python -m otomads.measure_loudness

# ③ 打包（归档布局 = 部署根；没有卡图就不给 --cards）
uv run --project tools python -m otomads.stage_media pack \
    --library <曲库> --cards <卡图目录> --out otomads-media.tar.gz
#    想先自检一遍再传：... stage_media review --archive otomads-media.tar.gz

# ④ 先换归档资产（要 gh auth login + 本仓库写权限）
gh release upload media otomads-media.tar.gz --clobber \
    -R Dustymind/touhou-music-cards-otomads-data

# ⑤ 再推源码（packs/ 是真源，必须推；这次 push 就是触发器）
git commit -am "..." && git push
```

⚠️ **④⑤ 不能反**：先推源码的话，CI 只能拿**旧归档**重打 ⇒ 新曲目"没音频" ⇒ 归档被换成少了那首的版本，
还照常上线（自检只在日志里警告）。弄反了就重做 ④⑤ 覆盖回来。

**只换了音频、`packs/` 一个字没改**（没有 git 事件）⇒ ④ 之后手动触发一次：

```bash
gh workflow run publish.yml -R Dustymind/touhou-music-cards-otomads-data
```

抓取是并发的（`--jobs`，默认 4）：瓶颈全在网络，`--jobs` 给太大容易撞 bilibili 的 **412 风控**。
**GitHub 托管 runner 抓不了**（实测三条真 source 全是 `HTTP Error 412`；86 条 source 全是 bilibili）
⇒ 只能在能上 bilibili 的机器上抓。

## 加一首曲目

```bash
uv run --project tools python -m otomads.parse_ingest_rows rows.txt     # ① 文本行 → JSON
uv run --project tools python -m otomads.ingest_pack \
    --pack otomads --rows tools/ingest_rows_<日期>.json                 # ② 按角色追加（幂等）
uv run --project tools python -m otomads.fetch_audio                    # ③ 抓取 + 刷新响度表
```

② 只追加、不改已有内容，同 `(专辑, 曲名)` 跳过；角色还没有文件就新建一份。
**手写也完全可以**：在 `packs/otomads/<角色 key>.toml` 里加 `[[track]]`。
加完按上一节 ③④⑤ 发布（有新音频就必须走本地打包）。

## 本地跑

需要 **Python ≥ 3.11**（工具用标准库 `tomllib` 读 TOML）与 `uv`；裁剪另需 `ffmpeg`。

```bash
uv sync --project tools            # 建 tools/.venv（依赖只有 yt-dlp）
cp local-source.toml.example local-source.toml
uv run --project tools python -m otomads.local_source    # → http://127.0.0.1:8011/manifest.json
uv run --project tools pytest                            # 141 条
```

- 曲库结构 `<root>/<专辑>/<曲目>.mp3`（默认 `<root>` = 本仓库 `.music/`，与 `local-source.toml` 一起 gitignore）。
- 助手**每次请求现读** `packs/`（几毫秒）⇒ 往 TOML 里加一首立刻生效，不用重启。
- 页面默认连 CDN；想听本机这份就加 `?localmusic=127.0.0.1:8011`（或在设置页填"本地曲库地址"）。

## 写清单的硬规矩

- `[[track]]` 里**不写** `character`：角色由**文件名 = 顶层 key** 决定，写错 key 会静默挂错人。
- key 必须在 `characters.toml` 里。那份清单是**派生**的（被曲包引用到的角色 + 手工追加的音MAD 自有角色），
  **还没有曲目的角色别往里写**。
- 曲目名 = 文件名的 stem（`作者 - 标题`），**不能改名** —— 它是 manifest 的匹配键，也是响度表的键。
- 作者两种写法等价：`author = "甲 & 乙"` 或 `authors = ["甲", "乙"]`（只能写一个；改成数组不用重抓音频）。
- 音频键（可选）：`source`（**一条 = 一首**；多 P 视频默认取 p1，要别的 P 就写 `?p=N`）/ `start_time` / `stop_time`。
- 卡面：角色文件里的 `card = [...]` 覆盖音MAD 侧卡面；**图不入库**。
- `sources/otomads.toml` 的 `table_url` 只能是**绝对 URL** 或**不带前导 `/`** 的相对路径
  （带前导 `/` 在子目录部署时会打到根上 → 404）。`kind = "local"` 是故意留的：`?localmusic=` 与设置页只覆盖它。
- 素材都不进 git ⇒ 归档必须**逐字节可复现**（同一份曲库打两次相同），否则客户端白重下。

## 部署怎么走

```
push(main) ─▶ ① test（pytest + node --test）
           ─▶ ② repack（重打归档；变了才换 media 资产）
           ─▶ ③ deploy（下归档 → python3 tools/build_cdn_site.py → npx wrangler deploy）→ 线上
```

**全在 GitHub Actions 里**（`.github/workflows/publish.yml`，D150）。不用 Cloudflare 的 Workers Builds 与
Deploy Hook：那条路依赖 CF 侧的 Git 集成，连接一失效就**零构建**，而 Deploy Hook 照样回 2xx
（2026-09-25 静默失败了一整天，站点一直停在旧构建上）⇒ 已退役，面板上那个项目可以删掉。

**一次性设置**（只这一步要人做）：CF 面板建一个 API Token（模板 **Edit Cloudflare Workers**；自定义则要
`Workers Scripts:Edit` + `Workers Routes:Edit`），然后：

```bash
gh secret set CLOUDFLARE_API_TOKEN -R Dustymind/touhou-music-cards-otomads-data
```

没配这个 secret 时第③段会**跳过并打一条 notice**（不算失败），所以其余两段照常可用。

- 归档内容：`manifest.json` + `media/otomads/*.mp3` + `loudness/otomads.json`（+ 可选 `cards-otomads/*`）。
- manifest 里的媒体地址默认是**相对**的 ⇒ 同一份归档放域名根或子目录都行；要给绝对地址就在 `stage` 时加 `--base https://host/sub/`。
- 媒体地址带 `?v=<内容哈希前 16 位>` ⇒ 只有真变过的曲目换 URL，不会整包重下。
- manifest 顶层还带 `albums` + `characters`（**曲目表也跟着源走**：这个包有哪些曲目、歌名/作者/附加信息、卡面覆盖），
  消费方严格校验后拿它当数据集 ⇒ 加曲目 / 改裁切 / 换音频**只动本仓库**。
- 响度表也跟着源走：manifest 用 `"loudness": "loudness/otomads.json"` 声明（路径相对 manifest），消费方优先用它。
- 归档当**不可信输入**处理：解包会拒绝绝对路径、`..`、符号/硬链接；铺之前先 `review` 自检
  （清单键、每行文件都在、与 `packs/` 对照），不过就不铺、线上保持原样。
- 两条工作流：`tests`（push/PR 只跑测试：141 条 pytest + 7 条 `node --test`）与 `publish`（push 主链路：测试 → 重打 → 部署；手动入口带 `dry_run`）。
- 站点侧代码必须在仓库里（`wrangler.jsonc` + `media-worker.mjs`）：静态资源那条路**不认 `Range`**，
  所以 `/media/*` 交给小脚本接管，按 `Range` 切 206 并**自己带 CORS**。
- 线上那份在 `otomads-cdn.tsukinomiyako-mangesui.top`（CORS `*`、支持 Range）。换宿主 = 重铺一份 + 改 `table_url`。
- 想自己铺一份（同源自测）：

  ```bash
  uv run --project tools python -m otomads.stage_media stage --archive <URL|路径> --out dist
  uv run --project tools python -m otomads.stage_media stage --from <曲库> --out dist
  ```

  铺素材**不构建应用**，而重新构建会清空 `dist/` ⇒ 素材要重铺。
- 自托管直接下归档（公开仓库，不用令牌）：

  ```bash
  curl -LO https://github.com/Dustymind/touhou-music-cards-otomads-data/releases/download/media/otomads-media.tar.gz
  ```

## 响度表自查（踩过的坑）

`fetch_audio` / `measure_loudness` 写完表后会自查一次并打印提示（只提示、不报错）：

- `⚠️ 响度表缺 N/M 首`：那首还没抓，或不在这个曲库（**合法**，增益按 1）；
- `⚠️ 响度表里有 N 个键对不上任何曲目`：改了作者/曲名后没重跑，或**拿别的曲库量过**（表被覆盖）。

第二条是真踩过的坑：拿别的曲库跑 `fetch_audio` 会按那个曲库重写 `loudness/otomads.json`，
增益全错却悄无声息（只是听感不对，不报错）。键 = 成品文件名 stem（`作者 - 标题`，不带扩展名）。

## 布局

```
characters.toml                 # 允许的角色清单（key / name / order）
packs/otomads.toml              # 曲包清单：[pack] + [[album]]
packs/otomads/<角色 key>.toml   # 一角色一份（35 份有曲目 + 86 份空骨架）
sources/otomads.toml            # 音源注册表：默认源（CDN）+ 响度表路径
loudness/otomads.json           # 本源的响度均衡表（键 = 文件名 stem）
local-source.toml.example       # 本地曲库配置模板（复制成 local-source.toml）
tools/                          # uv 工程：助手 / 格式层 / 抓取 / 响度 / 打包 / 录入（见 tools/README.md）
tools/build_cdn_site.py         # Cloudflare 构建入口（下归档 → 自检 → 铺 dist/）
wrangler.jsonc                  # Worker + 静态资源 + 自定义域名的部署配置（`wrangler deploy` 读它）
media-worker.mjs                # 接管 /media/*：Range + CORS
.github/workflows/              # tests / publish（测试 → 重打归档 → 构建部署）
```

## 现状与沿革

- 规模：**86 首 / 35 个角色**（86 首全带 `source`，16 首带裁剪区间）；角色文件共 **121 份** = 35 份有曲目 + 86 份骨架。
- 裁剪是"**解码后精确切 + 重编码**"：按采样点对齐（不是 mp3 帧边界），代价是二次有损 + 约 0.5 秒/首，只对有区间的 16 首付。
- 消费方（应用侧）把本仓库当 submodule **pin 到 tag**，不跟分支：本仓库改完数据打新 tag 推送，消费方切 tag 重新生成即可。
  本仓库也可以完全独立使用（不起应用，纯当数据与工具）。
- 提交历史是从别处按路径重写搬来的（2026-09-17 → 09-23），之后在这里独立提交；
  `tools/`、`characters.toml`、`loudness/` 于 2026-09-24 迁入（tag `th09.5-260924`）。
