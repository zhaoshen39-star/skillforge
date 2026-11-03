#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""skillforge — 把任意 repo / 文档 / PDF 变成可安装、可评分、可复现的 agent skill。

零依赖（纯标准库）。PDF 需要可选安装 pypdf。

用法:
  python skillforge.py build <source> -o out/ [--profile dsh|claude|generic] [--dry-run]
                                               # repo 目录 / git URL / 文档文件 / 网页
  python skillforge.py lint <skill_dir> [--json]      # 体检已有 skill 包
  python skillforge.py fix  <skill_dir> [--force]     # 自动修机械问题（不改内容）
  python skillforge.py diff <skill_dir> [--json]      # 源材料变了没有（0 一致 / 1 过期）
  python skillforge.py score <skill_dir> [--min N]    # 只打分，可做 CI 门禁
  python skillforge.py selftest                       # 内置自检

设计原则（和"生成一个能跑的东西"同样重要）:
  1. 确定性：同一输入 → 同样的 SKILL.md（不含时间戳），便于回归和审计。
  2. 可解释：每个分值/每行生成内容都能追溯到来源文件。
  3. 有预算：默认总预算 24000 字符，超出按优先级截断并记录日志。
  4. 不编造：只写从源材料抽取到的事实；缺什么就在"待补充"里列出。
  5. 可维护：meta.json 记录源文件哈希，diff 能发现 skill 已落后于源。
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import ssl
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

VERSION = "0.3.0"

# skill 运行时的 frontmatter 习惯不同：
#   dsh     — name / description / whenToUse（DSH 会读 whenToUse 做路由）
#   claude  — 只读 name / description，触发条件必须塞进 description
#   generic — 通用：保留 whenToUse，但 description 里也带触发词
PROFILES = ("dsh", "claude", "generic")
DEFAULT_BUDGET = 24000
MAX_REF_CHARS = 8000

TEXT_EXTS = {
    ".md", ".markdown", ".rst", ".txt", ".py", ".js", ".ts", ".mjs", ".cjs",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".sh", ".ps1", ".bat",
    ".html", ".htm", ".css", ".sql", ".go", ".rs", ".java", ".c", ".h", ".cpp",
    ".rb", ".php", ".lua", ".vue", ".jsx", ".tsx", ".csv", ".env.example",
}
DOC_EXTS = {".md", ".markdown", ".rst", ".txt"}
# 没有扩展名、但通常是高价值文本的裸文件名
TEXT_NAMES = {
    "license", "licence", "notice", "authors", "changelog", "contributing",
    "makefile", "dockerfile", "procfile", "gemfile", "rakefile",
    ".gitignore", ".dockerignore", ".editorconfig", ".npmrc", ".nvmrc",
}
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
turns turn into reads read uses using based allows allow includes include provides provide supports
support ensures ensure helps help offers offer designed built simple easy tiny small fast better best
new free open various many more most other some such only even like well need needs want wants get
gets got make makes made take takes given give gives set sets run runs work works feature features
dependency dependencies option options available
""".split())
SKIP_SECTION_TITLES = re.compile(
    r"^\s*(目录|contents|table of contents|toc|许可|license|licence|版权|"
    r"赞助|sponsor|致谢|acknowledg\w*|更新日志|changelog|版本|release|"
    r"roadmap|规划|贡献|contribut\w*|star|trending|donate|donation|捐赠|打赏)\s*$", re.I)
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
    """抓网页。部分 Windows Python 缺 CA 根证书，此时回退到「跳过校验」并放行一次。"""
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (compatible; skillforge/0.2; +https://github.com/)",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    })
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.URLError as e:
        if "CERTIFICATE_VERIFY_FAILED" not in str(e):
            raise
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        print("提示：本机 Python 缺 CA 根证书，本次抓取已跳过证书校验。", file=sys.stderr)
        r = urllib.request.urlopen(req, timeout=timeout, context=ctx)
    with r:
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


def _is_generated_bundle(p: Path) -> bool:
    """目录里若有 skillforge 生成的 meta.json，则整棵子树都不是源材料（避免自我吞噬）。"""
    m = p / "meta.json"
    if not m.is_file():
        return False
    try:
        return json.loads(read_text(m)).get("generator") == "skillforge"
    except Exception:  # noqa: BLE001
        return False


def iter_text_files(root: Path, limit_files: int = 4000) -> list[Path]:
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in SKIP_DIRS and not _is_generated_bundle(Path(dirpath) / d)
        )
        for fn in sorted(filenames):
            p = Path(dirpath) / fn
            if (p.suffix.lower() in TEXT_EXTS or fn.lower() in MANIFESTS
                    or fn.lower() in TEXT_NAMES or ENTRY_HINTS.match(fn)):
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
        para = first_paragraph(body, limit)
        if para:
            return para
    # 兜底：README 里第一段像样的正文（跳过标题/HTML/代码块）
    para = first_body_paragraph(src.readme, limit)
    if para:
        return para
    return one_line(src.manifest[:limit] if src.manifest else "", limit)


def first_body_paragraph(md: str, limit: int) -> str:
    """在整篇 markdown 里找第一段正文：跳过标题、HTML 块、徽章、代码块、表格。"""
    buf: list[str] = []
    in_fence = False
    for ln in md.splitlines():
        s = ln.strip()
        if s.startswith("```"):
            in_fence = not in_fence
            if buf:
                break
            continue
        if in_fence:
            continue
        if not s:
            if buf:
                break
            continue
        if BLOCK_START.match(s):
            if buf:
                break
            continue
        buf.append(s)
        if sum(len(x) for x in buf) > limit:
            break
    text = clean_md(" ".join(buf).strip())
    return one_line(text, limit) if len(text) >= 30 else ""


def first_paragraph(body: str, limit: int) -> str:
    buf: list[str] = []
    in_fence = False
    for ln in body.splitlines():
        s = ln.strip()
        if s.startswith("```"):
            in_fence = not in_fence
            if buf:
                break
            continue
        if in_fence:
            continue
        if not s:
            if buf:
                break
            continue
        if BLOCK_START.match(s):
            if buf:
                break
            continue
        buf.append(s)
        if sum(len(x) for x in buf) > limit:
            break
    text = " ".join(buf).strip()
    return clean_md(one_line(text, limit)) if len(text) >= 12 else ""


BLOCK_START = re.compile(r"^(#{1,6}\s|\||-\s|\*\s|\d+\.\s|```|!\[|\[[!\[]|<|>|\\)")
CODEY = re.compile(
    r"(^|\s)(cat|curl|wget|git|pip|pip3|npm|yarn|pnpm|docker|cd|ls|rm|python|python3|bash|sh|node|go|cargo|make|sudo|echo|export)\b"
    r"|\|\s*[a-z][\w.-]*\s|--[a-z][\w-]+")


def clean_paragraph(line: str, limit: int) -> str:
    s = line.strip()
    if len(s) < 30:
        return ""
    if BLOCK_START.match(s):
        return ""
    if REQUIREMENT_LINE.match(s):
        return ""
    if re.match(r"^\[.*\]\(.*\)$", s):
        return ""
    if CODEY.search(s):
        return ""
    return clean_md(one_line(s, limit))


def clean_md(text: str) -> str:
    """去掉 markdown 强调/行内代码/链接外壳，只留给人读的纯文本。"""
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = text.replace("**", "").replace("__", "")
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"<[^>]{1,400}>", "", text)  # 裸露 HTML / 徽章 / div 包装
    text = re.sub(r"\s{2,}", " ", text)
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
use using used uses without needing need needs avoid relying purposes permission service
services testing monitoring documentation
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
    display: dict[str, str] = {}
    for w in words:
        lw = w.lower().strip("._-")
        if lw in STOPWORDS or lw in TECH_STOPWORDS or len(lw) < 3:
            continue
        cnt[lw] += 1
        # 同一词的大小写形态合并，优先保留首字母大写的写法（专名/类型名）
        if lw not in display or (w[:1].isupper() and not display[lw][:1].isupper()):
            display[lw] = w.strip("._-")
    for w in cjk:
        if w in STOPWORDS or w.startswith(CJK_STOP_PREFIX):
            continue
        if w.endswith(("的", "了", "是", "把", "在", "和", "与", "就", "都", "也", "着", "过", "吧")):
            continue
        cnt[w] += 3 if lang == "zh" else 1
        display.setdefault(w, w)
    return [display.get(w, w) for w, _ in cnt.most_common(top)]


def detect_language(src: Source) -> str:
    if not src.readme:
        return "en"
    cjk = len(re.findall(r"[\u4e00-\u9fff]", src.readme))
    latin = len(re.findall(r"[A-Za-z]", src.readme))
    return "zh" if cjk * 3 >= latin else "en"


def guess_triggers(src: Source, kws: list[str], lang: str = "zh") -> list[str]:
    """给 whenToUse 生成可读的触发描述（只用抽取到的标题/关键词，不编造事实）。"""
    triggers: list[str] = []
    zh = lang == "zh"
    if src.readme:
        for title, body in extract_sections(src.readme):
            if not title or SKIP_SECTION_TITLES.match(title) or is_toc_section(body):
                continue
            if not (USEFUL_HEADING.search(title)
                    or re.search(r"(是什么|简介|能力|功能|特性|feature)", title, re.I)):
                continue
            t = one_line(title, 40)
            if t and not any(t in x for x in triggers):
                triggers.append(f"用户问到「{t}」相关的用法时" if zh else f'When the user asks about "{t}".')
    for kw in kws[:4]:
        triggers.append(f"用户提到 `{kw}` 或需要解释它时" if zh else f"When the user mentions `{kw}` and needs it explained.")
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

def build_frontmatter(name: str, description: str, when: list[str], profile: str = "dsh") -> str:
    """按目标运行时的习惯生成 frontmatter。

    claude 只认 name + description，所以把触发条件折叠进 description；
    其余 profile 额外挂 whenToUse 块。
    """
    desc = one_line(description, 480)
    if profile == "claude" and when:
        folded = " Use when the user: " + "; ".join(
            one_line(w, 120).lstrip("- ").rstrip("。.;；") for w in when[:3]) + "."
        if len(desc) + len(folded) <= 1000:
            desc = desc + folded
    lines = ["---", f"name: {name}", f"description: {desc}"]
    if profile != "claude" and when:
        lines.append("whenToUse: |")
        for w in when:
            lines.append("  - " + one_line(w, 200))
    lines.append("---")
    return "\n".join(lines)


def build_skill_md(src: Source, name: str, refs: list[tuple[str, str]], audit: dict,
                   profile: str = "dsh") -> str:
    lang = detect_language(src)
    summary = extract_summary(src) or f"{src.title or name}（源材料：{src.label}）"
    headings = " ".join(
        t for t, body in extract_sections(src.readme)
        if t and not SKIP_SECTION_TITLES.match(t) and not is_toc_section(body)
    ) if src.readme else ""
    kws = keywords(summary + "\n" + (headings + "\n") * 3, 14, lang)
    if len(kws) < 4:
        kws = keywords((src.readme or "") + "\n" + (src.manifest or ""), 14, lang)
    triggers = guess_triggers(src, kws, lang)
    code = extract_code_blocks(src.readme, 3)
    core = useful_sections(src.readme)

    desc = summary
    trig_words = ", ".join(f"`{k}`" for k in kws[:6])
    if profile == "claude":
        # 只读 name+description 的运行时：触发词必须写进 description
        if lang == "en":
            desc = (desc + f" Use when the user asks about {trig_words or 'this project'}.").strip()
        elif kws:
            desc = (desc.rstrip("。.") + "。当用户问到「" + "」「".join(kws[:6]) + "」等主题时使用。").strip()
    elif lang == "en":
        desc = (desc + f" Use when the user asks about {trig_words or 'this project'}.").strip()
    else:
        desc = (desc + "（触发词：" + "、".join(kws[:6]) + "）").strip() if kws else desc

    out: list[str] = []
    out.append(build_frontmatter(name, desc, [] if profile == "claude" else triggers, profile))
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
    """字数当量：英文按词，中文按 2.2 字符≈1 词折算，避免中文正文被误判超长。"""
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    words = len(re.findall(r"[A-Za-z0-9_]+", text))
    return int(round(cjk / 2.2 + words))


def score_bundle(skill_dir: Path) -> dict:
    skill_md = skill_dir / "SKILL.md"
    issues: list[str] = []
    items: list[dict] = []

    if not skill_md.exists():
        return {"ok": False, "score": 0, "grade": "无效", "issues": ["缺少 SKILL.md"], "items": []}

    text = read_text(skill_md)
    fm = parse_frontmatter(text)
    body = re.sub(r"(?s)^---\n.*?\n---\s*\n", "", text)
    meta_path = skill_dir / "meta.json"
    profile = "dsh"
    if meta_path.exists():
        try:
            profile = (json.loads(read_text(meta_path)).get("profile") or "dsh")
        except ValueError:
            profile = "dsh"
    refs_dir = skill_dir / "references"
    ref_files = sorted(p for p in refs_dir.rglob("*") if p.is_file()) if refs_dir.exists() else []
    words = count_words(body)

    def add(id_: str, weight: int, ok: bool, hint: str, detail: str = "", points: int | None = None) -> None:
        if points is None:
            points = weight if ok else 0
        points = max(0, min(weight, int(points)))
        items.append({"id": id_, "weight": weight, "ok": bool(ok),
                      "points": points, "hint": "" if ok else hint,
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
    when_ok = bool(when.strip()) or (
        profile == "claude"
        and any(w in desc.lower() for w in ("use when", "when the user", "当用户"))
    )
    add("whenToUse.exists", 10, when_ok,
        "缺少 whenToUse（多条触发场景更好；claude profile 可把触发词写进 description）")
    add("body.min", 10, words >= 120, f"正文只有 {words} 词，太薄（建议 ≥120 词/字）")
    add("body.max", 6, words <= 6000, f"正文 {words} 词，超出建议上限 6000（细节拆进 references）")
    add("body.todo", 8, not re.search(r"\bTODO\b", text.upper()),
        "正文里有 TODO 占位，安装方会以为没写完")
    add("refs.exists", 10, bool(ref_files), "没有 references/，细节无处安放（生成器会把 README 放进去）")
    linked = [p for p in ref_files
              if p.name in text or str(p.relative_to(skill_dir)).replace("\\", "/") in text]
    linked_points = round(6 * len(linked) / len(ref_files)) if ref_files else 0
    add("refs.linked", 6,
        bool(ref_files) and linked_points >= 4,
        f"有 {len(ref_files) - len(linked)}/{len(ref_files)} 份 references 没在正文里被引用（装了也不知道何时读）",
        f"{len(linked)}/{len(ref_files)}",
        points=linked_points)
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

def _write_bundle(out: Path, skill_md: str, refs: list[tuple[str, str]], meta: dict) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "SKILL.md").write_text(skill_md, encoding="utf-8")
    if refs:
        (out / "references").mkdir(exist_ok=True)
        for rel, content in refs:
            (out / "references" / rel).write_text(content, encoding="utf-8")
    (out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
                                   encoding="utf-8")


SKILL_TARGETS = ("auto", "dsh", "claude")


def skill_root(target: str = "auto") -> tuple[str, Path]:
    """返回 (实际目标名, skills 根目录)。

    dsh   → $DSH_HOME/skills（未设 DSH_HOME 时 ~/.dsh/skills）
    claude→ ~/.claude/skills
    auto  → 已存在的那个；都没有时按 dsh 创建
    """
    home = Path.home()
    dsh_home = Path(os.environ["DSH_HOME"]) if os.environ.get("DSH_HOME") else home / ".dsh"
    roots = {
        "dsh": dsh_home / "skills",
        "claude": home / ".claude" / "skills",
    }
    if target == "auto":
        if dsh_home.exists():
            target = "dsh"
        elif roots["claude"].parent.exists():
            target = "claude"
        else:
            target = "dsh"
    return target, roots[target]


def installed_name(bundle: Path) -> str:
    text = read_text(bundle / "SKILL.md") if (bundle / "SKILL.md").exists() else ""
    fm = parse_frontmatter(text)
    return slugify(fm.get("name") or bundle.name)


def _verify_install(dest: Path, name: str) -> list[tuple[str, bool]]:
    text = read_text(dest / "SKILL.md") if (dest / "SKILL.md").exists() else ""
    fm = parse_frontmatter(text)
    # 只认真正的文件名；正文里的「…见 references/）」「references/ 目录」不算引用
    refs = sorted(set(re.findall(r"references/([A-Za-z0-9_.\-]+\.[A-Za-z0-9]+)", text)))
    missing = [r for r in refs if not (dest / "references" / r).exists()]
    return [
        ("SKILL.md 已就位", (dest / "SKILL.md").exists()),
        ("frontmatter name 与目录名一致", fm.get("name") == name),
        (f"引用文件齐全（{len(refs) - len(missing)}/{len(refs)}）", not missing),
    ]


def _installed_skills(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir() and (p / "SKILL.md").exists())


def _list_installed(args) -> int:
    target, root = skill_root(args.target)
    if args.dir:
        root = Path(args.dir)
    skills = _installed_skills(root)
    rows = []
    for p in skills:
        a = score_bundle(p)
        rows.append({
            "name": installed_name(p),
            "path": str(p),
            "score": a["score"],
            "grade": a["grade"],
            "issues": a["issues"][:3],
        })
    if args.json:
        print(json.dumps({"root": str(root), "skills": rows}, ensure_ascii=False, indent=2))
        return 0
    print(f"skills 根目录：{root}（{target}）")
    if not rows:
        print("  （空）")
        return 0
    for r in rows:
        flag = "✓" if r["score"] >= 75 else "✗"
        print(f"  {flag} {r['name']:<28} {r['score']:>3}/100 {r['grade']}")
        if r["issues"]:
            print(f"      · {r['issues'][0]}")
    return 0


def cmd_install(args) -> int:
    if getattr(args, "list", False):
        return _list_installed(args)
    src = Path(args.skill_dir)
    if not (src / "SKILL.md").exists():
        raise SystemExit(f"不是 skill 包（缺 SKILL.md）：{src}")
    name = installed_name(src)
    target, root = skill_root(args.target)
    if args.dir:
        root = Path(args.dir)
    dest = root / name
    audit = score_bundle(src)
    if args.uninstall:
        if not dest.exists():
            raise SystemExit(f"没有安装：{dest}")
        if args.dry_run:
            print(f"[dry-run] 将删除：{dest}")
            return 0
        shutil.rmtree(dest)
        print(f"已卸载：{dest}")
        return 0
    if args.dry_run:
        print(f"[dry-run] {src} → {dest}（体检 {audit['score']}/100，目标 {target}）")
        return 0
    if dest.exists() and not args.force:
        raise SystemExit(f"已存在：{dest}（覆盖请加 --force）")
    if audit["score"] < 60 and not args.force:
        raise SystemExit(f"体检只有 {audit['score']}/100，先修好再加 --force 安装：\n  " +
                         "\n  ".join(audit["issues"][:5]))
    root.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pdf"))
    checks = _verify_install(dest, name)
    payload = {"target": target, "root": str(root), "path": str(dest), "name": name,
               "score": audit["score"], "grade": audit["grade"],
               "checks": [{"name": n, "ok": ok} for n, ok in checks]}
    bad = [n for n, ok in checks if not ok]
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"已安装：{dest}")
        for n, ok in checks:
            print(("  ✓ " if ok else "  ✗ ") + n)
        print(f"  体检：{audit['score']}/100（{audit['grade']}）")
        if target == "dsh":
            print("  下一步：在 DSH 工作区里启用该技能（技能管理 ↵ 工作区技能开关）。")
        else:
            print("  下一步：下次启动 Claude Code 即可自动发现。")
    return 0 if not bad else 1


def cmd_build(args) -> int:
    src = load_source(args.source, args)
    profile = getattr(args, "profile", "dsh") or "dsh"
    budget = args.budget
    name = slugify(args.name) if args.name else slugify(src.title or src.label)
    refs = pick_references(src, budget)
    gaps: list[str] = list(src.notes)
    if not src.readme:
        gaps.append("源材料没有 README/说明文档，概述只能从文件名与清单推断。")
    if len(refs) <= 1:
        gaps.append("可抽取的文档很少，建议把长文档放在 references/ 后重跑。")
    audit = {"gaps": gaps}
    skill_md = build_skill_md(src, name, refs, audit, profile)

    out = Path(args.output) if args.output else Path(name)
    sorted_files = sorted(src.files.items())
    meta = {
        "version": VERSION,
        "generator": "skillforge",
        "source": src.label,
        "source_kind": src.kind,
        "source_root": str(src.root) if src.root else None,
        "profile": profile,
        "name": name,
        "inputs": len(src.files),
        "source_files_total": len(sorted_files),
        "source_files": [{"path": rel, "sha256": sha256_text(text), "chars": len(text)}
                         for rel, text in sorted_files[:400]],
        "references": [{"path": "references/" + r, "chars": len(c), "sha256": sha256_text(c)}
                       for r, c in refs],
        "budget": budget,
        "chars_total": sum(len(c) for _, c in refs),
        "gaps": gaps,
        "commands": [f"python skillforge.py build {args.source} -o {out.as_posix()}"
                     + (f" --name {name}" if args.name else "")
                     + (f" --profile {profile}" if profile != "dsh" else "")],
    }

    if getattr(args, "dry_run", False):
        tmp = Path(tempfile.mkdtemp(prefix="skillforge-preview-"))
        try:
            _write_bundle(tmp, skill_md, refs, meta)
            audit = score_bundle(tmp)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        if args.json:
            print(json.dumps({"dry_run": True, "planned_out": str(out), "meta": meta,
                              "audit": audit}, ensure_ascii=False, indent=2))
            return 0 if audit["ok"] else 3
        print(f"（预演，不写盘）将生成：{out}")
        print(f"  SKILL.md  {len(skill_md):>7} 字符    profile={profile}")
        print(f"  references {len(refs):>6} 份 / {meta['chars_total']} 字符")
        print(f"  预计体检：{audit['score']}/100（{audit['grade']}） 正文 {audit.get('words', 0)} 词")
        if audit["issues"]:
            print("  待修：")
            for i in audit["issues"][:8]:
                print("   - " + i)
        print("  去掉 --dry-run 即写入。")
        return 0 if audit["ok"] else 3

    if out.exists():
        if not args.force and any(out.iterdir()):
            raise SystemExit(f"输出目录已存在且非空：{out}（加 --force 覆盖，或用 -o 换目录）")
        if args.force:
            shutil.rmtree(out)
    _write_bundle(out, skill_md, refs, meta)
    audit = score_bundle(out)
    if args.json:
        print(json.dumps({"out": str(out), "meta": meta, "audit": audit}, ensure_ascii=False, indent=2))
        return 0 if audit["ok"] else 3
    print(f"已生成：{out}")
    print(f"  SKILL.md  {len(skill_md):>7} 字符    profile={profile}")
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


def _split_frontmatter(text: str) -> tuple[str, str]:
    """返回 (frontmatter 块含 --- 分隔, 正文)。没有 frontmatter 时返回 ("", text)。"""
    m = re.match(r"(?s)^---\n.*?\n---\s*\n", text)
    if not m:
        return "", text
    return m.group(0), text[m.end():]


def cmd_diff(args) -> int:
    """检查 skill 是否落后于源材料：重读源、对比 meta.json 里记录的哈希。"""
    d = Path(args.skill_dir)
    meta_path = d / "meta.json"
    if not meta_path.exists():
        raise SystemExit(f"没有 meta.json：{meta_path}（只有 build 生成的目录才能 diff）")
    try:
        meta = json.loads(read_text(meta_path))
    except ValueError as e:
        raise SystemExit(f"meta.json 不是合法 JSON：{e}")
    recorded = {f["path"]: f["sha256"] for f in meta.get("source_files", [])}
    total = int(meta.get("source_files_total") or len(recorded))
    root = meta.get("source_root")
    label = meta.get("source") or ""

    if root and Path(root).exists():
        src = load_local_dir(Path(root)) if Path(root).is_dir() else load_single_file(Path(root))
    elif label and Path(label).exists():
        src = load_local_dir(Path(label)) if Path(label).is_dir() else load_single_file(Path(label))
    elif label:
        print(f"源材料不在本地（{label}），重新克隆/抓取后比对…", file=sys.stderr)
        src = load_source(label, None)
    else:
        raise SystemExit("meta.json 里没有 source / source_root，无法定位源材料")

    current = {rel: sha256_text(text) for rel, text in sorted(src.files.items())}
    added = sorted(set(current) - set(recorded))
    removed = sorted(set(recorded) - set(current))
    changed = sorted(k for k in set(current) & set(recorded) if current[k] != recorded[k])
    truncated = len(recorded) < total
    stale = bool(added or removed or changed or len(current) != total)
    result = {
        "skill_dir": str(d), "source": label,
        "recorded_files": len(recorded), "current_files": len(current),
        "truncated": truncated, "stale": stale,
        "added": added, "removed": removed, "changed": changed,
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if stale else 0
    print(f"{d}")
    print(f"  源：{label}（{len(recorded)}/{total} 个文件有哈希记录）")
    if truncated:
        print("  注意：meta.json 只记录了前 400 个文件，大批量改动可能漏报。")
    if not stale:
        print(f"  一致：{len(current)} 个源文件自上次生成后没有变化。")
        return 0
    print("  已过期：源材料变了，建议重跑 meta.json 里 commands 记录的命令。")
    for title, rows in (("新增", added), ("删除", removed), ("修改", changed)):
        if rows:
            print(f"  {title} {len(rows)} 个：")
            for r in rows[:10]:
                print("   - " + r)
            if len(rows) > 10:
                print(f"   … 另有 {len(rows) - 10} 个")
    if len(current) != total:
        print(f"  文件总数：{total} → {len(current)}")
    return 1


def cmd_fix(args) -> int:
    """自动修机械问题：name / description / whenToUse / references 链接。不动正文内容。"""
    d = Path(args.skill_dir)
    skill_md = d / "SKILL.md"
    if not skill_md.exists():
        raise SystemExit(f"没有 SKILL.md：{skill_md}")
    meta_path = d / "meta.json"
    if not getattr(args, "force", False):
        generated = False
        if meta_path.exists():
            try:
                generated = json.loads(read_text(meta_path)).get("generator") == "skillforge"
            except ValueError:
                generated = False
        if not generated:
            raise SystemExit(
                f"拒绝修改：{d} 不是 skillforge 生成的（没有 meta.json 或 generator 不符）。\n"
                "手写 skill 的措辞是你的资产，自动改写会破坏它；确认要改请加 --force。"
            )
    text = read_text(skill_md)
    fm = parse_frontmatter(text)
    _, body = _split_frontmatter(text)
    changes: list[str] = []

    old_name = (fm.get("name") or "").strip()
    name = slugify(old_name or d.name)
    if name != old_name:
        changes.append(f"name：{old_name or '（缺失）'} → {name}")

    desc = (fm.get("description") or "").strip()
    if len(desc) < 12:
        derived = first_body_paragraph(body, 300) or f"{name}：由 skillforge 生成、用于回答相关问题的 skill。"
        desc = one_line(clean_md(derived), 480)
        changes.append(f"description：重写（{len(desc)} 字符）")
    if not any(w in desc.lower() for w in ("use when", "when the user", "当用户", "用于", "触发")):
        zh = bool(re.search(r"[\u4e00-\u9fff]", body))
        desc = desc.rstrip("。.") + ("。当用户问到相关主题时使用。" if zh else ". Use when the user asks about this topic.")
        changes.append("description：补触发条件")

    when: list[str] = [x.strip("- ").strip() for x in str(fm.get("whenToUse") or "").splitlines() if x.strip()]
    if not when:
        lang = "zh" if re.search(r"[\u4e00-\u9fff]", body) else "en"
        kws = keywords(body, 6, lang)
        when = [f"用户问到「{k}」或需要它时" if lang == "zh" else f"the user asks about `{k}`"
                for k in kws[:3]]
        if not when:
            when = ["用户需要该 skill 描述的能力时" if lang == "zh" else "the user needs this skill's capability"]
        changes.append(f"whenToUse：补 {len(when)} 条")

    profile = "dsh"
    if meta_path.exists():
        try:
            profile = json.loads(read_text(meta_path)).get("profile") or "dsh"
        except ValueError:
            pass

    refs_dir = d / "references"
    if refs_dir.exists():
        ref_files = sorted(p.name for p in refs_dir.rglob("*") if p.is_file())
        unlinked = [n for n in ref_files if n not in body and f"references/{n}" not in body]
        if unlinked:
            body = body.rstrip() + "\n\n## 参考文件\n\n" + "\n".join(
                f"- `references/{n}` — 细节见该文件" for n in unlinked) + "\n"
            changes.append(f"参考文件：补 {len(unlinked)} 条链接")

    if not changes:
        print(f"{d}")
        print("  无需修复：机械项都已达标。")
        return 0

    new_text = build_frontmatter(name, desc, when, profile) + "\n" + body.lstrip("\n")
    skill_md.write_text(new_text, encoding="utf-8")
    audit = score_bundle(d)
    if args.json:
        print(json.dumps({"skill_dir": str(d), "changes": changes, "audit": audit},
                         ensure_ascii=False, indent=2))
        return 0 if audit["ok"] else 1
    print(f"{d}")
    for c in changes:
        print("  ✓ " + c)
    print(f"  修复后体检：{audit['score']}/100（{audit['grade']}）")
    if not audit["ok"] and audit["issues"]:
        print("  仍需手工处理：")
        for i in audit["issues"][:8]:
            print("   - " + i)
    return 0 if audit["ok"] else 1


def _lint_all(args) -> int:
    roots = [Path(r) for r in (args.roots or [])]
    if not roots:
        for t in ("dsh", "claude"):
            _, r = skill_root(t)
            if r.exists():
                roots.append(r)
        if Path("skills").is_dir():
            roots.append(Path("skills"))
    if not roots:
        print("没有找到任何 skills 根目录（用 --roots 指定，或先 skillforge install）。")
        return 1
    min_score = args.min if args.min else 75
    rows: list[dict] = []
    for root in roots:
        for p in _installed_skills(root):
            a = score_bundle(p)
            fm = parse_frontmatter(read_text(p / "SKILL.md"))
            rows.append({
                "root": str(root),
                "path": str(p),
                "name": fm.get("name") or p.name,
                "score": a["score"],
                "grade": a["grade"],
                "ok": a["ok"],
                "issues": a["issues"][:3],
            })
    bad = [r for r in rows if r["score"] < min_score]
    if args.json:
        print(json.dumps({"roots": [str(r) for r in roots], "min": min_score,
                          "total": len(rows), "below": len(bad), "skills": rows},
                         ensure_ascii=False, indent=2))
    else:
        if not rows:
            print(f"扫过 {len(roots)} 个根目录，没有找到 skill 包。")
            return 1
        print(f"共 {len(rows)} 个 skill（门禁 ≥{min_score}）")
        for r in rows:
            flag = "✓" if r["score"] >= min_score else "✗"
            print(f"  {flag} {r['name']:<28} {r['score']:>3}/100 {r['grade']:<3} {r['path']}")
            if r["score"] < min_score and r["issues"]:
                print(f"      · {r['issues'][0]}")
        print(f"结论：{len(rows) - len(bad)}/{len(rows)} 达标" +
              (f"，{len(bad)} 个低于门禁" if bad else ""))
    return 0 if not bad else 1


def cmd_lint(args) -> int:
    if getattr(args, "all", False):
        return _lint_all(args)
    if not args.skill_dir:
        raise SystemExit("用法：skillforge lint <skill_dir>  或  skillforge lint --all [--roots ROOT ...]")
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
    if audit["ok"]:
        print(f"  结论：通过（{audit['score']}/100，≥75 算健康）")
    else:
        print(f"  结论：不通过（{audit['score']}/100，<75）")
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
               "  python skillforge.py build paper.pdf -o out/paper-skill --profile claude\n"
               "  python skillforge.py build ./my-repo -o out/x --dry-run\n"
               "  python skillforge.py lint out/my-skill\n"
               "  python skillforge.py install out/my-skill          # 装到 DSH/Claude 的 skills 目录\n"
               "  python skillforge.py lint --all                     # 批量体检已安装的 skill\n"
               "  python skillforge.py fix  out/my-skill\n"
               "  python skillforge.py diff out/my-skill\n",
    )
    ap.add_argument("--version", action="version", version=f"skillforge {VERSION}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="从源材料生成 skill 包")
    b.add_argument("source", help="本地目录/文件、git URL、网页 URL 或 PDF")
    b.add_argument("-o", "--output", help="输出目录（默认 ./<name>）")
    b.add_argument("--name", help="skill 名（默认从源名推断，kebab-case）")
    b.add_argument("--profile", choices=PROFILES, default="dsh",
                   help="目标运行时：dsh（含 whenToUse）/ claude（触发词折叠进 description）/ generic")
    b.add_argument("--budget", type=int, default=DEFAULT_BUDGET, help=f"references 总字符预算（默认 {DEFAULT_BUDGET}）")
    b.add_argument("--force", action="store_true", help="输出目录已存在时覆盖")
    b.add_argument("--dry-run", action="store_true", help="只预演：打印将生成什么、预计得分，不写盘")
    b.add_argument("--json", action="store_true", help="额外输出机器可读 JSON")

    l = sub.add_parser("lint", help="体检一个（或全部）已有 skill 包")
    l.add_argument("skill_dir", nargs="?", help="skill 包目录（用了 --all 时可省略）")
    l.add_argument("--all", action="store_true", help="扫描 skills 根目录下所有 skill")
    l.add_argument("--roots", nargs="*", help="配合 --all：要扫描的根目录（默认 dsh/claude/./skills）")
    l.add_argument("--min", type=int, default=0, help="配合 --all：低于该分数视为不达标（默认 75）")
    l.add_argument("--json", action="store_true")

    ins = sub.add_parser("install", help="把生成的 skill 装到 DSH / Claude 的 skills 目录并回读校验")
    ins.add_argument("skill_dir", nargs="?", help="skill 包目录（用了 --list 时可省略）")
    ins.add_argument("--target", choices=SKILL_TARGETS, default="auto", help="目标运行时（默认 auto 探测）")
    ins.add_argument("--dir", help="自定义 skills 根目录（覆盖 --target）")
    ins.add_argument("--force", action="store_true", help="覆盖已安装的同名 skill / 低分也装")
    ins.add_argument("--dry-run", action="store_true", help="只打印计划，不落盘")
    ins.add_argument("--uninstall", action="store_true", help="卸载目标目录里的同名 skill")
    ins.add_argument("--list", action="store_true", help="列出现已安装的 skill 及分数")
    ins.add_argument("--json", action="store_true")

    fx = sub.add_parser("fix", help="自动修复机械问题（name/description/whenToUse/引用链接）")
    fx.add_argument("skill_dir")
    fx.add_argument("--force", action="store_true", help="允许修改非 skillforge 生成的目录")
    fx.add_argument("--json", action="store_true")

    df = sub.add_parser("diff", help="检查 skill 是否落后于源材料（对比 meta.json 里的哈希）")
    df.add_argument("skill_dir")
    df.add_argument("--json", action="store_true")

    s = sub.add_parser("score", help="只打分")
    s.add_argument("skill_dir")
    s.add_argument("--min", type=int, default=0, help="低于该分数时退出码为 1（默认 0=不检查）")
    s.add_argument("--json", action="store_true")

    sub.add_parser("selftest", help="运行内置自检")

    args = ap.parse_args(argv)
    if args.cmd == "build":
        return cmd_build(args)
    if args.cmd == "lint":
        return cmd_lint(args)
    if args.cmd == "install":
        return cmd_install(args)
    if args.cmd == "fix":
        return cmd_fix(args)
    if args.cmd == "diff":
        return cmd_diff(args)
    if args.cmd == "score":
        d = Path(args.skill_dir)
        if not (d / "SKILL.md").exists():
            raise SystemExit(f"没有 SKILL.md：{d}")
        a = score_bundle(d)
        if args.json:
            print(json.dumps(a, ensure_ascii=False, indent=2))
        else:
            print(f"{a['score']}/100（{a['grade']}）")
        if args.min and a["score"] < args.min:
            return 1
        return 0
    if args.cmd == "selftest":
        return cmd_selftest(args)
    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
