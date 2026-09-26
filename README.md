# 音 MAD 数据集 - 东方谐频拾遗 ~ Forgotten Harmonic Frequencies in Cards and Otomads

⚠⚠⚠AI 生成警告⚠⚠⚠  
此项目处于极早期阶段，仅仅刚好能用，且本体代码过于混乱

> 此仓库为音 MAD 数据集。  
> 尚未完善，欢迎提交 PR / issues。

由于 AI 生成的 README 过于冗长，此处仅简要提及必需的部分。  
AI 生成的原版 README 见 README.ai.MD。

## 本地音乐源服务器搭建指南

> 如果你只是想添加音 MAD 曲目，可以暂时跳过这栏。

<details>
<summary>点击展开</summary>

> TODO：非原曲非音 MAD 的自定义源模式还没做完（）

如果你想测试一下新添加的音 MAD，或者是建立属于自己的音 MAD 源，可以按照如下步骤部署本地服务器。  
部署静态源的方法也会一并提及。

前置依赖：uv (Python 3.11+), ffmpeg

1. 安装依赖

```
uv sync --project tools
```

如果依赖下载失败或过于缓慢，可以临时指定镜像源下载：  
（此处使用[校园网联合镜像站](https://mirrors.cernet.edu.cn/)的自动重定向源，当然你也可以自行换成别的源）

```
# Linux / macOS
UV_INDEX_URL=https://mirrors.cernet.edu.cn/pypi/web/simple uv sync --project tools

# Windows
$env:UV_INDEX_URL="https://mirrors.cernet.edu.cn/pypi/web/simpl"
v sync --project tools
```

2. 抓取音 MAD

```
uv run --project tools python -m otomads.fetch_audio --jobs 4
```

如果下载失败，请检查链接是否正确，或者代理环境变量有没有传入终端。

3. 部署源

<details>
<summary>方案 A. 部署本地服务器（点击展开）</summary>

```
cp local-source.toml.example local-source.toml
uv run --project tools python -m otomads.local_source
```

注：  
如果要让源在局域网可用，需查看自己的局域网 ip，然后将 `local-source.toml` 中的 `host = "127.0.0.1"` 改成对应 ip.  
然后你得在防火墙中放行端口，Windows / macOS / 114514 种 Linux 发行版下的操作各不相同，请根据实际情况自行调整，或者询问 AI 如何操作。  
> 或者我帮你问好了 [Windows 下的教程](https://chat.deepseek.com/share/rmwulpbvn53dtdwoxh)，照着操作放行端口即可（默认端口为 8011，以实际控制台为准）。

（不要让 AI 直接操作，尤其是在 `因为有 114514 种发行版所以有 1919810 种变数` 的 Linux 下！）

当然，图省事直接改成 `host = "0.0.0.0"` 也不是不行，能访问你的本机 ip 的所有人都可以连接。  
但如果是在按流量计费的服务器上部署，可能会喜提账单一份。  

</details>

<details>
<summary>方案 B. 构建静态源扔 Github Pages, Cloudflare Pages, 又或者是别的什么地方（点击展开）</summary>

```
uv run --project tools python -m otomads.stage_media pack --out otomads-media.tar.gz
```

然后拿着这 .tar.gz 玩意里的文件夹，原样部署到什么地方都行。  
至于 CI 什么的，因为阿 B 把 GitHub CI 等一众数据中心的 ip 拦了，做不到（摊手）。

</details>

4. 应用至歌牌游戏

向 `设置 -> 音乐源 -> 音 MAD 模式 -> 本地曲库地址` 中，填入你自部署曲库的地址即可（127.0.0.1 那个也可用）。  
如果你想回到“官方”源（即我部署的源），点重置即可。

</details>

## 角色数据编辑指南

> 注：自定义卡面尚未形成规范（我也没想好怎么设计音 MAD 卡面啊）

位置：`packs/otomads/[character-name].toml`

注意：  
非注释区域中，所有符号均为半角符号，注意空格。  
未标注为可选参数的均得填写。

可能文本过于冗长，还请见谅。

<details>
<summary>如果要给一个角色添加多个音 MAD（点击展开）</summary>

```
# 一个角色可以有多首曲目，因此你也可以给一个角色添加多个音 MAD。
# 参考下列格式即可，理论上没有曲目上限。

[[track]]
metadata-a = ...
metadata-b = ...
...

[[track]]
metadata-a = ...
metadata-b = ...
...

[[track]]
metadata-a = ...
metadata-b = ...
...
```

</details>

<details>
<summary>文件格式详解（点击展开）</summary>

```
# 角色名罗马音，保持默认即可。
key = "aki-minoriko"

[[track]]
# 原曲模式中为对应的原作/专辑，音 MAD 数据包中仅以虚拟专辑 "otomads" 代替。
# 不过**未来**不排除这种可能性：这个音 MAD 数据集已经足够庞大，可以以流派/风格给音 MAD 分类。
album = "otomads"

# （可选参数，如需留空可删除）
# （靠北这个示例有点神人）
# author = "凡文"
# 或 author = ["_Karasu_", "DJRicher"]
# 或 author = ["电棍Otto", "Last炫、", "山泥若"]
# 把参与制作了该音 MAD（单体/合作/片段）的人全写上即可。
# 理论无上限，不过对于人数较多的合作，可以自行决定是否省略该字段填写。
# 此时直接删除该行即可。
author = "作者名"

# 直接照抄即可。不过在我的 commit 中，title 可能有如下改动。
# 原标题过长，可能会删一点不必要的部分
# 有时候会使用未翻译的原标题
# 原标题过于谔谔而被和谐（目前东方食雪汉那个被和谐了）
# 必要时可以参考着修改一下。
title = "曲名"

# extra = "角色曲 | 道中曲 | 更多道中曲 | 秘封曲"
# 角色曲：通常情况下，在整数作/格斗作登场时作为 boss 的曲目。其余小数点则根据具体情况分析。
# 道中曲：仅限初登场作品，作为道中 boss 出现时，对应的道中曲。
# 更多道中曲：非初登场作品，作为道中 boss 出现时，对应的道中曲。
# 秘封曲：在秘封系列专辑中收录的曲目。
#
# 注：为了便于理解，部分并非角色曲的曲目，此处也归为角色曲。
# 如："御伽の国の鬼が島" 和 "砕月" 均记录为伊吹萃香的角色曲。
# 对于音 MAD 分支数据集，界限可以更模糊些。此数据集将 "東方萃夢想" 也一并视作伊吹萃香的角色曲。
#
# 此概念照抄自 lightbulb128 版本的歌牌，并加以修改 (https://lightbulb128.github.io/touhou-card-player-v3/)
# 原版的曲目分类方法有点乱（至少我是这么觉得），于是我重组了下，此选项即是重组产物之一。
extra = "角色曲"

# yt-dlp (https://github.com/yt-dlp/yt-dlp) 能识别的链接即可。
# 包括但不限于 Bilibili / Youtube / Niconico.
# 冷知识：直链也在支持范围内。
#
# 但需注意，测试下载时建议使用最新版 yt-dlp。
# 以及在进行本地构建时，根据视频源实际情况，请确保你的网络与国际互联网连接畅通。
# （目前仅包含 Bilibili 源，暂时无需担心这个。）
source = "https://www.bilibili.com/video/BV……"

# （可选参数，如需留空可删除）
# 现有的大多数源都是视频源，部分源还保留了开场前摇/素材展示等。
# 调整这两个参数可以将对应部分剪掉。
# 注意，即使仅精确到秒，后三位依旧得保留。
#
# 获取时间码的方式：
# 打开你的 DAW / 剪辑工具对轴（前提是精度支持）。
# 或者用支持显示时间码的视频播放器查看（如 mpv）。
# 如果没有上述软件，可以安装 REAPER 用于对轴。
# (https://www.reaper.fm/download.php)
start_time = "00:00:00.000"
stop_time = "00:00:30.000"
```

</details>
