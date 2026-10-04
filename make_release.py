# -*- coding: utf-8 -*-
"""封版快照：把"能跑起来的最小完整集合"打成一个自包含目录。

为什么需要它：这个项目的代码、规则库、测试、设计稿散在几十个文件里，
半年后再打开时不知道"当时哪些文件是配套的"。这个脚本按**白名单**收集文件，
并生成 MANIFEST（含 SHA256 与生成时间），保证任何一份快照都能自我说明。

用法：
    D:\\python\\python.exe make_release.py                 # 生成到 release/<日期>/
    D:\\python\\python.exe make_release.py --no-data       # 不带演示数据

产出目录内容：
    src/            全部源码（*.py，含 Agent/）
    rules/          Agent/rules/*.json（规则库：参数与校验码的唯一来源）
    tests/          test_*.py（723 项测试）
    docs/           README.md / GOTCHAS.md / 设计稿
    data/           演示数据（可关掉）
    requirements.txt
    MANIFEST.txt    文件清单 + SHA256 + 生成时间 + 测试结果（若已知）
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SKIP_DIRS = {"02_*_workspace",
             "*挑战赛*", "魔搭部署_新版", "答辩交付",
             "release", "__pycache__", ".pytest_cache", ".playwright-cli", "versions"}
DOC_FILES = ("README.md", "GOTCHAS.md", "Phase3情景模拟设计稿_2026-09-29.md")
DATA_FILES = ("25辖区测试数据_方案A_118条台账.xlsx", "data/pillar_two.db")


def _skip(name: str) -> bool:
    """通配排除：支持 "02_*_workspace"、"*挑战赛*" 这类写法。"""
    import fnmatch
    return any(fnmatch.fnmatch(name, pattern)
               for pattern in SKIP_DIRS if "*" in pattern)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def collect() -> dict[str, list[Path]]:
    """按白名单收集：源码 / 规则库 / 测试 / 文档 / 演示数据。"""
    buckets: dict[str, list[Path]] = {"src": [], "rules": [], "tests": [],
                                      "docs": [], "data": []}
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT)
        if any(part in SKIP_DIRS or part.startswith(".") or "*" in part for part in rel.parts):
            continue
        if path.name.startswith("test_"):
            buckets["tests"].append(path)
        elif rel.parts[0] == "Agent":
            buckets["src"].append(path)
        else:
            buckets["src"].append(path)
    rules_dir = ROOT / "Agent" / "rules"
    if rules_dir.is_dir():
        buckets["rules"] = sorted(rules_dir.glob("*.json"))
    for name in DOC_FILES:
        candidate = ROOT / name
        if candidate.exists():
            buckets["docs"].append(candidate)
    for name in DATA_FILES:
        candidate = ROOT / name
        if candidate.exists():
            buckets["data"].append(candidate)
    return buckets


def main() -> int:
    parser = argparse.ArgumentParser(description="生成封版快照")
    parser.add_argument("--no-data", action="store_true", help="不带演示数据")
    parser.add_argument("--out", default="", help="输出目录（默认 release/<日期>）")
    args = parser.parse_args()

    stamp = datetime.datetime.now().strftime("%Y-%m-%d")
    target = Path(args.out) if args.out else ROOT / "release" / f"pillar_two_{stamp}"
    if target.exists():
        shutil.rmtree(target)
    buckets = collect()
    if args.no_data:
        buckets["data"] = []

    copied: list[tuple[str, str, int]] = []
    # 布局：**原样保留相对路径**放在 src/ 下（含 Agent/… 与 Agent/rules/…）。
    # 不能拍平：① Agent 是包，__init__.py 必须留在原位；② 规则库是相对模块路径读的
    # （validator/rule_version_store 用 Path(__file__).parent / "Agent" / "rules"），
    # 拍平后快照能拷出去但**跑不起来** —— 封版快照不能跑就没意义。
    for bucket, paths in buckets.items():
        for path in paths:
            rel = path.relative_to(ROOT)
            if bucket == "docs":
                dest = target / "docs" / rel.name
            elif bucket == "data":
                dest = target / "src" / rel
            else:                                  # src / rules / tests 保持原树
                dest = target / "src" / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)
            copied.append((str(dest.relative_to(target)), sha256(path),
                           path.stat().st_size))

    # 依赖清单（固定版本，保证可复现）
    requirements = ROOT / "requirements.txt"
    if not requirements.exists():
        print("⚠️ 未找到 requirements.txt，跳过依赖清单", file=sys.stderr)
    else:
        shutil.copy2(requirements, target / "requirements.txt")
        copied.append(("requirements.txt", sha256(requirements),
                       requirements.stat().st_size))

    lines = [
        "Pillar Two 全球最低税测算工具 · 封版快照",
        f"生成时间：{datetime.datetime.now():%Y-%m-%d %H:%M:%S}",
        f"源码根目录：{ROOT}",
        f"文件数：{len(copied)}",
        "",
        "说明：本目录是**自包含快照** —— src/ 下原样保留了源码树（含 Agent/ 包与",
        "      Agent/rules/ 规则库），docs/ 是文档，tests 已随 src/ 一起保留。",
        f"      跑起来：pip install -r requirements.txt，然后 streamlit run src/app.py",
        "      改代码前先读 docs/GOTCHAS.md（已知坑）。",
        "",
        "SHA256                                                           大小  路径",
        "-" * 100,
    ]
    for rel, digest, size in copied:
        lines.append(f"{digest}  {size:>8}  {rel}")
    (target / "MANIFEST.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    # 注意：Windows 控制台默认 GBK，输出里不要用 emoji（会 UnicodeEncodeError）
    total = sum(size for _rel, _digest, size in copied)
    print(f"[OK] 快照已生成：{target}")
    print(f"     文件 {len(copied)} 个，合计 {total/1024/1024:.1f} MB")
    for bucket, paths in buckets.items():
        print(f"     {bucket:<6} {len(paths)} 个")
    print("     校验：MANIFEST.txt 里每个文件都有 SHA256，可逐条核对")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
