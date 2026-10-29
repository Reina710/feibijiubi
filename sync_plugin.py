# -*- coding: utf-8 -*-
"""把仓库里的插件源码同步到 AstrBot 的插件目录。

仓库里的 feibijiubi/ 是源码副本，运行时 AstrBot 加载的是
data/plugins/ 下那一份。改完代码跑一次这个脚本，别手工复制。

    python sync_plugin.py            # 仓库 -> AstrBot
    python sync_plugin.py --pull     # AstrBot -> 仓库（在容器里改过时用）
"""
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE / "feibijiubi"

sys.path.insert(0, str(HERE / "host"))
try:
    from config import ASTRBOT_DATA  # noqa: E402
except Exception:
    ASTRBOT_DATA = str(HERE.parent / "astrbot" / "data")

DST = Path(ASTRBOT_DATA) / "plugins" / "feibijiubi"
NAMES = ("main.py", "metadata.yaml")


def main() -> None:
    pull = "--pull" in sys.argv
    a, b = (DST, SRC) if pull else (SRC, DST)
    if not a.is_dir():
        sys.exit(f"源目录不存在: {a}")
    # 防手滑：直接在 AstrBot 目录里改过、又忘了 --pull，直接 push 会把改动覆盖掉。
    # 已经踩过一次（表情情绪那段白改了）。
    if not pull and (DST / "main.py").exists() and (SRC / "main.py").exists():
        if (DST / "main.py").stat().st_mtime > (SRC / "main.py").stat().st_mtime + 1:
            sys.exit(
                "AstrBot 目录里的 main.py 比仓库新，直接同步会覆盖掉。\n"
                "先跑 python sync_plugin.py --pull 把改动收回仓库。"
            )
    b.mkdir(parents=True, exist_ok=True)
    for n in NAMES:
        if (a / n).exists():
            shutil.copy2(a / n, b / n)
            print(f"{a / n}  ->  {b / n}")
    print("done. 重启 AstrBot 生效。" if not pull else "done. 记得在仓库里提交。")


if __name__ == "__main__":
    main()
