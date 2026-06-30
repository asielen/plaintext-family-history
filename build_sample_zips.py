#!/usr/bin/env python3
"""Build the downloadable sample archives into downloads/.

Each sample folder in the repo stays lean (it does NOT carry its own copy of the
big rulebooks); this script bundles the relevant docs into each zip so every
download is fully self-contained and detachable. Re-run after editing a sample,
or after a SPEC/TOOLING change, to refresh the zips.

    python build_sample_zips.py
"""
from __future__ import annotations
import pathlib, zipfile

REPO = pathlib.Path(__file__).resolve().parent
OUT = REPO / "downloads"

SKIP_SUFFIX = (".sqlite", ".sqlite-journal", ".pyc")
# .cache/ is the archive's rebuildable, machine-local state (see .gitignore) - the
# generated site, the query index, vendored JS. It is never archive content, so it
# must stay out of the downloads or they bloat and stop being reproducible.
SKIP_PART = (".cache", "__pycache__", ".DS_Store", "Thumbs.db")

# The full system showcase carries the tool design too; the by-hand starters omit
# TOOLING.md (irrelevant to a no-tools user) but keep the law + agent docs.
FULL_DOCS = ["SPEC.md", "TOOLING.md", "AGENTS.md", "CLAUDE.md", "docs/USING_WITH_OBSIDIAN.md"]
HAND_DOCS = ["SPEC.md", "AGENTS.md", "CLAUDE.md", "docs/USING_WITH_OBSIDIAN.md"]

SAMPLES = [
    ("example-archive", FULL_DOCS),
    ("quickstart-template", HAND_DOCS),
    ("quickstart-example", HAND_DOCS),
]


def included(rel: str) -> bool:
    if any(p in rel for p in SKIP_PART):
        return False
    return not rel.endswith(SKIP_SUFFIX)


def main() -> None:
    OUT.mkdir(exist_ok=True)
    for name, docs in SAMPLES:
        src = REPO / name
        if not src.is_dir():
            print(f"  SKIP {name} (folder not found)")
            continue
        zip_path = OUT / f"{name}.zip"
        n = 0
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            for f in sorted(src.rglob("*")):
                if not f.is_file():
                    continue
                rel = f.relative_to(src).as_posix()
                if included(rel):
                    z.write(f, f"{name}/{rel}")
                    n += 1
            for d in docs:
                doc = REPO / d
                if doc.exists():
                    z.write(doc, f"{name}/{doc.name}")
                    n += 1
                else:
                    print(f"  WARN {name}: missing bundled doc {d}")
        print(f"{zip_path.name:28} {n:4d} files  {zip_path.stat().st_size/1024:7.1f} KB")
    print("done ->", OUT)


if __name__ == "__main__":
    main()
