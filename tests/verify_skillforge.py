#!/usr/bin/env python3
"""skillforge 自测：生成器、体检、CLI 契约。

运行：python tests/verify_skillforge.py
零依赖、不联网（除 selftest 内置的本地 fixture）。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent
CLI = ROOT / "skillforge.py"
PY = sys.executable

CHECKS = 0
FAILS: list[str] = []


def check(cond, name: str) -> None:
    global CHECKS
    CHECKS += 1
    if not cond:
        FAILS.append(name)


def run(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [PY, str(CLI), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(cwd or ROOT), env=env, timeout=180,
    )


DEMO_README = """# tinycalc

**A tiny CLI that turns CSV into JSON.**

It has no dependencies and reads stdin or a file.

## Install

```bash
pip install tinycalc
```

## Usage

```bash
tinycalc data.csv --pretty
```

Options: `--pretty` indents output, `--strict` fails on ragged rows.

## Notes

Linux, macOS and Windows are supported.
"""

DEMO_DOC = """# Design notes

tinycalc keeps a streaming parser: it never loads the whole file.

## Edge cases

Ragged rows are dropped unless `--strict`.
"""


def make_demo(d: Path) -> None:
    (d / "README.md").write_text(DEMO_README, encoding="utf-8")
    (d / "docs").mkdir(parents=True, exist_ok=True)
    (d / "docs" / "design.md").write_text(DEMO_DOC, encoding="utf-8")
    (d / "tinycalc").mkdir(exist_ok=True)
    (d / "tinycalc" / "__main__.py").write_text(
        "def main():\n    print('tinycalc')\n\n\nif __name__ == '__main__':\n    main()\n",
        encoding="utf-8",
    )
    (d / "requirements.txt").write_text("", encoding="utf-8")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="skillforge-test-"))
    try:
        # A. selftest 内置 fixture
        p = run("selftest")
        check(p.returncode == 0, "A1 selftest 退出码 0")
        check("通过" in p.stdout, "A2 selftest 报告通过")

        # B. 从本地目录生成
        demo = tmp / "demo"
        demo.mkdir()
        make_demo(demo)
        out = tmp / "out-demo"
        p = run("build", str(demo), "-o", str(out))
        check(p.returncode == 0, "B1 build 退出码 0")
        check((out / "SKILL.md").exists(), "B2 生成 SKILL.md")
        check((out / "meta.json").exists(), "B3 生成 meta.json")
        skill = (out / "SKILL.md").read_text(encoding="utf-8")
        check(skill.startswith("---\n"), "B4 有 frontmatter")
        check("name: tinycalc" in skill, "B5 name 来自标题")
        check("whenToUse:" in skill, "B6 有 whenToUse")
        check("tinycalc data.csv" in skill, "B7 有可执行命令")
        check("## 参考文件" in skill, "B8 有参考文件清单")
        refs_dir = out / "references"
        names = sorted(x.name for x in refs_dir.iterdir()) if refs_dir.exists() else []
        check("README.md" in names, "B9 README 进入 references")
        check(any("design" in n for n in names), "B10 文档进入 references")
        check(not any(n.startswith("references__") for n in names), "B11 不再出现 references__ 命名")
        check(len(skill) < 12000, "B12 正文没有失控膨胀")
        meta = json.loads((out / "meta.json").read_text(encoding="utf-8"))
        check(meta.get("generator") == "skillforge", "B13 meta.generator 正确")
        check(len(meta.get("references", [])) >= 2, "B14 meta 记录 references")
        check(any("build" in c for c in meta.get("commands", [])), "B15 meta 记录复现命令")

        # C. 确定性：同输入两次输出一致
        out2 = tmp / "out-demo-2"
        run("build", str(demo), "-o", str(out2))
        a = (out / "SKILL.md").read_text(encoding="utf-8")
        b = (out2 / "SKILL.md").read_text(encoding="utf-8")
        check(a == b, "C1 两次生成完全一致")

        # D. lint / score
        p = run("lint", str(out))
        check(p.returncode == 0, "D1 lint 通过退出码 0")
        check("100/100" in p.stdout, "D2 lint 打印满分")
        p = run("score", str(out), "--json")
        check(p.returncode == 0, "D3 score --json 退出码 0")
        try:
            audit = json.loads(p.stdout)
        except json.JSONDecodeError:
            audit = {}
        check(audit.get("score") == 100, "D4 score JSON 为 100")
        check(audit.get("ok") is True, "D5 score JSON ok=true")
        items = audit.get("items") or audit.get("checks") or []
        check(len(items) >= 8, "D6 score 列出逐项检查")

        # E. 坏输入：lint 一个不健康目录要报问题
        bad = tmp / "bad"
        bad.mkdir()
        (bad / "SKILL.md").write_text("# no frontmatter\n\nTODO\n", encoding="utf-8")
        p = run("lint", str(bad))
        check(p.returncode == 1, "E1 不健康目录 lint 退出码 1")
        check("通过" not in p.stdout or "不通过" in p.stdout, "E2 明确给出不通过")

        # F. 空目录：要生成但标注空洞
        empty = tmp / "empty"
        empty.mkdir()
        out_empty = tmp / "out-empty"
        p = run("build", str(empty), "-o", str(out_empty), "--name", "empty-demo")
        check(p.returncode in (0, 3), "F1 空目录不崩")
        check((out_empty / "SKILL.md").exists(), "F2 空目录也产出 SKILL.md")
        check("待补充" in (out_empty / "SKILL.md").read_text(encoding="utf-8"), "F3 空目录标注待补充")

        # G. 单文件源 + 显式 name
        src_md = tmp / "single.md"
        src_md.write_text(
            "# single\n\n**Turn X into Y in one command.**\n\n## Usage\n\n```bash\nx --y\n```\n",
            encoding="utf-8",
        )
        out1 = tmp / "out-single"
        p = run("build", str(src_md), "-o", str(out1), "--name", "single-demo")
        check(p.returncode in (0, 3), "G1 单文件 build 不崩")
        check("name: single-demo" in (out1 / "SKILL.md").read_text(encoding="utf-8"), "G2 --name 生效")

        # H. name 规范化（空格/中文 → kebab）
        out_slug = tmp / "out-slug"
        p = run("build", str(demo), "-o", str(out_slug), "--name", "My Skill Name")
        slug = (out_slug / "SKILL.md").read_text(encoding="utf-8")
        check("name: my-skill-name" in slug, "H1 name 规范化为 kebab")

        # I. --json 输出可解析
        p = run("build", str(demo), "-o", str(tmp / "out-json"), "--json")
        try:
            data = json.loads(p.stdout)
        except json.JSONDecodeError:
            data = {}
        check(data.get("meta", {}).get("name") == "tinycalc", "I1 build --json 输出 meta")

        # J. 拒绝不存在的路径
        p = run("build", str(tmp / "does-not-exist"))
        check(p.returncode != 0, "J1 不存在路径报错")

        # K. 文本里不残留 markdown 强调标记（description 质量）
        m = re.search(r"^description:\s*(.+)$", skill, re.M)
        desc = m.group(1) if m else ""
        check("**" not in desc, "K1 description 无 markdown 粗体残留")
        check(not desc.startswith("是："), "K2 description 无「是：」标签残留")
        check(len(desc) <= 480, "K3 description 不超长")
        # L. --profile claude：不写 whenToUse，触发词进 description
        out_claude = tmp / "out-claude"
        p = run("build", str(demo), "-o", str(out_claude), "--profile", "claude")
        check(p.returncode == 0, "L1 claude profile 生成成功")
        cskill = (out_claude / "SKILL.md").read_text(encoding="utf-8")
        check("whenToUse:" not in cskill, "L2 claude profile 不写 whenToUse")
        check("use when" in cskill.lower(), "L3 claude profile 触发词进 description")
        cmeta = json.loads((out_claude / "meta.json").read_text(encoding="utf-8"))
        check(cmeta.get("profile") == "claude", "L4 meta.profile 记录 claude")
        p = run("lint", str(out_claude))
        check(p.returncode == 0, "L5 claude profile 体检不因 whenToUse 掉分")

        # M. --dry-run：打预演、不落盘
        dry = tmp / "out-dry"
        p = run("build", str(demo), "-o", str(dry), "--dry-run")
        check(p.returncode == 0, "M1 dry-run 退出码 0")
        check("预演" in p.stdout, "M2 dry-run 打印预演")
        check(not dry.exists(), "M3 dry-run 不写盘")

        # N. diff：一致 → 0；改源 → 1 并列出改动
        p = run("diff", str(out))
        check(p.returncode == 0, "N1 diff 一致时退出码 0")
        check("一致" in p.stdout, "N2 diff 报告一致")
        demo_doc = demo / "docs" / "design.md"
        demo_doc.write_text(demo_doc.read_text(encoding="utf-8") + "\n<!-- touched -->\n", encoding="utf-8")
        p = run("diff", str(out))
        check(p.returncode == 1, "N3 源变化后 diff 退出码 1")
        check("修改" in p.stdout and "design.md" in p.stdout, "N4 diff 列出被改文件")
        (demo / "docs" / "new.md").write_text("# New\n\nA new doc.\n", encoding="utf-8")
        p = run("diff", str(out))
        check("新增" in p.stdout and "new.md" in p.stdout, "N5 diff 列出新增文件")
        p = run("diff", str(out), "--json")
        try:
            d = json.loads(p.stdout)
        except json.JSONDecodeError:
            d = {}
        check(d.get("stale") is True, "N6 diff --json 输出 stale=true")

        # O. fix：机械问题自动修
        bad = tmp / "bad-skill"
        (bad / "references").mkdir(parents=True)
        (bad / "references" / "extra.md").write_text("# Extra\n\ndetails\n", encoding="utf-8")
        (bad / "SKILL.md").write_text(
            "---\nname: Bad Skill\ndescription: stuff\n---\n\n# Bad Skill\n\n"
            + ("This widget tool does useful things. " * 12) + "\n",
            encoding="utf-8")
        p = run("lint", str(bad))
        check(p.returncode == 1, "O1 坏 skill 体检不通过")
        p = run("fix", str(bad))
        check(p.returncode != 0, "O2 未声明的 skill 默认拒绝修改（保护手写措辞）")
        (bad / "meta.json").write_text(json.dumps({"generator": "skillforge", "profile": "dsh"}),
                                       encoding="utf-8")
        p = run("fix", str(bad))
        check(p.returncode == 0, "O3 fix 后达到健康线")
        fixed = (bad / "SKILL.md").read_text(encoding="utf-8")
        check("name: bad-skill" in fixed, "O4 fix 规范化 name")
        check("whenToUse:" in fixed, "O5 fix 补 whenToUse")
        check("references/extra.md" in fixed, "O6 fix 补 references 链接")
        p = run("fix", str(bad))
        check(p.returncode == 0 and "无需修复" in p.stdout, "O7 二次 fix 幂等")

        # P. score --min 作为 CI 门禁
        p = run("score", str(out), "--min", "100")
        check(p.returncode == 0, "P1 score --min 达标退出 0")
        p = run("score", str(bad), "--min", "95")
        check(p.returncode == 1, "P2 score --min 不达标退出 1")

        # Q. meta 记录升级
        raw = json.loads((out / "meta.json").read_text(encoding="utf-8"))
        check(raw.get("version") == "0.2.0", "Q1 meta.version 为 0.2.0")
        check(raw.get("profile") == "dsh", "Q2 meta.profile 默认为 dsh")
        check(len(raw.get("source_files", [])) >= 3, "Q3 meta 记录源文件哈希")
        check(bool(raw.get("source_root")), "Q4 meta 记录 source_root")

        # R. 已存在的输出目录需要 --force
        p = run("build", str(demo), "-o", str(out))
        check(p.returncode != 0, "R1 非空输出目录默认拒绝覆盖")
        p = run("build", str(demo), "-o", str(out), "--force")
        check(p.returncode == 0, "R2 --force 允许覆盖")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"verify_skillforge: {CHECKS} 项检查")
    if FAILS:
        for f in FAILS:
            print("  ✗ " + f)
        print(f"失败 {len(FAILS)} 项")
        return 1
    print("全部通过 ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
