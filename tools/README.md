# 音MAD 数据仓库工具

数据仓库自带的 uv 工程：**与主仓库零依赖**（不 import `tmc.*`，也不做 path 依赖），两边只靠文件格式当契约。

| 模块 | 作用 |
|---|---|
| `otomads.local_source` | 本地曲库助手：`/manifest.json` + `/media/...`（Range/CORS、端口回退、按请求头现拼地址；清单里还带**本包自己的曲目表** `albums`/`characters`，主仓库 D145） |
| `otomads.packformat` | 曲包格式层：读 `packs/`、严格校验键名、音频路径/时间工具、`pack_snapshot()`（曲目表快照）、`loudness_path()` |
| `otomads.ingest_pack` | 把录入行按角色追加进 `packs/<包>/<key>.toml`（幂等；校验 `characters.toml` 清单） |
| `otomads.fetch_covers` | 抓**每条曲目**的 B 站封面直链 → 写进**那一条 `[[track]]` 里的 `cover`**（`--dry-run` / `--force` / `--jobs` / JSONL 缓存；文本级写回、默认**只补没有的**、旧形状的顶层 `cover` 数组自动迁移） |
| `otomads.fetch_audio` | yt-dlp 抓取 + ffmpeg 裁剪；**并发**（`--jobs`，默认 4）；按源刷新 `loudness/<包>.json` |
| `otomads.loudness` / `otomads.measure_loudness` | 逐曲响度均衡：度量核心 + CLI |
| `otomads.parse_ingest_rows` | 录入原始行 → `tools/ingest_rows_<日期>.json` |
| `otomads.ingest_otomads` / `otomads.ingest_local_audio` | 批量下载 / 本地待转音频入库 |
| `otomads.stage_media` | 静态部署三步：`pack` 打归档（`manifest.json` + `media/<专辑>/*.mp3` + **本源响度表** + 可选卡面；**可复现**）、`stage` 铺进 `dist/`（从归档或本地曲库；自检 manifest ↔ 音频 ↔ 响度表覆盖）、**`review` 铺之前自检一个待发布的归档**（清单五键、每行都有文件、并对照本仓库 `packs/`；主仓库 D138/D139/D145/**D147**）。归档按**不可信输入**处理（拒绝对路径/`..`/链接） |
| `build_cdn_site.py`（脚本，不在包里） | **Cloudflare Pages 的构建入口**：`python3 tools/build_cdn_site.py` → 取 Release 归档 → `review` → 解到 `dist/`（主仓库 D148）。CF 的构建镜像自带 Python 3.13、不装依赖 ⇒ 只补一条 `sys.path`（工具是纯标准库） |

## 独立跑（不依赖主仓库）

```bash
uv sync                                             # 建 .venv（运行时依赖只有 yt-dlp）
cp local-source.toml.example local-source.toml      # 曲库默认指向本仓库 .music/

uv run python -m otomads.local_source               # 起完整本地源：http://127.0.0.1:8011/manifest.json
uv run python -m otomads.measure_loudness           # 量 .music/otomads → loudness/otomads.json
uv run python -m otomads.fetch_audio --dry-run      # 看抓取/裁剪计划
uv run python -m otomads.fetch_audio --jobs 4       # 抓取/裁剪（并发 4；1 = 串行）
uv run python -m otomads.ingest_pack --pack otomads \
    --rows tools/ingest_rows_<日期>.json --dry-run  # 看录入计划
uv run python -m otomads.fetch_covers --dry-run      # 看封面抓取计划（默认**逐条补缺**：
                                                     #  已经有 cover 的一条都不动；旧形状的顶层
                                                     #  数组自动迁移；整包重抓用 --force）
uv run python -m otomads.stage_media review --archive <归档>   # 铺之前自检（CDN 工作流用的就是它）
uv run python -m otomads.stage_media repack --previous <归档> --out <归档>   # CI 用：仓库真源 + 旧归档的媒体重打
python3 tools/build_cdn_site.py                     # CF Pages 的构建入口（本地演练：加 OTOMADS_MEDIA_URL=file://…）
uv run pytest                                       # 工具测试（CI 里由 .github/workflows/tests.yml 跑）
```

`local-source.toml`、`.music/`、`.uv/` 都已 gitignore：曲库与配置是机器相关的，不进仓库。
主仓库的 `pnpm local` / `pnpm audio:fetch` 只是把 `--config` 指回主仓库那份配置的**路径包装**，
不是 Python 依赖。

## 录入一条新曲目

```bash
uv run python -m otomads.parse_ingest_rows rows.txt          # ① 解析 + 校验 → tools/ingest_rows_<日期>.json
uv run python -m otomads.ingest_otomads                      # ② （可选）按内置清单批量下载
uv run python -m otomads.ingest_pack \
    --pack otomads --rows tools/ingest_rows_<日期>.json      # ③ 按角色追加进 packs/otomads/<key>.toml
uv run python -m otomads.fetch_audio                         # ④ 抓取/裁剪音频 + 刷新 loudness/otomads.json
```

③ 只追加、不改写已有内容，同 `(专辑, 曲名)` 幂等跳过；角色 key 必须在 `characters.toml` 清单里。
写完在本仓库提交推送；主仓库切到那个 **commit**（`git -C data/otomads checkout <commit>`）后再跑
`pnpm data:build`（生成物与响度表都随主仓库提交）。tag 可选，只是里程碑标记。
封面（**每条 `[[track]]` 里自己的 `cover`**，一条曲目一张）由 `otomads.fetch_covers` 生成/补缺：
它**逐条**来 —— 已经有 `cover` 的一条都不动（手工覆写与工具补缺共存），旧形状的顶层
`cover = [...]` 数组会先被**自动迁移**进各条曲目（不联网、幂等）；要按当前 `source` 整包重抓用
`--force`（**会盖掉手改**）。

## 契约

曲目键、时间语义与抓取流程以主仓库 `docs/packs-audio-v1.md` 为准；本仓库 README 写"写入侧"要点。
抓取要 **yt-dlp**（uv 管）、裁剪/量响度要系统 **ffmpeg**。

## 两条实现纪律（改了会疼）

1. **并发只在应用层**：yt-dlp 的 `--concurrent-fragments` 只并行 HLS/DASH 的**分片**，而 bilibili 的音频
   是单个文件直链 —— 对抓取没有帮助。`fetch_audio` 因此自己开线程池（`--jobs`），瓶颈全在网络：
   本地开销实测只有约 0.25 秒/首（`YoutubeDL()`；**裁剪**另算 —— D142 之后是"解码后精确切 + V0 重编码"，
   ≈ 0.4–0.6 秒/首，但只有带区间的 16 首付这笔），86 首合计约 30 秒。
   并发下的三处不变量：状态**进线程池前读全**（运行期只有写）、状态落盘加锁、
   "同源同区间"的认领与产出在**同一把锁**里（否则两条同源曲目会白裁两遍）。
2. **每个 `ffmpeg` / `yt-dlp` 子进程都要显式重定向 stdin**（`stdin=subprocess.DEVNULL`，ffmpeg 另加
   `-nostdin`）：子进程继承终端的 stdin 时，ffmpeg 会去接管它，异常路径退出后终端可能**不再回显**。
   量响度一轮就 86 次机会。这条由 `tests/test_pack_audio.py::test_every_ffmpeg_and_ytdlp_call_gets_its_own_stdin`
   静态守着（读源码 AST，不跑子进程）。
