#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""skillforge — 把任意 repo / 文档 / PDF 变成可安装、可评分、可复现的 agent skill。

零依赖（纯标准库）。PDF 需要可选安装 pypdf。

用法:
  python skillforge.py build <source> -o out/      # repo 目录 / git URL / 文档文件 / 网页
  python skillforge.py lint <skill_dir> [--json]   # 体检已有 skill 包
  python skillforge.py score <skill_dir> [--json]  # 只打分
  python skillforge.py selftest                    # 内置自检

设计原则（和"生成一个能跑的东西"同样重要）:
  1. 确定性：同一输入 → 同样的 SKILL.md（不含时间戳），便于回归和审计。
  2. 可解释：每个分值/每行生成内容都能追溯到来源文件。
  3. 有预算：默认总预算 24000 字符，超出按优先级截断并记录日志。
  4. 不编造：只写从源材料抽取到的事实；缺什么就在"待补充"里列出。
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

try:  # 让 Windows 控制台也能输出中文/✓
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

VERSION = "0.1.0"
DEFAULT_BUDGET = 24000
MAX_REF_CHARS = 8000

TEXT_EXTS = {
    ".md", ".markdown", ".rst", ".txt", ".py", ".js", ".ts", ".mjs", ".cjs",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".sh", ".ps1", ".bat",
    ".html", ".htm", ".css", ".sql", ".go", ".rs", ".java", ".c", ".h", ".cpp",
    ".rb", ".php", ".lua", ".vue", ".jsx", ".tsx", ".csv", ".env.example",
}
DOC_EXTS = {".md", ".markdown", ".rst", ".txt"}
SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    "env", "dist", "build", "target", ".idea", ".vscode", ".dsh-vision-router",
    ".pytest_cache", ".mypy_cache", "coverage", "htmlcov", ".next", ".nuxt",
}
MANIFESTS = [
    "package.json", "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt",
    "Cargo.toml", "go.mod", "pom.xml", "build.gradle", "composer.json", "Gemfile",
    "SKILL.md", "skill.md",
]
ENTRY_HINTS = re.compile(
    r"^(main|cli|app|server|index|run|start|manage|__main__|bin|tool)\.(py|js|ts|mjs|sh|go|rs)$",
    re.I,
)
STOPWORDS = set("""
the and for with that this from are was were you your our their his her its it is be to of in on at as
by an or if then than so not no yes do does did done can could should would may might will just also
使用 我们 你们 他们 这个 那个 可以 需要 支持 通过 以及 一个 没有 什么 如果 因为 所以 已经 进行
""".split())
SKIP_SECTION_TITLES = re.compile(
    r"^\s*(目录|contents|table of contents|toc|许可|license|licence|版权|"
    r"赞助|sponsor|致谢|acknowledg\w*|更新日志|changelog|版本|release|"
    r"roadmap|规划|贡献|contribut\w*|star|trending)\s*$", re.I)
USEFUL_HEADING = re.compile(
    r"(安装|快速开始|开始|用法|使用|示例|上手|配置|参数|命令|api|接口|cli|"
    r"quick ?start|install|install(ing)?|usage|example|config|option|"
    r"faq|常见问题|注意|限制|坑|note|caution|caveat|troubleshoot)", re.I)
REQUIREMENT_LINE = re.compile(
    r"^\s*(\**\s*)?(要求|依赖|前置|前置条件|环境|requirements?|prerequisites?|"
    r"dependencies|install(ation)?|注意|警告|warn(ing)?)\b", re.I)


# ───────────────────────── 基础工具 ─────────────────────────

def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def slugify(name: str, max_len: int = 40) -> str:
    name = name.strip().lower()
    name = re.sub(r"\.(git|zip|tar\.gz)$", "", name)
    name = re.sub(r"^https?://", "", name)
    name = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "-", name)
    name = name.strip("-")
    if not name:
        name = "generated-skill"
    if not re.match(r"^[a-z0-9]", name):
        name = "skill-" + name
    return name[:max_len].strip("-")


def read_text(path: Path) -> str:
    raw = path.read_bytes()
    if b"\x00" in raw[:4096]:
        return ""
    for enc in ("utf-8-sig", "utf-8", "gb18030", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("utf-8", errors="replace")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def truncate(text: str, limit: int, marker: str = "\n\n…（此处截断，原文见 references/）") -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - len(marker))].rstrip() + marker


def one_line(text: str, limit: int = 300) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text[:limit].strip()


def strip_html(html: str) -> str:
    html = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?s)<br\s*/?>", "\n", html)
    html = re.sub(r"(?s)</(p|div|li|h[1-6]|tr|pre)>", "\n", html)
    html = re.sub(r"(?s)<[^>]+>", " ", html)
    html = html.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    html = re.sub(r"[ \t]+", " ", html)
    html = re.sub(r"\n\s*\n+", "\n\n", html)
    return html.strip()


def fetch_url(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (compatible; skillforge/0.1; +https://github.com/)",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
        ctype = r.headers.get("Content-Type", "")
    if "charset=" in ctype:
        enc = ctype.split("charset=")[-1].split(";")[0].strip()
    else:
        enc = "utf-8"
    try:
        return data.decode(enc, errors="replace")
    except LookupError:
        return data.decode("utf-8", errors="replace")


# ───────────────────────── 源加载 ─────────────────────────

class Source:
    def __init__(self, kind: str, label: str):
        self.kind = kind              # dir | git | file | url
        self.label = label
        self.files: dict[str, str] = {}   # rel_path -> 文本内容（只存文本文件，已按大小排序）
        self.readme = ""
        self.manifest = ""
        self.title = ""
        self.notes: list[str] = []
        self.root: Path | None = None

    def add_file(self, rel: str, text: str) -> None:
        if text and text.strip():
            self.files[rel] = text


def iter_text_files(root: Path, limit_files: int = 4000) -> list[Path]:
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".git"))
        for fn in sorted(filenames):
            p = Path(dirpath) / fn
            if p.suffix.lower() in TEXT_EXTS or fn.lower() in MANIFESTS or ENTRY_HINTS.match(fn):
                if p.stat().st_size <= 2 * 1024 * 1024:
                    out.append(p)
            if len(out) >= limit_files:
                return out
    return out


def load_local_dir(path: Path) -> Source:
    src = Source("dir", str(path))
    src.root = path
    for p in iter_text_files(path):
        rel = p.relative_to(path).as_posix()
        text = read_text(p)
        src.add_file(rel, text)
        low = p.name.lower()
        if not src.readme and low.startswith("readme"):
            src.readme = text
        if not src.manifest and low in [m.lower() for m in MANIFESTS]:
            src.manifest = text
    src.title = md_heading(src.readme) if src.readme else ""
    src.title = src.title or path.name
    if not src.readme:
        src.notes.append("未找到 README，简介由文件名/清单推断。")
    return src


def load_single_file(path: Path) -> Source:
    src = Source("file", str(path))
    src.root = path.parent
    text = read_text(path)
    if path.suffix.lower() == ".pdf":
        text = pdf_text(path)
    ext = path.suffix.lower()
    rel = path.name
    src.add_file(rel, text)
    if ext in DOC_EXTS or rel.lower().startswith("readme"):
        src.readme = text
    src.title = path.stem
    return src


def pdf_text(path: Path) -> str:
    try:
        import pypdf  # type: ignore
        reader = pypdf.PdfReader(str(path))
        return "\n\n".join((pg.extract_text() or "") for pg in reader.pages)
    except ImportError:
        raise SystemExit("读取 PDF 需要可选依赖：pip install pypdf（或先导出为 md/txt 再喂给 skillforge）")
    except Exception as e:  # 损坏/加密 PDF
        raise SystemExit(f"读取 PDF 失败：{e}")


def load_git(url: str) -> Source:
    tmp = Path(tempfile.mkdtemp(prefix="skillforge-"))
    r = None
    try:
        r = subprocess.run(
            ["git", "clone", "--depth", "1", "--single-branch", url, str(tmp / "repo")],
            capture_output=True, text=True, timeout=180,
        )
    except Exception as e:  # noqa: BLE001
        err = str(e)
    else:
        err = (r.stderr or "").strip() if r.returncode != 0 else ""
    if r is not None and r.returncode == 0 and (tmp / "repo").exists():
        src = load_local_dir(tmp / "repo")
        src.kind = "git"
        src.label = url
        src.title = Path(urllib.parse.urlparse(url).path).stem or src.title
        return src
    shutil.rmtree(tmp, ignore_errors=True)
    # 回退：直接抓 GitHub 页面里的 README
    src = Source("url", url)
    src.notes.append(f"git clone 失败（{one_line(err, 120) or '原因不明'}），回退为网页抽取。")
    try:
        html = fetch_url(url)
        text = strip_html(html)
        src.readme = text[:MAX_REF_CHARS * 2]
        src.title = Path(urllib.parse.urlparse(url).path).stem or "github-repo"
    except Exception as e:  # noqa: BLE001
        raise SystemExit(f"无法获取 {url}：git clone 失败且网页抓取失败（{e}）")
    return src


def load_url(url: str) -> Source:
    src = Source("url", url)
    html = fetch_url(url)
    text = strip_html(html) if "<html" in html.lower() or "<body" in html.lower() else html
    src.readme = text[:MAX_REF_CHARS * 2]
    src.title = Path(urllib.parse.urlparse(url).path).stem or urllib.parse.urlparse(url).netloc
    src.add_file("source.md", src.readme)
    return src


# ───────────────────────── 抽取启发式 ─────────────────────────

def md_heading(md: str, level: int = 1) -> str:
    hashes = "#" * level
    m = re.search(rf"(?m)^{hashes}\s+(.+)$", md)
    return m.group(1).strip() if m else ""


def is_toc_section(body: str) -> bool:
    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    if not lines:
        return False
    anchor_links = sum(1 for ln in lines if re.match(r"^[-*]\s*\[.+\]\(#.+\)$", ln))
    return anchor_links >= 4 or (anchor_links >= 2 and len(lines) <= 12)


def extract_summary(src: Source, limit: int = 600) -> str:
    """取 README 里最能代表项目的段落：优先「是什么/简介」节，否则第一段像样的正文。"""
    if not src.readme:
        return ""
    sections = extract_sections(src.readme)
    for title, body in sections:
        if re.search(r"(是什么|简介|介绍|what is|about|overview|description)", title, re.I):
            para = first_paragraph(body, limit)
            if para:
                return para
    for title, body in sections:
        if SKIP_SECTION_TITLES.match(title) or is_toc_section(body):
            continue
        for ln in body.splitlines():
            para = clean_paragraph(ln, limit)
            if para:
                return para
    # 兜底：README 最开头的正文行
    for ln in src.readme.splitlines():
        para = clean_paragraph(ln, limit)
        if para:
            return para
    return one_line(src.manifest[:limit] if src.manifest else "", limit)


def first_paragraph(body: str, limit: int) -> str:
    buf: list[str] = []
    for ln in body.splitlines():
        s = ln.strip()
        if not s:
            if buf:
                break
            continue
        if buf and (s.startswith(("#", "|", "-", "*", ">", "```"))):
            break
        buf.append(s)
        if sum(len(x) for x in buf) > limit:
            break
    text = " ".join(buf).strip()
    return clean_md(one_line(text, limit)) if len(text) >= 12 else ""


def clean_paragraph(line: str, limit: int) -> str:
    s = line.strip()
    if len(s) < 30:
        return ""
    if s.startswith(("#", "|", "-", "*", ">", "```", "!", "[!( ", "<")):
        return ""
    if REQUIREMENT_LINE.match(s):
        return ""
    if re.match(r"^\[.*\]\(.*\)$", s):
        return ""
    return clean_md(one_line(s, limit))


def clean_md(text: str) -> str:
    """去掉 markdown 强调/行内代码/链接外壳，只留给人读的纯文本。"""
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = text.replace("**", "").replace("__", "")
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"^\s*(是|这是|它是|本项目|这个项目|该项目)\s*[：:]\s*", "", text)
    text = re.sub(r"^\s*[：:；;，,\-—]+\s*", "", text)
    return text.strip()


def extract_code_blocks(md: str, max_blocks: int = 4) -> list[str]:
    blocks = re.findall(r"(?s)```[^\n]*\n(.*?)```", md)
    out = []
    for b in blocks:
        b = b.strip("\n")
        if not b.strip():
            continue
        # 跳过徽章/JSON 大块/纯目录树
        if b.lstrip().startswith(("{", "[", "<")):
            continue
        if sum(1 for ln in b.splitlines() if ln.strip().startswith(("├", "└", "│"))) > 3:
            continue
        out.append(b)
        if len(out) >= max_blocks:
            break
    return out


def extract_sections(md: str) -> list[tuple[str, str]]:
    """把 markdown 拆成 (二级标题, 正文) 列表。"""
    sections: list[tuple[str, str]] = []
    cur_title, buf = "", []
    for ln in md.splitlines():
        m = re.match(r"^##\s+(.+?)\s*$", ln)
        if m:
            if cur_title or buf:
                sections.append((cur_title, "\n".join(buf).strip()))
            cur_title, buf = m.group(1).strip(), []
        else:
            buf.append(ln)
    if cur_title or buf:
        sections.append((cur_title, "\n".join(buf).strip()))
    return [(t, b) for t, b in sections if b]


TECH_STOPWORDS = set("""
python pip git github bash shell zsh pwsh cmd cli api json yaml toml http https url uri
install usage example examples config option options param params args flag flags
readme license mit version quick start getting started true false none null
scripts script src main index app server client tool tools test tests docs doc file files
male female sex date time hour minute second year month day longitude latitude
references topics plain summary section sections field fields output outputs input inputs
content contents value values data item items entry entries example output sample samples
""".split())


CJK_STOP_PREFIX = ("一个", "这个", "那个", "以及", "可以", "需要", "通过", "使用", "其中",
                   "如果", "因为", "所以", "已经", "进行", "其他", "另外", "这里", "那里",
                   "我们", "你们", "他们", "什么", "没有", "就是", "也是", "不是", "而是",
                   "全部", "直接", "然后", "同时", "而且", "但是", "或者", "例如", "比如")


def keywords(text: str, top: int = 12, lang: str = "en") -> list[str]:
    # 只看正文，代码块里的标识符不算「用户会说的词」
    plain = re.sub(r"(?s)```.*?```", " ", text)
    words = re.findall(r"[A-Za-z][A-Za-z0-9+#._-]{2,30}", plain)
    cjk = re.findall(r"[\u4e00-\u9fff]{2,6}", plain)
    cnt: Counter = Counter()
    for w in words:
        lw = w.lower().strip("._-")
        if lw in STOPWORDS or lw in TECH_STOPWORDS or len(lw) < 3:
            continue
        cnt[w] += 1
    for w in cjk:
        if w in STOPWORDS or w.startswith(CJK_STOP_PREFIX):
            continue
        if w.endswith(("的", "了", "是", "把", "在", "和", "与", "就", "都", "也", "着", "过", "吧")):
            continue
        cnt[w] += 3 if lang == "zh" else 1
    return [w for w, _ in cnt.most_common(top)]


def detect_language(src: Source) -> str:
    if not src.readme:
        return "en"
    cjk = len(re.findall(r"[\u4e00-\u9fff]", src.readme))
    latin = len(re.findall(r"[A-Za-z]", src.readme))
    return "zh" if cjk * 3 >= latin else "en"


def guess_triggers(src: Source, kws: list[str]) -> list[str]:
    """给 whenToUse 生成可读的触发描述（只用抽取到的标题/关键词，不编造事实）。"""
    triggers: list[str] = []
    if src.readme:
        for title, body in extract_sections(src.readme):
            if SKIP_SECTION_TITLES.match(title) or is_toc_section(body):
                continue
            if not (USEFUL_HEADING.search(title)
                    or re.search(r"(是什么|简介|能力|功能|特性|feature)", title, re.I)):
                continue
            t = one_line(title, 40)
            if t and not any(t in x for x in triggers):
                triggers.append(f"用户问到「{t}」相关的用法时")
    for kw in kws[:4]:
        triggers.append(f"用户提到 `{kw}` 或需要解释它时")
    return triggers[:8]


def useful_sections(md: str, limit_sections: int = 6, total_chars: int = 3600) -> list[tuple[str, str]]:
    """从 README 里挑对「照着做」有用的章节：跳过目录/许可/更新日志/对比营销。"""
    picked: list[tuple[str, str]] = []
    used = 0
    ranked: list[tuple[int, str, str]] = []
    for title, body in extract_sections(md):
        if not title or SKIP_SECTION_TITLES.match(title) or is_toc_section(body):
            continue
        score = 0
        if USEFUL_HEADING.search(title):
            score = 2
        elif re.search(r"(能力|功能|特性|feature|概念|原理|how it works|结构)", title, re.I):
            score = 1
        if score == 0:
            continue
        ranked.append((score, title, body))
    ranked.sort(key=lambda x: (-x[0], len(x[2])))
    for _s, title, body in ranked:
        if len(picked) >= limit_sections or used >= total_chars:
            break
        room = min(1200, total_chars - used)
        if room < 120:
            break
        content = truncate(body, room)
        picked.append((one_line(title, 60), content))
        used += len(content)
    return picked


def pick_references(src: Source, budget: int) -> list[tuple[str, str]]:
    """挑选并截断参考文件：README 优先，其次 docs/*.md，再次清单与入口文件。"""
    picked: list[tuple[str, str]] = []
    used = 0
    cands: list[tuple[str, str, int]] = []   # (rel, text, priority 越小越先)

    def add(rel: str, text: str, prio: int) -> None:
        if text and text.strip():
            cands.append((rel, text, prio))

    if src.readme:
        add("README.md", src.readme, 0)
    if src.manifest:
        add("manifest.txt", src.manifest, 1)
    for rel, text in self_sorted_files(src):
        if rel.lower().startswith("references/") or rel.lower().endswith(("language-reference.md",)):
            add(rel, text, 0)
            continue
        if rel.endswith(".md") and (rel.lower().startswith("docs/") or rel.count("/") == 0):
            add(rel, text, 2)
        elif rel.endswith(".md"):
            add(rel, text, 3)
        elif rel.lower() in [m.lower() for m in MANIFESTS]:
            continue
        elif ENTRY_HINTS.match(Path(rel).name):
            add("entry-" + Path(rel).name.replace("/", "-"), text, 4)
        elif rel.endswith((".py", ".js", ".ts", ".go", ".rs", ".sh")):
            add("code-" + Path(rel).name.replace("/", "-"), text, 5)

    cands.sort(key=lambda x: (x[2], x[0]))
    seen_names: set[str] = set()
    for rel, text, _prio in cands:
        if used >= budget:
            break
        name = ref_name(rel)
        if name in seen_names:
            continue
        room = min(MAX_REF_CHARS, budget - used)
        if room < 400:
            break
        content = truncate(text, room)
        if not content.strip():
            continue
        seen_names.add(name)
        picked.append((name, content))
        used += len(content)
    return picked


def ref_name(rel: str) -> str:
    """把源文件路径压成 references/ 里的文件名：references/x.md → x.md，docs/y.md → docs-y.md。"""
    r = rel.replace("\\", "/")
    if r.lower().startswith("references/"):
        r = r[len("references/"):]
    r = r.replace("/", "-")
    return re.sub(r"[^A-Za-z0-9._-]+", "-", r) or "ref.md"


def self_sorted_files(src: Source) -> list[tuple[str, str]]:
    return sorted(src.files.items(), key=lambda kv: kv[0])


# ───────────────────────── 生成 SKILL.md ─────────────────────────

def build_frontmatter(name: str, description: str, when: list[str]) -> str:
    lines = ["---", f"name: {name}", f"description: {one_line(description, 480)}"]
    if when:
        lines.append("whenToUse: |")
        for w in when:
            lines.append("  - " + one_line(w, 200))
    lines.append("---")
    return "\n".join(lines)


def build_skill_md(src: Source, name: str, refs: list[tuple[str, str]], audit: dict) -> str:
    lang = detect_language(src)
    summary = extract_summary(src) or f"{src.title or name}（源材料：{src.label}）"
    headings = " ".join(t for t, _ in extract_sections(src.readme)) if src.readme else ""
    kws = keywords(summary + "\n" + headings, 14, lang)
    if len(kws) < 4:
        kws = keywords((src.readme or "") + "\n" + (src.manifest or ""), 14, lang)
    triggers = guess_triggers(src, kws)
    code = extract_code_blocks(src.readme, 3)
    core = useful_sections(src.readme)

    desc = summary
    if lang == "en":
        trig = ", ".join(f"`{k}`" for k in kws[:6])
        desc = (desc + f" Use when the user asks about {trig or 'this project'}.").strip()
    else:
        desc = (desc + "（触发词：" + "、".join(kws[:6]) + "）").strip() if kws else desc

    out: list[str] = []
    out.append(build_frontmatter(name, desc, triggers))
    out.append("")
    out.append(f"# {md_heading(src.readme) or src.title or name}")
    out.append("")
    out.append("> 本 skill 由 `skillforge` 从原始材料自动生成，内容均可追溯到 `references/`。")
    out.append("> 生成依据：" + ", ".join(f"`{r}`" for r, _ in refs[:6]) + (f" 等 {len(refs)} 份" if len(refs) > 6 else ""))
    out.append("")
    out.append("## 这是什么 / 什么时候用")
    out.append("")
    out.append(summary)
    if triggers:
        out.append("")
        for t in triggers[:6]:
            out.append(f"- {t}")
    if core:
        out.append("")
        out.append("## 核心内容")
        out.append("")
        for title, body in core:
            out.append(f"### {title}")
            out.append("")
            out.append(body)
            out.append("")
    elif code:
        out.append("")
        out.append("## 核心内容")
        out.append("")

    if code:
        out.append("## 快速开始（摘自 README，原样保留）")
        out.append("")
        for i, b in enumerate(code, 1):
            out.append(f"```")
            out.append(truncate(b, 1400))
            out.append("```")
            out.append("")
        out.append("先照抄上面的命令跑通一次，再按下面的约定改。")
        out.append("")

    if kws:
        out.append("## 关键概念")
        out.append("")
        out.append("材料里高频出现、需要理解的词（按出现次数）：")
        out.append("")
        out.append("、".join(f"`{k}`" for k in kws[:12]))
        out.append("")

    if refs:
        out.append("## 参考文件")
        out.append("")
        for rel, content in refs:
            hint = first_heading(content) or one_line(content, 80)
            out.append(f"- `references/{rel}` — {hint}")
        out.append("")
        out.append("读哪份：先读 `README.md`；需要细节时按上面提示跳转；不要一次性全读，按问题取用。")
        out.append("")

    gaps = audit.get("gaps") or []
    out.append("## 待补充 / 已知限制")
    out.append("")
    if gaps:
        for g in gaps:
            out.append(f"- {g}")
    else:
        out.append("- 生成材料里没有明显的空洞；若实际使用发现缺项，请回到源材料补 `references/`。")
    out.append("- 生成器不会替源材料「补全」内容：没有写到的事实不会出现在本 skill 里。")
    out.append("")
    out.append("## 维护")
    out.append("")
    out.append("源材料更新后重跑一次生成即可，命令记录在 `meta.json` 的 `commands` 字段；")
    out.append("改动请先跑 `python skillforge.py lint <本目录>` 看是否会掉分。")
    out.append("")
    return "\n".join(out).rstrip() + "\n"


def first_heading(md: str) -> str:
    for ln in md.splitlines():
        s = ln.strip()
        if s.startswith("#"):
            return one_line(s.lstrip("#").strip(), 70)
    return ""


# ───────────────────────── 评分 / 体检 ─────────────────────────

def parse_frontmatter(text: str) -> dict:
    m = re.match(r"(?s)^---\n(.*?)\n---\s*\n", text)
    if not m:
        return {}
    body = m.group(1)
    fm: dict = {"_raw": body}
    lines = body.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line or line.startswith(" "):
            continue
        k, v = line.split(":", 1)
        key = k.strip()
        val = v.strip()
        if val in ("|", ">", "|-", ">-", ""):
            # 块标量：收集后续缩进行
            block: list[str] = []
            while i < len(lines) and (lines[i].startswith(("  ", "\t")) or not lines[i].strip()):
                block.append(lines[i].strip())
                i += 1
            if val.startswith(">"):
                fm[key] = " ".join(x for x in block if x)
            else:
                fm[key] = "\n".join(x for x in block if x)
        else:
            fm[key] = val.strip('"').strip("'")
    return fm


def count_words(text: str) -> int:
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    words = len(re.findall(r"[A-Za-z0-9_]+", text))
    return cjk + words


def score_bundle(skill_dir: Path) -> dict:
    skill_md = skill_dir / "SKILL.md"
    issues: list[str] = []
    items: list[dict] = []

    if not skill_md.exists():
        return {"ok": False, "score": 0, "grade": "无效", "issues": ["缺少 SKILL.md"], "items": []}

    text = read_text(skill_md)
    fm = parse_frontmatter(text)
    body = re.sub(r"(?s)^---\n.*?\n---\s*\n", "", text)
    refs_dir = skill_dir / "references"
    ref_files = sorted(p for p in refs_dir.rglob("*") if p.is_file()) if refs_dir.exists() else []
    words = count_words(body)

    def add(id_: str, weight: int, ok: bool, hint: str, detail: str = "") -> None:
        items.append({"id": id_, "weight": weight, "ok": bool(ok),
                      "points": weight if ok else 0, "hint": "" if ok else hint,
                      "detail": detail})
        if not ok:
            issues.append(f"{id_}: {hint}")

    name = (fm.get("name") or "").strip()
    desc = (fm.get("description") or "").strip()
    when = fm.get("whenToUse") or ""

    add("name.kebab", 10, bool(re.match(r"^[a-z0-9][a-z0-9-]{1,62}$", name)),
        "frontmatter.name 必须是 kebab-case（小写字母/数字/连字符，2–63 字符）")
    add("description.exists", 12, 12 <= len(desc) <= 1024,
        "description 需 12–1024 字符（给路由器判断何时使用）", f"{len(desc)} 字符")
    add("description.has_trigger", 10,
        any(w in desc.lower() for w in ("use when", "when the user", "当用户", "用于", "触发"))
        or (len(when.strip()) >= 20 and len(desc) >= 80),
        "description 里没有触发条件（建议含 “Use when …” 或 “当用户…时”，或补 whenToUse）")
    add("whenToUse.exists", 10, bool(when.strip()), "缺少 whenToUse（多条触发场景更好）")
    add("body.min", 10, words >= 120, f"正文只有 {words} 词，太薄（建议 ≥120 词/字）")
    add("body.max", 6, words <= 6000, f"正文 {words} 词，超出建议上限 6000（细节拆进 references）")
    add("body.todo", 8, not re.search(r"\bTODO\b", text.upper()),
        "正文里有 TODO 占位，安装方会以为没写完")
    add("refs.exists", 10, bool(ref_files), "没有 references/，细节无处安放（生成器会把 README 放进去）")
    add("refs.linked", 6,
        bool(ref_files) and all(p.name in text or str(p.relative_to(skill_dir)).replace("\\", "/") in text
                                for p in ref_files),
        "有 references/ 文件没在正文里被引用（装了也不知道何时读）")
    dup_ratio = 0.0
    lines = [ln.strip() for ln in body.splitlines() if len(ln.strip()) > 20]
    if lines:
        dup_ratio = 1 - len(set(lines)) / len(lines)
    add("dedupe", 6, dup_ratio <= 0.35, f"正文重复行占 {dup_ratio:.0%}，疑似拼接重复")
    add("no_placeholder", 6, "{{" not in text and "todo" not in body.lower()[:200],
        "疑似模板占位符（{{…}}）")
    add("headings", 6, len(re.findall(r"(?m)^#{1,3}\s", body)) >= 4,
        "标题层级少于 4 个，扫描性差")

    total_w = sum(i["weight"] for i in items)
    got = sum(i["points"] for i in items)
    score = round(100.0 * got / total_w) if total_w else 0
    grade = "A" if score >= 90 else "B" if score >= 75 else "C" if score >= 60 else "D"
    return {"ok": score >= 75, "score": score, "grade": grade, "issues": issues,
            "items": items, "words": words, "refs": len(ref_files)}


def render_audit(audit: dict) -> str:
    lines = []
    for it in audit["items"]:
        mark = "✓" if it["ok"] else "✗"
        lines.append(f"  {mark} {it['id']:<24} {it['points']:>2}/{it['weight']:>2}  {it['detail']}")
    return "\n".join(lines)


# ───────────────────────── 命令 ─────────────────────────

def cmd_build(args) -> int:
    src = load_source(args.source, args)
    budget = args.budget
    name = slugify(args.name) if args.name else slugify(src.title or src.label)
    refs = pick_references(src, budget)
    gaps: list[str] = list(src.notes)
    if not src.readme:
        gaps.append("源材料没有 README/说明文档，概述只能从文件名与清单推断。")
    if len(refs) <= 1:
        gaps.append("可抽取的文档很少，建议把长文档放在 references/ 后重跑。")
    audit = {"gaps": gaps}
    skill_md = build_skill_md(src, name, refs, audit)

    out = Path(args.output) if args.output else Path(name)
    if out.exists() and args.force:
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "SKILL.md").write_text(skill_md, encoding="utf-8")
    if refs:
        (out / "references").mkdir(exist_ok=True)
        for rel, content in refs:
            (out / "references" / rel).write_text(content, encoding="utf-8")
    meta = {
        "version": VERSION,
        "generator": "skillforge",
        "source": src.label,
        "source_kind": src.kind,
        "name": name,
        "inputs": len(src.files),
        "references": [{"path": "references/" + r, "chars": len(c), "sha256": sha256_text(c)}
                       for r, c in refs],
        "budget": budget,
        "chars_total": sum(len(c) for _, c in refs),
        "gaps": gaps,
        "commands": [f"python skillforge.py build {args.source} -o {out.as_posix()}"],
    }
    (out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    audit = score_bundle(out)
    if args.json:
        print(json.dumps({"out": str(out), "meta": meta, "audit": audit}, ensure_ascii=False, indent=2))
        return 0 if audit["ok"] else 3
    print(f"已生成：{out}")
    print(f"  SKILL.md  {len(skill_md):>7} 字符")
    print(f"  references {len(refs):>6} 份 / {meta['chars_total']} 字符")
    print(f"  来源材料 {len(src.files):>6} 个文本文件（{src.kind}）")
    print(f"  体检：{audit['score']}/100（{audit['grade']}） 正文 {audit.get('words', 0)} 词")
    if audit["issues"]:
        print("  待修：")
        for i in audit["issues"][:8]:
            print("   - " + i)
    return 0 if audit["ok"] else 3


def load_source(source: str, args) -> Source:
    p = Path(source)
    if p.exists():
        return load_local_dir(p) if p.is_dir() else load_single_file(p)
    low = source.lower()
    if low.startswith(("http://", "https://", "git@")):
        if low.startswith("git@") or low.endswith(".git") or "github.com" in low:
            return load_git(source)
        return load_url(source)
    raise SystemExit(f"找不到源：{source}（既不是本地路径，也不是支持的网络地址）")


def cmd_lint(args) -> int:
    d = Path(args.skill_dir)
    if not d.exists():
        raise SystemExit(f"目录不存在：{d}")
    audit = score_bundle(d)
    text = read_text(d / "SKILL.md") if (d / "SKILL.md").exists() else ""
    fm = parse_frontmatter(text)
    if args.json:
        audit = dict(audit)
        audit["name"] = fm.get("name", "")
        audit["description"] = one_line(fm.get("description", ""), 480)
        print(json.dumps(audit, ensure_ascii=False, indent=2))
        return 0 if audit["ok"] else 1
    print(f"{d}")
    print(f"  名称：{fm.get('name', '（缺失）')}")
    print(f"  description：{one_line(fm.get('description', '（缺失）'), 160)}")
    print(f"  体检：{audit['score']}/100（{audit['grade']}）")
    print(render_audit(audit))
    if audit["issues"]:
        print("  结论：不通过（≥75 才算健康）")
    else:
        print("  结论：通过")
    return 0 if audit["ok"] else 1


def cmd_selftest(args) -> int:
    tmp = Path(tempfile.mkdtemp(prefix="skillforge-selftest-"))
    # 造一个最小 fixture repo
    repo = tmp / "demo-repo"
    (repo / "docs").mkdir(parents=True)
    (repo / "README.md").write_text(
        "# demo-tool\n\nA tiny CLI that converts CSV to JSON.\n\n"
        "## Install\n\n```bash\npip install demo-tool\n```\n\n"
        "## Usage\n\n```bash\ndemo input.csv -o out.json\n```\n\n"
        "## Notes\n\nHandles UTF-8 BOM; large files are streamed.\n", encoding="utf-8")
    (repo / "docs" / "advanced.md").write_text("## Advanced\n\nUse `--stream` for >1GB.\n", encoding="utf-8")
    (repo / "pyproject.toml").write_text("[project]\nname='demo-tool'\n", encoding="utf-8")
    (repo / "main.py").write_text("def main():\n    pass\n", encoding="utf-8")

    src = load_local_dir(repo)
    refs = pick_references(src, DEFAULT_BUDGET)
    skill = build_skill_md(src, "demo-tool", refs, {"gaps": []})
    checks: list[tuple[str, bool]] = [
        ("读到 README", "A tiny CLI" in src.readme),
        ("抓到 docs", any("advanced" in r for r, _ in refs)),
        ("frontmatter name", "name: demo-tool" in skill),
        ("有 whenToUse", "whenToUse:" in skill),
        ("含快速开始", "快速开始" in skill),
        ("含参考文件清单", "references/README.md" in skill),
        ("README 进入 references", any(r == "README.md" for r, _ in refs)),
        ("确定性（同输入同输出）", skill == build_skill_md(load_local_dir(repo), "demo-tool", refs, {"gaps": []})),
    ]
    out = tmp / "demo-tool"
    out.mkdir()
    (out / "SKILL.md").write_text(skill, encoding="utf-8")
    (out / "references").mkdir()
    for rel, content in refs:
        (out / "references" / rel).write_text(content, encoding="utf-8")
    audit = score_bundle(out)
    checks += [
        ("体检 ≥75", audit["score"] >= 75),
        ("体检识别 refs", audit.get("refs", 0) >= 2),
        ("无 name 报错", not any("name.kebab" in i for i in audit["issues"])),
    ]
    shutil.rmtree(tmp, ignore_errors=True)
    bad = [n for n, ok in checks if not ok]
    for n, ok in checks:
        print(("  ✓ " if ok else "  ✗ ") + n)
    print(f"selftest: {'通过' if not bad else '失败'}（{len(checks) - len(bad)}/{len(checks)}）")
    return 0 if not bad else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="skillforge",
        description="把任意 repo / 文档 / PDF 变成可安装、可评分、可复现的 agent skill（零依赖）。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="例子：\n"
               "  python skillforge.py build ./my-repo -o out/my-skill\n"
               "  python skillforge.py build https://github.com/user/repo -o out/repo-skill\n"
               "  python skillforge.py build paper.pdf -o out/paper-skill\n"
               "  python skillforge.py lint out/my-skill\n",
    )
    ap.add_argument("--version", action="version", version=f"skillforge {VERSION}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="从源材料生成 skill 包")
    b.add_argument("source", help="本地目录/文件、git URL、网页 URL 或 PDF")
    b.add_argument("-o", "--output", help="输出目录（默认 ./<name>）")
    b.add_argument("--name", help="skill 名（默认从源名推断，kebab-case）")
    b.add_argument("--budget", type=int, default=DEFAULT_BUDGET, help=f"references 总字符预算（默认 {DEFAULT_BUDGET}）")
    b.add_argument("--force", action="store_true", help="输出目录已存在时覆盖")
    b.add_argument("--json", action="store_true", help="额外输出机器可读 JSON")

    l = sub.add_parser("lint", help="体检一个已有 skill 包")
    l.add_argument("skill_dir")
    l.add_argument("--json", action="store_true")

    s = sub.add_parser("score", help="只打分")
    s.add_argument("skill_dir")
    s.add_argument("--json", action="store_true")

    sub.add_parser("selftest", help="运行内置自检")

    args = ap.parse_args(argv)
    if args.cmd == "build":
        return cmd_build(args)
    if args.cmd == "lint":
        return cmd_lint(args)
    if args.cmd == "score":
        a = score_bundle(Path(args.skill_dir))
        if args.json:
            print(json.dumps(a, ensure_ascii=False, indent=2))
        else:
            print(f"{a['score']}/100（{a['grade']}）")
        return 0
    if args.cmd == "selftest":
        return cmd_selftest(args)
    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
