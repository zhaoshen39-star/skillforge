# Changelog

All notable changes to skillforge are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning: semver.

## [0.2.0] — 2026-11-03

Generation is only half the problem: skills also go stale and rot silently.
This release closes the loop — generate → audit → fix → detect drift.

### Added
- `fix <skill-dir>` — repairs the mechanical failures the auditor finds:
  non-kebab `name`, missing/too-short `description`, missing trigger words,
  missing `whenToUse`, and `references/*` that the body never links.
  It rewrites only frontmatter and appends a reference list — never body content.
  Refuses to touch directories it did not generate (needs `--force`), because a
  hand-written skill's wording is its value.
- `diff <skill-dir>` — compares the source against the SHA-256 hashes recorded in
  `meta.json` and reports added / removed / changed files. Exit code `1` when stale,
  so CI can fail builds whose skills no longer match their source.
- `--profile dsh|claude|generic` on `build` — target-runtime frontmatter:
  DSH keeps `whenToUse`; Claude-only runtimes fold trigger text into `description`
  (they route on `name` + `description` alone). The scorer now waives the
  `whenToUse` check for `claude`-profile bundles.
- `--dry-run` on `build` — prints what would be generated and what it would score,
  without writing anything.
- `--min N` on `score` — exit `1` when below the threshold, for CI gates.
- `meta.json` now records `profile`, `source_root`, and a per-file hash manifest
  (first 400 files) — the basis for `diff`.

### Changed
- `meta.json` schema bumped alongside `VERSION`; `diff` tolerates old bundles
  that predate the hash manifest (reports them as stale).

### Fixed
- `fix` is idempotent: a second run reports "无需修复" instead of re-appending the
  trigger sentence to `description`.
- Test suite grew from 37 to 64 checks covering profiles, dry-run, diff, fix,
  the fix guard rail, and score thresholds.

## [0.1.0] — 2026-10-31

### Added
- `build` from a local directory, a single file (`.md` / `.pdf` via optional
  `pypdf`), a GitHub-style git URL (`git clone --depth 1`, HTML fallback), or a web page.
- Deterministic generation: `SKILL.md` + budgeted `references/` + `meta.json`,
  with no timestamps in the output.
- `lint` / `score`: 12 weighted checks over 100 points (name, description,
  routing, body size, TODO residue, references, dedupe, placeholders, headings),
  with CI-friendly exit codes.
- `selftest` — offline fixture, no network needed.
- Windows-safe I/O (UTF-8 console, CRLF-normalised reads, mojibake guards).
