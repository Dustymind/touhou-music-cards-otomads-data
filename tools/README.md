# 音MAD 数据仓库工具

数据仓库自带的 uv 工程：**与主仓库零依赖**（不 import `tmc.*`，也不做 path 依赖），两边只靠文件格式当契约。

| 模块 | 作用 |
|---|---|
| `otomads.local_source` | 本地曲库助手：`/manifest.json` + `/media/...`（Range/CORS、端口回退、按请求头现拼地址） |
| `otomads.packformat` | 曲包格式层：读 `packs/`、严格校验键名、音频路径/时间工具、`loudness_path()` |
| `otomads.ingest_pack` | 把录入行按角色追加进 `packs/<包>/<key>.toml`（幂等；校验 `characters.toml` 清单） |
| `otomads.fetch_audio` | yt-dlp 抓取 + ffmpeg 裁剪；按源刷新 `loudness/<包>.json` |
| `otomads.loudness` / `otomads.measure_loudness` | 逐曲响度均衡：度量核心 + CLI |
| `otomads.parse_ingest_rows` | 录入原始行 → `tools/ingest_rows_<日期>.json` |
| `otomads.ingest_otomads` / `otomads.ingest_local_audio` | 批量下载 / 本地待转音频入库 |

## 独立跑（不依赖主仓库）

```bash
uv sync                                             # 建 .venv（运行时依赖只有 yt-dlp）
cp local-source.toml.example local-source.toml      # 曲库默认指向本仓库 .music/

uv run python -m otomads.local_source               # 起完整本地源：http://127.0.0.1:8011/manifest.json
uv run python -m otomads.measure_loudness           # 量 .music/otomads → loudness/otomads.json
uv run python -m otomads.fetch_audio --dry-run      # 看抓取/裁剪计划
uv run python -m otomads.ingest_pack --pack otomads \
    --rows tools/ingest_rows_<日期>.json --dry-run  # 看录入计划
uv run pytest                                       # 工具测试
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
写完在本仓库提交、打 tag；主仓库切到新 tag 后再跑 `pnpm data:build`（生成物与响度表都随主仓库提交）。

## 契约

曲目键、时间语义与抓取流程以主仓库 `docs/packs-audio-v1.md` 为准；本仓库 README 写"写入侧"要点。
抓取要 **yt-dlp**（uv 管）、裁剪/量响度要系统 **ffmpeg**。
