# 东方歌牌 · 音MAD 曲包数据（otomads）

音MAD（音MAD 曲包）的**真源**。主仓库
[`Dustymind/touhou-music-cards-reconstructed`](https://github.com/Dustymind/touhou-music-cards-reconstructed)
把它作为 **git submodule** 挂在 `data/otomads/`，并在主仓库里生成 `public/data/otomads/*.json`（生成物随主仓库提交）。

## 布局

```
packs/otomads.toml              # 曲包清单：只放 [pack] 与 [[album]]
packs/otomads/<角色 key>.toml   # 一角色一份：顶层 key + 若干 [[track]]
sources/otomads.toml            # 音源注册表：本模式唯一来源 = 本地曲库助手
```

## 口径（硬规矩）

- 清单里**不许**写 `[[track]]`；`[[track]]` 里**不许**写 `character` —— 角色由文件的 `key` 决定，
  且文件名必须等于 `key`（写错一个 key 会让曲目静默错挂）。
- 角色 key 必须已存在于主仓库的 `data/characters/*.toml`（今天的音MAD 角色是原曲 121 个的子集）。
  **之后若要引入原曲没有的角色**，要么主仓库先加同名 key，要么另立一份"音MAD 自己的身份"契约
  （主仓库契约 `docs/otomads-separation-v1.md` §5 的 S2）。
- 卡面：角色文件里可选的 `card = [...]` 覆盖该角色在音MAD 侧的卡面（缺省沿用共享身份），
  配套主仓库 `data/card-sets.toml` 里 `id = "otomads"` 那套 `local_only` 图集。
  **素材不入库**，由用户自己放进主仓库的 `public/cards-otomads/`。
- 音频键（可选）：`source`（yt-dlp 抓取来源）/ `start_time` / `stop_time`（裁剪区间）。
  运行时不进 `characters.json`，但会进 `contentHash` —— 两端音频口径不同会在联机握手期就被拒
  （主仓库契约 `docs/packs-audio-v1.md` §6）。
- 音频与卡面素材都**不在本仓库**：`kind = "local"`，由本机曲库助手
  （主仓库 `tools/src/tmc/local_source.py`）从 `<曲库>` 提供。

## 加一首曲目

工具链留在主仓库，数据写回本仓库：

```bash
# 在主仓库根目录
python3 tools/parse_ingest_rows.py rows.txt
python3 tools/ingest_otomads.py
cd tools && UV_CACHE_DIR=.uv/cache uv run python -m tmc.ingest_pack \
    --pack otomads --rows ../tools/ingest_rows_<日期>.json
```

`ingest_pack` 只追加、不改写已有内容，同 `(专辑, 曲名)` 幂等跳过。写完后在**本仓库**提交、打新 tag；
主仓库 `cd data/otomads && git checkout <新 tag>`，再 `pnpm data:build && pnpm data:validate`，提交生成的 JSON。

## 版本与消费

- 主仓库把 submodule **pin 到 tag**（当前 `th09.5`），不用分支跟踪。
- 升级流程：本仓库改数据 → 提交 → `git tag <新 tag>` → 推送；主仓库切到该 tag →
  `pnpm data:build` → 提交 gitlink 与 `public/data/otomads/*.json`。
- submodule 在主仓库里**可选**：不初始化它，主仓库仍能 build/check/test（用已提交的生成物），
  只是无法重新生成音MAD 数据集。

## 来源

曲目归属沿用改版仓库（v2 工作区）的角色归属；`source` 行尾注释即来源（多数是 B 站）。
当前规模：**86 首 / 35 个角色**，其中 84 首带 `source`、16 首带裁剪区间。
## 沿革

本仓库的提交历史是从主仓库按路径重写搬来的（`git filter-repo`，13 条，2026-09-17 → 2026-09-23），
之后在这里独立提交。
