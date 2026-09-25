"""本仓库（音MAD 曲包数据仓库）的路径常量与查找函数。

与主仓库的 `tmc.repo` 分开：两份工程**零 import**，只靠文件格式（TOML/JSON）当契约。
名字刻意与 `tmc.repo` 对齐，方便两边对照着读。
"""
from __future__ import annotations

import pathlib

#: 仓库根（本文件在 `tools/src/otomads/paths.py` → 上溯 3 层）
ROOT = pathlib.Path(__file__).resolve().parents[3]

#: 真源根。数据仓库里 `packs/`、`sources/`、`loudness/`、`characters.toml` 都在仓库根下，
#: 所以 `DATA` 就是 `ROOT`（保留这个名字是为了与主仓库的写法一致）。
DATA = ROOT

#: 本地曲库助手配置（本机自用，已 gitignore）
DEFAULT_CONFIG = ROOT / "local-source.toml"

def packs_dir() -> pathlib.Path:
    """曲包目录：`<DATA>/packs`。"""
    return DATA / "packs"


def sources_dir() -> pathlib.Path:
    """源注册表目录：`<DATA>/sources`。"""
    return DATA / "sources"


def characters_file() -> pathlib.Path:
    """角色清单：`<DATA>/characters.toml`（带 name/order；由主仓库 `pnpm data:roster` 生成）。"""
    return DATA / "characters.toml"


def pack_roots() -> tuple[pathlib.Path, ...]:
    """曲包根目录。数据仓库只有一处：`<DATA>/packs`（函数而非常量：测试会 monkeypatch `DATA`）。"""
    return (packs_dir(),)


def source_roots() -> tuple[pathlib.Path, ...]:
    """源注册表根目录：`<DATA>/sources`。"""
    return (sources_dir(),)


def find_pack_manifest(pack_id: str) -> pathlib.Path | None:
    """按 :func:`pack_roots` 找曲包清单 `<id>.toml`；找不到返回 `None`。"""
    for root in pack_roots():
        path = root / f"{pack_id}.toml"
        if path.exists():
            return path
    return None


def find_source_registry(mode: str) -> pathlib.Path | None:
    """按 :func:`source_roots` 找源注册表 `<mode>.toml`；找不到返回 `None`。"""
    for root in source_roots():
        path = root / f"{mode}.toml"
        if path.exists():
            return path
    return None


def shown(path: pathlib.Path) -> str:
    """路径尽量相对仓库根显示（测试会把 `DATA` 指到临时目录，那时只能给绝对路径）。"""
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)
