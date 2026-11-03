# v0.5.0 — triggers from real questions, watch mode, PDF cleanup

**skillforge** compiles any repo, doc or PDF into an installable, auditable agent skill.
Zero dependencies, reproducible output, 120 offline tests.

```bash
git clone https://github.com/zhaoshen39-star/skillforge.git
cd skillforge
python skillforge.py --help          # build / lint / fix / diff / install / scan / score / selftest
python skillforge.py build . -o out/myskill
python skillforge.py lint out/myskill
```

## Highlights

- **`build --from-tests PATH`** — real user questions go first in `whenToUse`
  (Chinese questions get a Chinese template, the rest English; sources recorded in `meta.json`).
- **`build --watch [SEC]`** — fingerprints the source and rebuilds on every save,
  default every 2 s; Ctrl-C to stop, a failing rebuild does not kill the loop.
- **PDF layout cleanup** — page markers, repeated header/footer removal,
  hyphenated word rejoin; falls back to system `pdftotext` when `pypdf` is absent
  and tells you what to install when neither is available.

## Full changelog

### Added

- `build --from-tests PATH` — reads example user questions (one per line; a file or a
  directory of `.md`/`.txt`, repeatable) and writes them **first** in `whenToUse`.
  Chinese questions get a Chinese template, the rest an English one; the source paths
  are recorded in `meta.json.from_tests`.
- `build --watch [SEC]` — fingerprints the source (file list + per-file hash), rebuilds
  on change (default every 2 s), keeps the output directory in sync, Ctrl-C to stop.
  A failing rebuild does not kill the loop.
- PDF layout cleanup: `[第 N 页]` page markers, repeated header/footer removal
  (≥60% of pages, ≥3 pages), hyphenated word rejoin; falls back to system
  `pdftotext -layout` when `pypdf` is absent, and says what to install when neither exists.

### Fixed

- `build` into an existing output directory in watch mode no longer fails with
  "directory not empty" — watch owns its output and overwrites it.
- PDF-sourced skills use the first body line as the title instead of leaking the
  absolute file path into `description`.

### Verification

- 120 offline checks (`python tests/verify_skillforge.py`) + 11 built-in `selftest` checks.
- CI green on ubuntu-latest and windows-latest × Python 3.9 / 3.12.
- Self-build: `skillforge build .` → 100/100, `lint` passes, `diff` reports all 11 sources in sync.
- Real `--watch` run: edit source → rebuild detected and emitted, clean exit.
- Real PDF run: 3-page PDF with repeating headers → page markers correct, headers removed, build 100/100.
