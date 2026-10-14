"""编译上游 Qlib 的两个 Cython 扩展（滚动/扩展窗口算子），原地生成到 upstream/qlib/data/_libs/。

    python packaging/build_ext.py

为什么要自己编：上游 v0.9.8 在 PyPI 上还没有现成的 pyqlib 轮子（最新轮子是 2025-08 的 0.9.7），
研究台用的是 upstream/ 里固定版本的源码，所以和上游 setup.py 一样把 rolling.pyx、expanding.pyx 编成
rolling.*.so / .pyd。不经过上游 setup.py：它会调用 setuptools-scm 推算版本（源码副本里没有版本库历史会失败），
还会改写 qlib/_version.py。需要：Cython、numpy、C++ 编译器（macOS 的 Xcode 命令行工具 / Windows 的 MSVC，
GitHub Actions 的 macOS、Windows 机器自带）。生成的 .so/.pyd/.cpp 被 upstream/.gitignore 忽略，不进仓库。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

UP = Path(__file__).resolve().parent.parent / "upstream"


def main() -> list[Path]:
    import numpy
    from Cython.Build import cythonize
    from setuptools import Extension
    from setuptools.dist import Distribution

    names = ("rolling", "expanding")
    libs = UP / "qlib" / "data" / "_libs"
    old = os.getcwd()
    # 在临时目录里编：那里没有上游的 pyproject.toml，setuptools-scm 不会介入
    tmp = Path(tempfile.mkdtemp(prefix="qlib-ext-"))
    (tmp / "qlib" / "data" / "_libs").mkdir(parents=True)
    for n in names:
        shutil.copy2(libs / f"{n}.pyx", tmp / "qlib" / "data" / "_libs" / f"{n}.pyx")
    exts = [Extension(f"qlib.data._libs.{n}", [f"qlib/data/_libs/{n}.pyx"], language="c++",
                      include_dirs=[numpy.get_include()]) for n in names]
    os.chdir(tmp)
    try:
        dist = Distribution({"name": "qlib-ext", "ext_modules": cythonize(exts, language_level=3, quiet=True)})
        cmd = dist.get_command_obj("build_ext")
        cmd.inplace = True
        cmd.build_temp = str(tmp / "build")
        cmd.ensure_finalized()
        cmd.run()
        for f in (tmp / "qlib" / "data" / "_libs").iterdir():
            if f.suffix in (".so", ".pyd"):
                shutil.copy2(f, libs / f.name)
    finally:
        os.chdir(old)
        shutil.rmtree(tmp, ignore_errors=True)
    built = sorted(p for p in libs.iterdir() if p.suffix in (".so", ".pyd"))
    if len(built) < 2:
        raise SystemExit(f"没有编译出扩展：{built}")
    return built


if __name__ == "__main__":
    for p in main():
        print("已编译：", p)
    sys.exit(0)
