#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""统一路径解析模块。

优先级规则（从高到低）：
1. 显式命令行参数（--julia-cmd / --python-cmd）
2. 环境变量（JULIA_EXE / LIDAR_MIE_PYTHON）
3. 安装目录推导（LIDAR_INSTALL_DIR）
4. 项目内候选路径（如 .pixi/envs）
5. PATH 搜索
6. 回退默认值
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def resolve_julia_executable(explicit_cmd: str | None = None) -> str:
    """统一 Julia 可执行文件解析逻辑。

    Args:
        explicit_cmd: 显式指定的 julia 命令（最高优先级）

    Returns:
        Julia 可执行文件路径

    Raises:
        RuntimeError: 所有候选路径均不可用
    """
    attempted: list[str] = []

    def _try(candidate: str | None) -> str | None:
        if not candidate:
            return None
        attempted.append(candidate)
        p = Path(candidate)
        if p.is_file():
            return str(p.resolve())
        # 尝试 which 查找（处理 PATH 中的命令）
        found = shutil.which(candidate)
        if found:
            attempted.append(f"{candidate} -> {found}")
            return found
        return None

    # 优先级顺序
    candidates = [
        explicit_cmd,                                                           # 1. 显式参数
        os.environ.get("JULIA_EXE"),                                           # 2. 环境变量
        _build_path(os.environ.get("JULIA_BINDIR"), "julia.exe"),            # 3. JULIA_BINDIR
        _build_path(os.environ.get("LIDAR_INSTALL_DIR"), "julia", "bin", "julia.exe"),  # 4. 安装目录
        _build_path(_get_project_root(), "julia", "bin", "julia.exe"),       # 5. 项目内候选路径
        "julia",                                                               # 6. PATH 搜索
    ]

    for candidate in candidates:
        resolved = _try(candidate)
        if resolved:
            return resolved

    raise RuntimeError(
        f"Julia 可执行文件未找到。\n"
        f"请设置 JULIA_EXE 环境变量或确保 julia 在 PATH 中。\n"
        f"尝试过的路径: {attempted}"
    )


def resolve_mie_python_executable(explicit_cmd: str | None = None) -> str:
    """统一 Mie Python 可执行文件解析逻辑。

    Args:
        explicit_cmd: 显式指定的 python 命令（最高优先级）

    Returns:
        Mie Python 可执行文件路径
    """
    attempted: list[str] = []

    def _try(candidate: str | None) -> str | None:
        if not candidate:
            return None
        attempted.append(candidate)
        p = Path(candidate)
        if p.is_file():
            return str(p.resolve())
        return None

    # 优先级顺序
    candidates = [
        explicit_cmd,                                                           # 1. 显式参数
        os.environ.get("LIDAR_MIE_PYTHON"),                                    # 2. 环境变量
        _build_path(os.environ.get("LIDAR_INSTALL_DIR"), ".pixi", "envs", "mie", "python.exe"),  # 3. 安装目录
        _build_path(_get_project_root(), ".pixi", "envs", "mie", "python.exe"),  # 4. 项目内候选路径
        sys.executable,                                                         # 5. 当前 Python（回退）
    ]

    for candidate in candidates:
        resolved = _try(candidate)
        if resolved:
            return resolved

    # 回退到当前 Python（总是可用）
    return sys.executable


def _build_path(base: str | None, *parts: str) -> str | None:
    """安全构建路径（base 为 None 时返回 None）。"""
    if not base:
        return None
    return str(Path(base).joinpath(*parts))


def _get_project_root() -> Path:
    """根据当前文件位置推导项目根目录。"""
    return Path(__file__).resolve().parent.parent


def get_root() -> Path:
    """获取项目根目录，优先使用 LIDAR_INSTALL_DIR。"""
    install_dir = os.environ.get("LIDAR_INSTALL_DIR")
    if install_dir:
        return Path(install_dir)
    return Path(__file__).resolve().parent.parent


def get_install_dir() -> Path | None:
    """获取安装目录。"""
    install_dir = os.environ.get("LIDAR_INSTALL_DIR")
    if install_dir:
        return Path(install_dir)
    return None


def get_julia_depot_path() -> Path | None:
    """获取 Julia depot 路径。"""
    install_dir = get_install_dir()
    if install_dir:
        candidate = install_dir / "julia_depot"
        if candidate.exists():
            return candidate
    project_root = _get_project_root()
    for candidate in (
        project_root / "julia_depot",
    ):
        if candidate.exists():
            return candidate
    depot = os.environ.get("JULIA_DEPOT_PATH")
    if depot:
        candidate = Path(depot)
        if candidate.exists():
            return candidate
    return None
