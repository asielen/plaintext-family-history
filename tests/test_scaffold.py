"""
Tests for tools/scaffold.py - `fha install` and `fha update-tools` (BUILD.md M9).

Two layers:
  1. A manifest-sync guard that recomputes the operating-layer manifest from the
     real repo and asserts the committed manifest.json still matches it - so a PR
     that changes a tool/doc/skeleton file but forgets to regenerate fails here.
  2. Behavior tests against a small, hand-built FAKE repo (no .git/, proving the
     git-free / zip install path) and throwaway archives, exercising install,
     re-install refusal, the four update outcomes (add/stock/customized/retired),
     the critical skeleton-is-never-touched safety property, dry-run no-ops, and
     the friendly error paths.

Run: python -m unittest tests.test_scaffold -v
"""

import argparse
import contextlib
import io
import json
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import scaffold
from _lib import EXIT_CLEAN, EXIT_FAILURE, EXIT_WARNINGS


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')


_MD_LINK = re.compile(r'\[[^\]]*\]\(([^)\s]+)(?:\s+"[^"]*")?\)')
_FENCED_CODE = re.compile(r'^```.*?^```', re.MULTILINE | re.DOTALL)
_CODE_SPAN = re.compile(r'`[^`\n]*`')


def _strip_code(text: str) -> str:
    """Blank out fenced blocks and inline code spans before hunting for links.

    A link shape inside code is an EXAMPLE, not a link: tools/README.md's
    allowlist row documents `[text](url)` in backticks, and a fenced block may
    show a snippet of markdown. Neither is something a reader clicks, so neither
    should be resolved against the shipped file set. Replaced with blank lines
    rather than deleted so nothing outside the code accidentally joins up.
    """
    text = _FENCED_CODE.sub(lambda m: '\n' * m.group(0).count('\n'), text)
    return _CODE_SPAN.sub(' ', text)


def _dead_relative_links(archive_path: str, text: str,
                         shipped: set[str]) -> list[str]:
    """Relative markdown links in `text` that no installed archive can follow.

    `archive_path` is where the document LANDS in an archive, which is what the
    links resolve against - not where its source sits in this repo. A shipped
    document may legitimately point at a folder (`../design/`), so a target is
    also accepted when it is an ancestor of some shipped file. Absolute URLs,
    bare fragments and protocol-relative links are somebody else's problem.
    """
    text = _strip_code(text)
    directories = set()
    for path in shipped:
        parts = path.split('/')
        for depth in range(1, len(parts)):
            directories.add('/'.join(parts[:depth]))

    dead: list[str] = []
    for target in _MD_LINK.findall(text):
        if target.startswith(('#', '//')) or re.match(r'^[a-z][a-z0-9+.-]*:', target):
            continue
        bare = target.split('#')[0]
        if not bare:
            continue
        # Resolve textually. PurePosixPath keeps '..' segments, and the real
        # question is where a reader's file browser ends up, not what exists on
        # the machine running the tests.
        segments: list[str] = []
        for part in f'{pathlib.PurePosixPath(archive_path).parent}/{bare}'.split('/'):
            if part == '..':
                if segments:
                    segments.pop()
                else:
                    segments.append('..')
            elif part not in ('.', ''):
                segments.append(part)
        resolved = '/'.join(segments)
        if resolved not in shipped and resolved not in directories:
            dead.append(f'{target} -> {resolved}')
    return dead


def _make_fake_repo(repo: Path) -> Path:
    """Build a minimal public-repo clone: 3 operating files + 3 skeleton seeds.

    Deliberately tiny so the tests are fast and every file's role is obvious. No
    .git/ is created anywhere - install/update working against this directory is
    itself the proof of the zip-based, git-free path (BUILD.md M9.1).
    """
    _write(repo / 'SPEC.md', '# SPEC\n\n**Version 9.9 - 2026-01-01**\n\nbody\n')
    _write(repo / 'tools' / 'atool.py', 'print("a tool")\n')
    _write(repo / 'docs' / 'guide.md', '# Guide\n')
    # bytecode that must NOT enter the manifest
    _write(repo / 'tools' / '__pycache__' / 'atool.cpython-312.pyc', 'junk\n')
    # skeleton (remapped from archive-template/ → archive root)
    _write(repo / 'archive-template' / 'fha.yaml', 'roots:\n  photos: photos\n')
    _write(repo / 'archive-template' / 'places' / 'places.yaml', '# places\n')
    _write(repo / 'archive-template' / 'sources' / '.gitkeep', '')
    _write(repo / 'archive-template' / 'README.md', 'template readme - excluded\n')
    # The install-once skeleton override that lives UNDER an operating subtree,
    # so it is the one seed whose archive path moves with the vendor layout.
    _write(repo / 'design' / 'custom.css', '/* stock */\n')
    scaffold._write_manifest(repo)
    return repo


class ManifestSyncTest(unittest.TestCase):
    """The committed manifest.json must match what the repo currently contains."""

    @unittest.skipIf(sys.platform == 'win32',
                     'git core.autocrlf rewrites tracked LF files (e.g. '
                     'design/custom.css, serve.cmd) to CRLF on a Windows '
                     'checkout, so regenerated byte-hashes differ from the '
                     'LF-based committed manifest; CI (Linux) checks currency.')
    def test_committed_manifest_is_current(self):
        committed = json.loads((ROOT / 'manifest.json').read_text(encoding='utf-8'))
        regenerated = scaffold.generate_manifest(ROOT)
        # `generated` is a date stamp that legitimately changes day to day; the
        # contract is the file set + checksums + versions.
        self.assertEqual(committed['manifest_version'], regenerated['manifest_version'])
        self.assertEqual(committed['spec_version'], regenerated['spec_version'])
        self.assertEqual(
            committed['files'], regenerated['files'],
            'manifest.json is out of date - run '
            '`python tools/scaffold.py write-manifest --repo .` and commit the result.',
        )

    def test_manifest_excludes_repo_furniture(self):
        paths = {e['path'] for e in scaffold.generate_manifest(ROOT)['files']}
        # Public-repo furniture that must never enter an archive: PRIVACY.md is the
        # "no real data" policy (contradictory inside a real archive), and the
        # release checklist / packing list / template's own readme are spec-repo
        # maintenance.
        for furniture in ('PRIVACY.md', 'RELEASE_CHECKLIST.md',
                          'manifest.json', 'archive-template/README.md'):
            self.assertNotIn(furniture, paths)
        # No bytecode, no example/test furniture.
        self.assertFalse(any('__pycache__' in p or p.endswith('.pyc') for p in paths))
        self.assertFalse(any(p.startswith(('example-archive/', 'tests/', 'archive-template/'))
                             for p in paths))

    def test_manifest_includes_operating_extras(self):
        paths = {e['path'] for e in scaffold.generate_manifest(ROOT)['files']}
        # The owner's entry point + the agent's workflow procedures ship into
        # archives. (The project README does NOT - see
        # test_no_readme_ships_into_an_archive.)
        self.assertIn('GETTING_STARTED.md', paths)
        self.assertIn('.claude/skills/README.md', paths)
        # but the spec-repo's own agent config does not.
        self.assertNotIn('.claude/settings.json', paths)

    def test_manifest_excludes_builder_docs(self):
        # The tool-BUILDING docs are not vendored into an archive: no tool reads
        # them at run time and a genealogist never needs them (workshop-only).
        paths = {e['path'] for e in scaffold.generate_manifest(ROOT)['files']}
        for builder in ('BUILD.md', 'BUILD_INGESTION.md', 'BUILD_INTERFACE.md',
                        'TOOLING_INGESTION.md', 'TOOLING_INTERFACE.md',
                        'AGENTS_TOOLING.md'):
            self.assertNotIn(builder, paths, builder)
        # The operating spec/agent docs that DO ship stay.
        for shipped in ('SPEC.md', 'TOOLING.md', 'AGENTS.md', 'CLAUDE.md',
                        'GETTING_STARTED.md', 'CHEATSHEET.md'):
            self.assertIn(shipped, paths, shipped)

    def test_manifest_vendors_the_browser_companion(self):
        # The capture extension is a front-end tool like the serve workbench
        # (owner decision 2026-07-26): it has no build step, so its source tree
        # IS the loadable artifact and every archive carries it ready for
        # chrome://extensions "Load unpacked" at .fha/browser-companion/.
        entries = {e['path']: e for e in scaffold.generate_manifest(ROOT)['files']}
        for shipped in ('.fha/browser-companion/manifest.json',
                        '.fha/browser-companion/src/background.js',
                        '.fha/browser-companion/src/panel.html',
                        '.fha/browser-companion/src/lib/capture-json.js',
                        '.fha/browser-companion/icons/icon128.png',
                        '.fha/browser-companion/README.md'):
            self.assertIn(shipped, entries, shipped)
            self.assertEqual(entries[shipped]['category'], 'operating')
        # The src/path seam records the repo-flat source for vendored files -
        # except the README, which is deliberately a DIFFERENT document in an
        # archive than in the project (see the next test).
        for shipped, src in entries.items():
            if not shipped.startswith('.fha/browser-companion/'):
                continue
            if shipped == '.fha/browser-companion/README.md':
                continue
            self.assertEqual(src['src'], shipped.removeprefix('.fha/'), shipped)
        # Dev furniture never enters an archive: the node test-suite, its
        # capture-bundle fixtures, the npm manifest, the hand-test walkthrough.
        paths = set(entries)
        self.assertFalse(any('browser-companion/tests/' in p for p in paths))
        self.assertFalse(any('browser-companion/test-bundle/' in p for p in paths))
        self.assertNotIn('.fha/browser-companion/package.json', paths)
        self.assertNotIn('.fha/browser-companion/ANCESTRY-AUTOFETCH-TEST.md', paths)

    def test_the_installed_capture_readme_is_the_owner_one(self):
        # The extension's project README is written for whoever WORKS ON the
        # extension: it points at ../TOOLING_INGESTION.md, the node test-suite,
        # test-bundle/ and the hand-test walkthrough, none of which an installed
        # archive carries. Shipping it handed the owner a guide of dead links and
        # a test command that cannot run, so the archive gets its own README.
        entries = {e['path']: e for e in scaffold.generate_manifest(ROOT)['files']}
        installed = entries['.fha/browser-companion/README.md']
        self.assertEqual(installed['src'], 'browser-companion/README-ARCHIVE.md')
        # The project README stays home - and would still be a dead-link guide.
        self.assertNotIn('browser-companion/README.md',
                         {e.get('src', e['path'])
                          for e in scaffold.generate_manifest(ROOT)['files']})
        text = (ROOT / 'browser-companion' / 'README-ARCHIVE.md').read_text(
            encoding='utf-8')
        # It has to answer the four questions an owner actually has.
        self.assertIn('Load unpacked', text)
        self.assertIn('fha capture --ingest', text)
        self.assertIn('.fha', text)
        # And none of the workshop-only references that made the old one useless.
        for absent in ('TOOLING_INGESTION', 'ANCESTRY-AUTOFETCH-TEST',
                       'test-bundle', 'test_browser_companion', 'package.json'):
            self.assertNotIn(absent, text, absent)

    def test_the_installed_capture_readme_links_only_to_shipped_files(self):
        # The rule the old README broke, enforced: every relative link in the
        # document an archive receives has to land on something the installer
        # actually put there. An owner following a dead link has no way to tell
        # whether the file is missing or they are.
        manifest = scaffold.generate_manifest(ROOT)['files']
        shipped = {e['path'] for e in manifest}
        dead = _dead_relative_links(
            '.fha/browser-companion/README.md',
            (ROOT / 'browser-companion' / 'README-ARCHIVE.md').read_text(
                encoding='utf-8'),
            shipped)
        self.assertEqual(dead, [], f'links to files no archive has: {dead}')

    def test_manifest_includes_launchers(self):
        # plan 17 + archive-layout: the double-clickable workbench launcher AND
        # the terminal CLI shims all ship into every archive - BOTH platforms'
        # shims regardless of the installing OS, because an archive is a portable
        # folder that may well be opened on a different machine than it was made
        # on. All are layout-agnostic - they probe .fha/tools/ first, then
        # tools/ - so one vendored file works flat or consolidated under .fha/.
        entries = {e['path']: e for e in scaffold.generate_manifest(ROOT)['files']}
        for launcher in ('serve.cmd', 'fha.cmd', 'fha'):
            self.assertIn(launcher, entries, launcher)
            self.assertEqual(entries[launcher]['category'], 'operating')
        serve = (ROOT / 'serve.cmd').read_text(encoding='utf-8')
        self.assertIn(r'.fha\tools\fha.py', serve)         # consolidated
        self.assertIn(r'tools\fha.py', serve)               # flat fallback
        fha_cmd = (ROOT / 'fha.cmd').read_text(encoding='utf-8')
        self.assertIn(r'.fha\tools\fha.py', fha_cmd)

    def test_double_click_launcher_never_shows_a_raw_error(self):
        # serve.cmd is the file a NON-TECHNICAL owner double-clicks, so it must
        # never flash a raw interpreter error and vanish. fha.cmd already guards
        # both failure modes; serve.cmd must too - by delegating to it, or (for an
        # archive predating the launchers) by checking the same two things itself.
        serve = (ROOT / 'serve.cmd').read_text(encoding='utf-8')
        self.assertIn('fha.cmd', serve, 'should hand over to the hardened shim')
        self.assertIn('version_info >= (3, 10)', serve,
                      'a standalone fallback must still check the Python version')
        self.assertIn(':no_tools', serve,
                      'a missing toolset must produce guidance, not a raw error')
        # Every exit path a double-click can reach has to hold the window open.
        for label in (':no_tools', ':no_python', ':trouble'):
            # Split on the label DEFINITION (start of line), not the goto above it.
            tail = serve.split(f'\n{label}\n', 1)[1].split('exit /b', 1)[0]
            self.assertIn('pause', tail,
                          f'{label} must pause so the message can be read')

    def test_posix_launcher_is_usable(self):
        # Without this file, every `fha ...` example in the guides is a
        # `command not found` on macOS and Linux - including the very first
        # post-install health check.
        posix = ROOT / 'fha'
        self.assertTrue(posix.is_file(), 'the POSIX `fha` launcher must ship')
        text = posix.read_text(encoding='utf-8')
        self.assertTrue(text.startswith('#!/bin/sh'), 'needs a shebang')
        self.assertIn('.fha/tools/fha.py', text)   # consolidated, probed first
        self.assertIn('tools/fha.py', text)        # flat fallback
        # LF-only: .gitattributes pins it so a Windows checkout cannot produce a
        # CRLF script that /bin/sh chokes on (and that would break its checksum).
        self.assertNotIn(b'\r\n', posix.read_bytes())
        if os.name != 'nt':
            self.assertTrue(os.access(posix, os.X_OK),
                            'the POSIX launcher must be executable')
        # The tool suite is 3.10+ syntax, so a python3 that is 3.8/3.9 must be
        # REJECTED rather than handed the code - otherwise the user gets a raw
        # SyntaxError instead of the launcher's install guidance. Both candidates
        # are probed for the real version, not trusted by name.
        self.assertEqual(text.count('version_info >= (3, 10)'), 1)
        self.assertIn('for candidate in python3 python', text)
        # The missing-tools message must name a command that can actually run:
        # `fha update-tools` alone loops back here, and update-tools needs both
        # --repo and --root when driven from outside the archive.
        recovery = text.split('cannot find the tools')[1]
        self.assertIn('--repo', recovery)
        self.assertIn('--root', recovery)

    @unittest.skipIf(os.name == 'nt', 'POSIX launcher; /bin/sh not available')
    def test_posix_launcher_rejects_old_python_and_guides(self):
        # Behavioural, not just textual: stand up a fake `python3` that reports
        # older than 3.10 and confirm the launcher refuses it with guidance.
        with tempfile.TemporaryDirectory() as td:
            box = Path(td)
            shutil.copy2(ROOT / 'fha', box / 'fha')
            os.chmod(box / 'fha', 0o755)
            _write(box / 'tools' / 'fha.py', 'print("SHOULD NOT RUN")\n')
            fakebin = box / 'bin'
            for name in ('python3', 'python'):
                _write(fakebin / name,
                       '#!/bin/sh\ncase "$*" in *version_info*) exit 1 ;; esac\n'
                       'echo SHOULD-NOT-RUN\n')
                os.chmod(fakebin / name, 0o755)
            # PATH holds ONLY the fake interpreters, so neither candidate can
            # satisfy the version probe and the guidance path must be taken.
            proc = subprocess.run(
                [str(box / 'fha'), 'lint'], capture_output=True, text=True,
                env={**os.environ, 'PATH': str(fakebin)})
            self.assertEqual(proc.returncode, EXIT_FAILURE)
            self.assertIn('3.10', proc.stderr)
            self.assertNotIn('SHOULD-NOT-RUN', proc.stdout)

    @unittest.skipIf(os.name == 'nt', 'POSIX launcher; /bin/sh not available')
    def test_posix_launcher_missing_tools_message_is_actionable(self):
        with tempfile.TemporaryDirectory() as td:
            box = Path(td)
            shutil.copy2(ROOT / 'fha', box / 'fha')
            os.chmod(box / 'fha', 0o755)
            proc = subprocess.run([str(box / 'fha'), 'lint'],
                                  capture_output=True, text=True)
            self.assertEqual(proc.returncode, EXIT_FAILURE)
            # Names the archive it could not repair, and a runnable command.
            self.assertIn(str(box), proc.stderr)
            self.assertIn('update-tools', proc.stderr)
            self.assertIn('--repo', proc.stderr)
            self.assertIn('--root', proc.stderr)

    def test_windows_launcher_validates_python(self):
        # A machine with no `py` launcher, or one whose `py -3` is older than
        # 3.10, otherwise gets a bare "not recognized" or a raw SyntaxError -
        # neither of which tells a non-technical owner what to do. The POSIX
        # launcher already guides them; Windows must match.
        text = (ROOT / 'fha.cmd').read_text(encoding='utf-8')
        self.assertEqual(text.count('sys.version_info >= (3, 10)'), 2)  # py, python
        self.assertIn('python.org/downloads', text)
        self.assertIn(':no_python', text)
        # cmd expands %VAR% when it PARSES a parenthesised block, so a variable
        # set and read in one block reads a stale value. The interpreter choice
        # must therefore not be made inside an if-block.
        self.assertNotIn('if not defined FHA_PY (', text)

    def test_windows_launcher_handles_missing_tools(self):
        # Parity with the POSIX launcher: without this, a damaged archive hands
        # the missing path straight to Python and the Windows user gets a raw
        # interpreter error with no recovery step.
        text = (ROOT / 'fha.cmd').read_text(encoding='utf-8')
        self.assertIn(r'if exist "%~dp0tools\fha.py"', text)
        self.assertIn('cannot find the tools', text)
        recovery = text.split('cannot find the tools')[1]
        self.assertIn('update-tools', recovery)
        self.assertIn('--repo', recovery)
        self.assertIn('--root', recovery)
        self.assertIn('exit /b 3', text)

    def test_shipped_rulebooks_do_not_link_relatively_to_unshipped_docs(self):
        # AGENTS.md/CLAUDE.md tell an agent to read AGENTS_TOOLING.md, which this
        # PR stopped vendoring. In an installed archive a relative link there is a
        # dead end, so the mandated checklist cannot be followed. Every reference
        # from a SHIPPED file to an UNSHIPPED doc must be an absolute URL.
        manifest = scaffold.generate_manifest(ROOT)['files']
        shipped = {e['path'] for e in manifest}
        unshipped = ('AGENTS_TOOLING.md', 'BUILD.md', 'BUILD_INGESTION.md',
                     'BUILD_INTERFACE.md', 'TOOLING_INGESTION.md',
                     'TOOLING_INTERFACE.md')
        for doc in unshipped:
            self.assertNotIn(doc, shipped, f'{doc} is workshop-only')
        offenders = []
        # Read each doc through its manifest `src`, not its archive path: a
        # vendored file's archive path (.fha/browser-companion/README.md) does
        # not exist in this repo, so resolving by path skipped every vendored
        # document silently - which is how the capture extension's README kept
        # its ../TOOLING_INGESTION.md link through this very check.
        for entry in sorted(manifest, key=lambda e: e['path']):
            rel = entry['path']
            src = ROOT / entry.get('src', rel)
            if src.suffix != '.md' or not src.is_file():
                continue
            text = src.read_text(encoding='utf-8', errors='replace')
            for doc in unshipped:
                for form in (f']({doc})', f'](../{doc})', f'](../../{doc})'):
                    if form in text:
                        offenders.append(f'{rel} -> {form}')
        self.assertEqual(offenders, [], f'relative links to workshop-only docs: {offenders}')

    def test_no_shipped_markdown_has_a_dead_relative_link(self):
        # The general form of the check above, and the one that catches the whole
        # class: EVERY markdown file the installer copies, resolved at the path it
        # LANDS on, must have every relative link land on something the installer
        # also copied. The narrow version only looked for six named workshop docs,
        # so a guide pointing at `../archive-template/` or `../example-archive/` -
        # repo folders no archive receives - sailed through, and the owner
        # following it hit a folder that does not exist with no way to tell
        # whether the archive was broken or they were.
        manifest = scaffold.generate_manifest(ROOT)['files']
        shipped = {e['path'] for e in manifest}
        offenders: list[str] = []
        # Read through the manifest `src`, not the archive path: a vendored file's
        # archive path (.fha/tools/README.md) does not exist in this repo, so
        # resolving by path would skip every vendored document silently.
        for entry in sorted(manifest, key=lambda e: e['path']):
            rel = entry['path']
            src = ROOT / entry.get('src', rel)
            if src.suffix != '.md' or not src.is_file():
                continue
            for dead in _dead_relative_links(
                    rel, src.read_text(encoding='utf-8', errors='replace'),
                    shipped):
                offenders.append(f'{rel}: {dead}')
        self.assertEqual(
            offenders, [],
            'links an installed archive cannot follow:\n  '
            + '\n  '.join(offenders))

    def test_the_two_owner_entry_docs_install_at_the_archive_root(self):
        # Owner decision (2026-08-16): docs are split by audience, expressed
        # through location. The two an archive owner needs on day one sit at the
        # archive ROOT where they cannot be missed; everything else stays in
        # docs/. Their repo path is identical to their install path, because the
        # installer does no link rewriting - a doc whose repo depth differs from
        # its archive depth has relative links that can only be right in one of
        # the two places.
        by_path = {e['path']: e for e in scaffold.generate_manifest(ROOT)['files']}
        for doc in ('GETTING_STARTED.md', 'CHEATSHEET.md'):
            self.assertIn(doc, by_path, f'{doc} must install at the archive root')
            self.assertEqual(by_path[doc]['category'], 'operating')
            self.assertNotIn('src', by_path[doc],
                             f'{doc} must not be remapped - repo path == install path')
            self.assertNotIn(f'docs/{doc}', by_path,
                             f'{doc} must not also ship under docs/')
            self.assertTrue((ROOT / doc).is_file(),
                            f'{doc} must live at the repo root too')

    def test_no_readme_ships_into_an_archive(self):
        # The repo README is repo-facing: badges, milestone tables, contributing,
        # and links to example-archive/, quickstart-template/, obsidian-templater/
        # - none of which an archive has. GETTING_STARTED.md is the archive's
        # entry point, and its name says what to do with it.
        by_path = {e['path'] for e in scaffold.generate_manifest(ROOT)['files']}
        self.assertNotIn('README.md', by_path)

    def test_skeleton_and_operating_categories(self):
        files = scaffold.generate_manifest(ROOT)['files']
        by_path = {e['path']: e for e in files}
        # Skeleton seeds present, remapped, and carry a src that differs.
        self.assertEqual(by_path['fha.yaml']['category'], 'skeleton')
        self.assertEqual(by_path['fha.yaml']['src'], 'archive-template/fha.yaml')
        self.assertEqual(by_path['places/places.yaml']['category'], 'skeleton')
        # The five BUILD-mandated docs ship: only the machinery (tools/,
        # design/) is vendored under .fha/. The two an owner needs on day one sit
        # at the archive ROOT; the rest of the manual stays in docs/, which stays
        # put so its two-way link graph with the root docs keeps resolving - see
        # test_owner_docs_stay_at_root_and_project_docs_are_vendored.
        for doc in ('GETTING_STARTED.md', 'CHEATSHEET.md',
                    'docs/SETUP_FROM_ZIP.md', 'docs/TROUBLESHOOTING.md',
                    'docs/FILING_CABINET.md'):
            self.assertIn(doc, by_path, doc)
            self.assertEqual(by_path[doc]['category'], 'operating')
        # A vendored operating entry carries a src remap (repo-flat -> archive
        # .fha/); a root-pinned operating entry (SPEC.md) carries none.
        self.assertEqual(by_path['.fha/tools/scaffold.py']['src'], 'tools/scaffold.py')
        self.assertNotIn('src', by_path['SPEC.md'])

    def test_owner_docs_stay_at_root_and_project_docs_are_vendored(self):
        # Owner-facing docs are the manual someone reaches for when something is
        # wrong - exactly when a hidden folder helps least - and they sit in a
        # two-way link graph with the root docs and rulebooks
        # (`GETTING_STARTED.md` -> `docs/FAQ.md`; docs -> `../SPEC.md`), which
        # only survives if both ends stay put. Project docs (the visual-language reference, the
        # roadmap) are not owner material and ride with the machinery instead.
        by_path = {e['path']: e for e in scaffold.generate_manifest(ROOT)['files']}
        doc_paths = [p for p in by_path if p.split('/')[0] == 'docs']
        self.assertTrue(doc_paths, 'docs/ must ship')
        for p in doc_paths:
            self.assertNotIn('src', by_path[p], f'{p} must not be remapped')
        # Exactly the declared project docs are vendored - nothing else drifts in.
        self.assertEqual(
            sorted(p for p in by_path if p.startswith('.fha/docs/')),
            sorted(f'.fha/{d}' for d in scaffold._VENDORED_DOCS),
            'only the declared project docs may live under .fha/docs/')
        # A vendored doc must not also ship at the root, or an archive holds two
        # copies and the owner edits whichever they find first.
        for d in scaffold._VENDORED_DOCS:
            self.assertNotIn(d, by_path, f'{d} must not also install at the root')
        # Both ends of the link graph land where the links expect them.
        self.assertIn('GETTING_STARTED.md', by_path)
        self.assertIn('docs/FAQ.md', by_path)
        # Meanwhile the machinery IS vendored.
        self.assertTrue([p for p in by_path if p.startswith('.fha/tools/')])
        self.assertTrue([p for p in by_path if p.startswith('.fha/design/')])


class InstallTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.repo = _make_fake_repo(self.tmp / 'repo')
        self.archive = self.tmp / 'archive'

    def tearDown(self):
        self._tmp.cleanup()

    def test_install_copies_files_and_stamps(self):
        rc = scaffold.run_install(self.archive, self.repo)
        self.assertEqual(rc, EXIT_CLEAN)
        # operating + skeleton landed, remapped correctly
        self.assertTrue((self.archive / 'SPEC.md').is_file())
        self.assertTrue((self.archive / '.fha' / 'tools' /'atool.py').is_file())
        self.assertTrue((self.archive / 'docs' / 'guide.md').is_file())
        self.assertTrue((self.archive / 'fha.yaml').is_file())
        self.assertTrue((self.archive / 'places' / 'places.yaml').is_file())
        self.assertTrue((self.archive / 'sources' / '.gitkeep').is_file())
        # archive-template/ folder itself is never created in the archive
        self.assertFalse((self.archive / 'archive-template').exists())
        # stamp records every copied file's checksum
        stamp = json.loads((self.archive / '.plaintext-version').read_text(encoding='utf-8'))
        self.assertIn('SPEC.md', stamp['files'])
        self.assertIn('fha.yaml', stamp['files'])
        self.assertEqual(stamp['manifest_version'], scaffold.MANIFEST_VERSION)

    def test_install_dry_run_writes_nothing(self):
        rc = scaffold.run_install(self.archive, self.repo, dry_run=True)
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertFalse(self.archive.exists())

    def test_reinstall_refused(self):
        scaffold.run_install(self.archive, self.repo)
        with self.assertRaises(scaffold.ScaffoldError) as ctx:
            scaffold.run_install(self.archive, self.repo)
        self.assertIn('already', str(ctx.exception).lower())

    def test_missing_source_refused_before_writing(self):
        (self.repo / 'tools' / 'atool.py').unlink()  # manifest still lists it
        with self.assertRaises(scaffold.ScaffoldError) as ctx:
            scaffold.run_install(self.archive, self.repo)
        self.assertIn('missing', str(ctx.exception).lower())
        # nothing half-written
        self.assertFalse(self.archive.exists())

    def test_missing_manifest_is_friendly(self):
        (self.repo / 'manifest.json').unlink()
        with self.assertRaises(scaffold.ScaffoldError) as ctx:
            scaffold.run_install(self.archive, self.repo)
        self.assertIn('manifest.json', str(ctx.exception))

    def test_python_too_old_is_a_hard_stop(self):
        with mock.patch.object(scaffold.sys, 'version_info', (3, 9, 0)):
            rc = scaffold._cmd_install(argparse.Namespace(
                archive_path=str(self.archive), repo=str(self.repo), dry_run=False))
        self.assertEqual(rc, EXIT_FAILURE)
        self.assertFalse(self.archive.exists())

    def test_exiftool_missing_is_only_advisory(self):
        with mock.patch('scaffold.shutil.which', return_value=None):
            rc = scaffold.run_install(self.archive, self.repo)
        self.assertEqual(rc, EXIT_CLEAN)  # install still succeeds
        self.assertTrue((self.archive / 'SPEC.md').is_file())


class ExecBitRepairTest(unittest.TestCase):
    """A chmod is a mutation: it must be previewed and it must be audited."""

    def setUp(self):
        if os.name == 'nt':
            self.skipTest('POSIX permission bits only')
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.repo = _make_fake_repo(self.tmp / 'repo')
        _write(self.repo / 'fha', '#!/bin/sh\necho hi\n')
        (self.repo / 'fha').chmod(0o755)
        scaffold._write_manifest(self.repo)
        self.archive = self.tmp / 'archive'
        with contextlib.redirect_stdout(io.StringIO()):
            scaffold.run_install(self.archive, self.repo)
        # Bytes stay current; only the mode is lost - as a Windows round trip,
        # a zip without Unix modes, or a sync service would leave it.
        (self.archive / 'fha').chmod(0o644)

    def tearDown(self):
        self._tmp.cleanup()

    def test_dry_run_previews_the_repair_and_changes_nothing(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            scaffold.run_update_tools(self.archive, self.repo, dry_run=True)
        self.assertIn('would restore the executable permission', buf.getvalue())
        # And the preview really was a preview.
        self.assertFalse((self.archive / 'fha').stat().st_mode & 0o111,
                         'a dry run must not chmod')

    def test_the_repair_is_recorded_in_changed(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = scaffold.run_update_tools(self.archive, self.repo)
        self.assertTrue((self.archive / 'fha').stat().st_mode & 0o111,
                        'the live run must repair the bit')
        self.assertTrue(
            any(c.endswith('/fha') for c in rc.changed),
            'a repaired launcher is a mutation and belongs in changed')


class FlatArchiveRefusalTest(unittest.TestCase):
    """Both install AND update must refuse an unstamped flat archive.

    update-tools is the command an owner of a hand-copied archive reaches for,
    so the refusal install got is worth little if this one silently vendors a
    second copy and switches the first off.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.repo = _make_fake_repo(self.tmp / 'repo')
        self.archive = self.tmp / 'archive'
        # A hand-copied pre-.fha archive: tools at the root, no stamp.
        _write(self.archive / 'tools' / 'fha.py', 'print("legacy")\n')
        _write(self.archive / 'design' / 'custom.css', '/* my colours */\n')
        _write(self.archive / 'fha.yaml', 'roots: {}\n')

    def tearDown(self):
        self._tmp.cleanup()

    def test_update_refuses_and_writes_nothing(self):
        with self.assertRaises(scaffold.ScaffoldError) as caught:
            with contextlib.redirect_stdout(io.StringIO()):
                scaffold.run_update_tools(self.archive, self.repo)
        msg = str(caught.exception)
        self.assertIn('tools', msg)
        self.assertIn('custom.css', msg, 'the stylesheet is the thing most '
                                         'likely to be lost, so name it')
        # Nothing vendored, nothing stamped, the owner's files untouched.
        self.assertFalse((self.archive / '.fha').exists())
        self.assertFalse((self.archive / '.plaintext-version').exists())
        self.assertEqual(
            (self.archive / 'design' / 'custom.css').read_text(encoding='utf-8'),
            '/* my colours */\n')

    def test_a_stamp_with_no_usable_file_map_is_treated_as_unstamped(self):
        # `{"files": []}` is a perfectly good object that records nothing. A
        # `stamp is None` check waves it past while _plan_update still finds no
        # flat files to retire - so the guard has to ask "does anything record
        # what is on disk?", not "is there a file?".
        for broken in ({'files': []}, {'files': {}}, {'files': 'nope'}, {}):
            with self.subTest(stamp=broken):
                _write(self.archive / '.plaintext-version', json.dumps(broken))
                with self.assertRaises(scaffold.ScaffoldError):
                    with contextlib.redirect_stdout(io.StringIO()):
                        scaffold.run_update_tools(self.archive, self.repo)
                self.assertFalse((self.archive / '.fha').exists())
                self.assertEqual(
                    (self.archive / 'design' / 'custom.css').read_text(
                        encoding='utf-8'), '/* my colours */\n')

    def test_a_stamped_archive_is_unaffected(self):
        # The guard keys on the ABSENCE of a stamp; a normal archive must update.
        shutil.rmtree(self.archive)
        with contextlib.redirect_stdout(io.StringIO()):
            scaffold.run_install(self.archive, self.repo)
            rc = scaffold.run_update_tools(self.archive, self.repo)
        self.assertEqual(rc.exit_code, EXIT_CLEAN)


class ResumeInterruptedInstallTest(unittest.TestCase):
    """An interrupted install must accept the bytes it copied itself."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.repo = _make_fake_repo(self.tmp / 'repo')

    def tearDown(self):
        self._tmp.cleanup()

    def test_resume_accepts_source_bytes_that_differ_from_the_manifest(self):
        # The packaged source differs from the manifest's prediction, as a zip
        # built from repository blobs does for CRLF-pinned launchers.
        manifest = json.loads((self.repo / 'manifest.json').read_text(encoding='utf-8'))
        entry = next(e for e in manifest['files'] if e.get('category') == 'operating')
        src = self.repo / entry.get('src', entry['path'])
        _write(src, 'packaged bytes\n')          # manifest still holds the old sha

        archive = self.tmp / 'archive'
        archive.mkdir()
        # Interrupted: this file was copied, the stamp was never written.
        dest = archive / entry['path']
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)

        with contextlib.redirect_stdout(io.StringIO()):
            rc = scaffold.run_install(archive, self.repo)
        self.assertEqual(rc.exit_code, EXIT_CLEAN,
                         'resuming must not refuse the bytes install copied')


class FlatLayoutRefusalTest(unittest.TestCase):
    """A flat archive is refused whatever its stamp says.

    The case that matters is an archive installed by the PREVIOUS release: a
    perfectly valid populated stamp AND flat tools. Guards keyed on the stamp
    let it through, and the retire-and-add path then performs the layout
    conversion this project decided not to build.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.repo = _make_fake_repo(self.tmp / 'repo')
        self.archive = self.tmp / 'archive'
        _write(self.archive / 'tools' / 'fha.py', 'print("previous release")\n')
        _write(self.archive / 'design' / 'custom.css', '/* my colours */\n')
        _write(self.archive / 'fha.yaml', 'roots: {}\n')

    def tearDown(self):
        self._tmp.cleanup()

    def _stamps(self):
        """Every stamp shape a flat archive can plausibly carry."""
        populated = {'manifest_version': '1', 'files': {
            'tools/fha.py': scaffold._sha256_file(self.archive / 'tools' / 'fha.py'),
            'design/custom.css': 'whatever'}}
        return [None, {}, {'files': []}, {'files': {}}, populated]

    def test_every_stamp_shape_is_refused(self):
        for stamp in self._stamps():
            with self.subTest(stamp='none' if stamp is None else str(stamp)[:30]):
                stamp_file = self.archive / '.plaintext-version'
                if stamp is None:
                    stamp_file.unlink(missing_ok=True)
                else:
                    _write(stamp_file, json.dumps(stamp))
                with self.assertRaises(scaffold.ScaffoldError):
                    with contextlib.redirect_stdout(io.StringIO()):
                        scaffold.run_update_tools(self.archive, self.repo)
                self.assertFalse((self.archive / '.fha').exists())
                self.assertEqual(
                    (self.archive / 'design' / 'custom.css').read_text(
                        encoding='utf-8'), '/* my colours */\n',
                    'the owner styling must never be swapped for stock')


class CrlfLauncherTest(unittest.TestCase):
    """A ZIP carries repository blobs, so .cmd arrives LF whatever git says."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.repo = _make_fake_repo(self.tmp / 'repo')
        (self.repo / 'fha.cmd').write_bytes(b'@echo off\ngoto :run\n:run\n')
        (self.repo / 'serve.cmd').write_bytes(b'@echo off\ngoto :run\n:run\n')
        scaffold._write_manifest(self.repo)

    def tearDown(self):
        self._tmp.cleanup()

    def test_install_normalizes_launchers_to_crlf(self):
        archive = self.tmp / 'archive'
        with contextlib.redirect_stdout(io.StringIO()):
            scaffold.run_install(archive, self.repo)
        for name in ('fha.cmd', 'serve.cmd'):
            raw = (archive / name).read_bytes()
            self.assertIn(b'\r\n', raw, f'{name} must be CRLF in the archive')
            self.assertNotIn(b'\n\n', raw.replace(b'\r\n', b''),
                             f'{name} must not keep bare LF endings')

    def test_the_stamp_records_the_normalized_bytes(self):
        archive = self.tmp / 'archive'
        with contextlib.redirect_stdout(io.StringIO()):
            scaffold.run_install(archive, self.repo)
        stamp = json.loads(
            (archive / '.plaintext-version').read_text(encoding='utf-8'))
        self.assertEqual(stamp['files']['fha.cmd'],
                         scaffold._sha256_file(archive / 'fha.cmd'),
                         'the baseline must match what is really on disk')
        # And a follow-up update must not call the untouched launcher edited.
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            scaffold.run_update_tools(archive, self.repo)
        self.assertNotIn('has been backed up', buf.getvalue())


class SymlinkDestinationTest(unittest.TestCase):
    """Writing through a link changes whatever it points at, possibly outside."""

    def setUp(self):
        if os.name == 'nt':
            self.skipTest('POSIX symlinks')
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.repo = _make_fake_repo(self.tmp / 'repo')
        self.archive = self.tmp / 'archive'
        self.outside = self.tmp / 'SECRET.txt'
        _write(self.outside, 'private\n')

    def tearDown(self):
        self._tmp.cleanup()

    def test_a_symlinked_destination_is_refused(self):
        (self.archive / 'sources').mkdir(parents=True)
        os.symlink(self.outside, self.archive / 'sources' / '.gitkeep')
        with self.assertRaises(scaffold.ScaffoldError) as caught:
            with contextlib.redirect_stdout(io.StringIO()):
                scaffold.run_install(self.archive, self.repo)
        self.assertIn('symbolic link', str(caught.exception))
        self.assertEqual(self.outside.read_text(encoding='utf-8'), 'private\n')

    def test_a_symlinked_ANCESTOR_is_refused(self):
        # The subtler half: nothing is a link at the destination itself, but a
        # parent directory puts every path beneath it somewhere else.
        elsewhere = self.tmp / 'elsewhere'
        elsewhere.mkdir()
        self.archive.mkdir()
        os.symlink(elsewhere, self.archive / 'sources')
        with self.assertRaises(scaffold.ScaffoldError) as caught:
            with contextlib.redirect_stdout(io.StringIO()):
                scaffold.run_install(self.archive, self.repo)
        self.assertIn('symbolic link', str(caught.exception))
        self.assertFalse(list(elsewhere.iterdir()), 'nothing may be written there')

    def test_update_refuses_too(self):
        with contextlib.redirect_stdout(io.StringIO()):
            scaffold.run_install(self.archive, self.repo)
        target = self.archive / 'docs' / 'guide.md'
        target.unlink()
        os.symlink(self.outside, target)
        with self.assertRaises(scaffold.ScaffoldError):
            with contextlib.redirect_stdout(io.StringIO()):
                scaffold.run_update_tools(self.archive, self.repo)
        self.assertEqual(self.outside.read_text(encoding='utf-8'), 'private\n')


class CrlfFailureIsReportedTest(unittest.TestCase):
    """A launcher that cannot be normalized is a failure, not a shrug.

    Mock-driven: the real trigger is a read-only destination, which is a no-op
    for root - so a permission-based test would silently skip in exactly the
    environments most likely to run as root.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.repo = _make_fake_repo(self.tmp / 'repo')
        (self.repo / 'fha.cmd').write_bytes(b'@echo off\ngoto :r\n:r\n')
        scaffold._write_manifest(self.repo)

    def tearDown(self):
        self._tmp.cleanup()

    def test_install_surfaces_a_normalization_failure(self):
        with mock.patch.object(scaffold, '_normalize_crlf',
                               side_effect=OSError(13, 'Permission denied')):
            with self.assertRaises(scaffold.ScaffoldError):
                with contextlib.redirect_stdout(io.StringIO()):
                    scaffold.run_install(self.tmp / 'archive', self.repo)

    def test_update_reports_it_and_keeps_the_old_launcher(self):
        archive = self.tmp / 'archive'
        with contextlib.redirect_stdout(io.StringIO()):
            scaffold.run_install(archive, self.repo)
        before = (archive / 'fha.cmd').read_bytes()
        (self.repo / 'fha.cmd').write_bytes(b'@echo off\ngoto :new\n:new\n')
        scaffold._write_manifest(self.repo)

        buf = io.StringIO()
        with mock.patch.object(scaffold, '_normalize_crlf',
                               side_effect=OSError(13, 'Permission denied')):
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                rc = scaffold.run_update_tools(archive, self.repo)
        self.assertEqual(rc.exit_code, EXIT_WARNINGS)
        self.assertIn('fha.cmd', str(rc.data.get('failures', '')))
        # Normalized before the swap, so a failure leaves the OLD launcher whole
        # rather than a half-right new one.
        self.assertEqual((archive / 'fha.cmd').read_bytes(), before)


class DuplicateManifestPathTest(unittest.TestCase):
    """One archive file, one source, one lifecycle."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.repo = _make_fake_repo(self.tmp / 'repo')

    def tearDown(self):
        self._tmp.cleanup()

    def test_a_repeated_destination_is_refused(self):
        manifest = json.loads((self.repo / 'manifest.json').read_text(encoding='utf-8'))
        dupe = dict(manifest['files'][0])
        dupe['src'] = 'docs/guide.md'
        manifest['files'].append(dupe)
        _write(self.repo / 'manifest.json', json.dumps(manifest))
        with self.assertRaises(scaffold.ScaffoldError) as caught:
            scaffold.load_manifest(self.repo)
        self.assertIn('twice', str(caught.exception))


class DirectoryDestinationTest(unittest.TestCase):
    """A contained path can still name the human's records folder.

    `people` is contained, ordinary-looking, and is where the genealogy lives.
    Treated as a file it gets moved wholesale into .plaintext-backup/ and
    replaced - the records being the collateral of a damaged packing list.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.repo = _make_fake_repo(self.tmp / 'repo')
        self.archive = self.tmp / 'archive'
        with contextlib.redirect_stdout(io.StringIO()):
            scaffold.run_install(self.archive, self.repo)
        # The owner's records.
        _write(self.archive / 'people' / 'P-abcd.md', '# Margaret\n')
        manifest = json.loads((self.repo / 'manifest.json').read_text(encoding='utf-8'))
        manifest['files'].append({'path': 'people', 'category': 'operating',
                                  'src': 'tools/atool.py', 'sha256': 'x'})
        _write(self.repo / 'manifest.json', json.dumps(manifest))

    def tearDown(self):
        self._tmp.cleanup()

    def test_update_refuses_and_the_records_survive(self):
        with self.assertRaises(scaffold.ScaffoldError) as caught:
            with contextlib.redirect_stdout(io.StringIO()):
                scaffold.run_update_tools(self.archive, self.repo)
        self.assertIn('people', str(caught.exception))
        self.assertTrue((self.archive / 'people').is_dir())
        self.assertEqual(
            (self.archive / 'people' / 'P-abcd.md').read_text(encoding='utf-8'),
            '# Margaret\n')
        self.assertFalse((self.archive / '.plaintext-backup').exists(),
                         'nothing may be moved aside before the refusal')

    def test_install_refuses_too(self):
        fresh = self.tmp / 'fresh'
        (fresh / 'people').mkdir(parents=True)
        _write(fresh / 'people' / 'P-abcd.md', '# Margaret\n')
        with self.assertRaises(scaffold.ScaffoldError):
            with contextlib.redirect_stdout(io.StringIO()):
                scaffold.run_install(fresh, self.repo)
        self.assertEqual(
            (fresh / 'people' / 'P-abcd.md').read_text(encoding='utf-8'),
            '# Margaret\n')


class ManifestCategoryTest(unittest.TestCase):
    """An unrecognized category installs and is then never managed again."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.repo = _make_fake_repo(self.tmp / 'repo')
        self.pristine = (self.repo / 'manifest.json').read_text(encoding='utf-8')

    def tearDown(self):
        self._tmp.cleanup()

    def test_missing_or_unknown_category_is_refused(self):
        for value in (None, 'operatng', 'data', ''):
            with self.subTest(category=value):
                manifest = json.loads(self.pristine)
                if value is None:
                    manifest['files'][0].pop('category', None)
                else:
                    manifest['files'][0]['category'] = value
                _write(self.repo / 'manifest.json', json.dumps(manifest))
                with self.assertRaises(scaffold.ScaffoldError) as caught:
                    scaffold.load_manifest(self.repo)
                self.assertIn('category', str(caught.exception))

    def test_the_real_manifest_still_loads(self):
        self.assertTrue(scaffold.load_manifest(ROOT)['files'])


class ManifestContainmentTest(unittest.TestCase):
    """A manifest is a downloaded file; it must not be able to reach outside."""

    ESCAPES = ('../outside.txt', 'a/../../b', '/etc/passwd', 'C:\\Windows\\x',
               '\\\\server\\share\\x', 'tools\\fha.py', '..')

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.repo = _make_fake_repo(self.tmp / 'repo')
        self.pristine = (self.repo / 'manifest.json').read_text(encoding='utf-8')

    def tearDown(self):
        self._tmp.cleanup()

    def test_escaping_paths_are_refused(self):
        for field in ('path', 'src'):
            for bad in self.ESCAPES:
                with self.subTest(field=field, value=bad):
                    _write(self.repo / 'manifest.json', self.pristine)
                    manifest = json.loads(self.pristine)
                    manifest['files'][0][field] = bad
                    _write(self.repo / 'manifest.json', json.dumps(manifest))
                    with self.assertRaises(scaffold.ScaffoldError):
                        scaffold.load_manifest(self.repo)

    def test_ordinary_paths_still_load(self):
        # The guard must not refuse the manifest the project actually ships.
        manifest = scaffold.load_manifest(self.repo)
        self.assertTrue(manifest['files'])
        real = scaffold.load_manifest(ROOT)
        self.assertTrue(real['files'])

    def test_install_cannot_write_outside_the_archive(self):
        victim = self.tmp / 'VICTIM.txt'
        _write(victim, 'do not touch\n')
        manifest = json.loads(self.pristine)
        manifest['files'].append({'path': '../VICTIM.txt', 'category': 'operating',
                                  'src': 'tools/atool.py', 'sha256': 'x'})
        _write(self.repo / 'manifest.json', json.dumps(manifest))
        with self.assertRaises(scaffold.ScaffoldError):
            with contextlib.redirect_stdout(io.StringIO()):
                scaffold.run_install(self.tmp / 'archive', self.repo)
        self.assertEqual(victim.read_text(encoding='utf-8'), 'do not touch\n')


class InstallBaselineTest(unittest.TestCase):
    """The stamp must record the bytes that actually landed."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.repo = _make_fake_repo(self.tmp / 'repo')

    def tearDown(self):
        self._tmp.cleanup()

    def test_baseline_is_the_installed_bytes_not_the_manifest_prediction(self):
        # A package built from the repository blobs can carry different bytes
        # than the checkout the manifest was generated in - LF vs CRLF launchers
        # being the case in hand. Trusting the manifest's prediction makes the
        # next release that touches the file call an untouched copy "customized".
        scaffold._write_manifest(self.repo)
        manifest = json.loads((self.repo / 'manifest.json').read_text(encoding='utf-8'))
        target = next(e for e in manifest['files']
                      if e.get('category') == 'operating')
        # Source on disk differs from what the manifest recorded, as a
        # differently-packaged download would.
        src = self.repo / target.get('src', target['path'])
        _write(src, 'packaged differently\n')
        _write(self.repo / 'manifest.json', json.dumps(manifest))

        archive = self.tmp / 'archive'
        with contextlib.redirect_stdout(io.StringIO()):
            scaffold.run_install(archive, self.repo)

        stamp = json.loads(
            (archive / '.plaintext-version').read_text(encoding='utf-8'))
        on_disk = scaffold._sha256_file(archive / target['path'])
        self.assertEqual(
            stamp['files'][target['path']], on_disk,
            'the recorded baseline must be the installed bytes')
        self.assertNotEqual(
            stamp['files'][target['path']], target['sha256'],
            'and NOT the manifest prediction when the two differ')

        # The consequence that matters: the next update sees no customization.
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            scaffold.run_update_tools(archive, self.repo)
        self.assertNotIn('has been backed up', buf.getvalue(),
                         'an untouched file must never be reported as edited')


class InstallUnreadableTargetTest(unittest.TestCase):
    """An unreadable file must refuse plainly, not raise out of the CLI.

    Driven with a mocked read failure rather than chmod 0o000: the real
    permission trick is a no-op for root, so a privilege-dependent test would
    silently skip in exactly the environments most likely to run as root - and a
    test that does not run is not a guard.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.repo = _make_fake_repo(self.tmp / 'repo')

    def tearDown(self):
        self._tmp.cleanup()

    def test_unreadable_target_file_is_a_plain_refusal(self):
        archive = self.tmp / 'archive'
        archive.mkdir()
        _write(archive / 'SPEC.md', 'mine\n')

        real_sha = scaffold._sha256_file

        def unreadable(path, *a, **kw):
            if Path(path).name == 'SPEC.md' and archive in Path(path).parents:
                raise OSError(13, 'Permission denied')
            return real_sha(path, *a, **kw)

        with mock.patch.object(scaffold, '_sha256_file', side_effect=unreadable):
            with self.assertRaises(scaffold.ScaffoldError) as caught:
                scaffold.run_install(archive, self.repo)
        # A ScaffoldError is what _cmd_install knows how to print; an OSError
        # escaping here is the traceback this guard exists to prevent.
        self.assertIn('SPEC.md', str(caught.exception))
        self.assertIn('permission', str(caught.exception).lower())


class ZipWorkshopLauncherTest(unittest.TestCase):
    """A workshop unzipped from a download has no Unix modes at all.

    Deriving "should this be executable?" from the source copy's mode then
    concludes no, installs a launcher nobody can run, and leaves the repair pass
    agreeing there is nothing to repair - so `./fha` fails with "Permission
    denied" and no amount of update-tools fixes it. The requirement belongs to
    the archive contract, not to whichever copy of the repo we install from.
    """

    def setUp(self):
        if os.name == 'nt':
            self.skipTest('POSIX permission bits only')
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.repo = _make_fake_repo(self.tmp / 'repo')
        _write(self.repo / 'fha', '#!/bin/sh\necho hi\n')
        (self.repo / 'fha').chmod(0o644)      # as a zip extraction leaves it
        scaffold._write_manifest(self.repo)

    def tearDown(self):
        self._tmp.cleanup()

    def test_install_from_a_zip_workshop_yields_a_runnable_launcher(self):
        archive = self.tmp / 'archive'
        with contextlib.redirect_stdout(io.StringIO()):
            scaffold.run_install(archive, self.repo)
        self.assertTrue((archive / 'fha').stat().st_mode & 0o111,
                        'the installed launcher must be runnable even when the '
                        'source copy carries no execute bit')

    def test_update_repairs_it_even_when_the_source_lacks_the_bit(self):
        archive = self.tmp / 'archive'
        with contextlib.redirect_stdout(io.StringIO()):
            scaffold.run_install(archive, self.repo)
        (archive / 'fha').chmod(0o644)
        with contextlib.redirect_stdout(io.StringIO()):
            scaffold.run_update_tools(archive, self.repo)
        self.assertTrue((archive / 'fha').stat().st_mode & 0o111,
                        'the repair pass must not take the zip source as '
                        'authority on whether the launcher is executable')


class PipCommandTest(unittest.TestCase):
    """The recovery command must run as printed, for the interpreter it names."""

    def test_a_spaced_interpreter_path_is_quoted(self):
        import _lib
        with mock.patch.object(_lib.sys, 'executable', '/home/u/Family Tools/python'):
            cmd = _lib.pip_command('pyyaml')
        # Pasteable: the shell must see one argument, not two.
        self.assertEqual(shlex.split(cmd)[0], '/home/u/Family Tools/python')
        self.assertEqual(shlex.split(cmd)[1:], ['-m', 'pip', 'install', 'pyyaml'])

    def test_a_spaced_requirements_path_is_quoted_too(self):
        # Quoting the interpreter but not the `-r` argument just moves the split
        # one argument to the right - and an archive called "Family Archive" is
        # an entirely ordinary thing to have.
        import _lib
        with mock.patch.object(_lib.sys, 'executable', '/home/u/py env/bin/python'):
            cmd = _lib.pip_command('-r /home/u/Family Archive/.fha/tools/requirements.txt')
        self.assertEqual(
            shlex.split(cmd),
            ['/home/u/py env/bin/python', '-m', 'pip', 'install', '-r',
             '/home/u/Family Archive/.fha/tools/requirements.txt'])

    def test_it_names_the_running_interpreter(self):
        # pip_command quotes platform-appropriately (POSIX single quotes,
        # Windows double quotes), so unwrap whichever style arrived rather than
        # assert one of them - asserting shlex.quote() here fails on any
        # Windows interpreter installed under a spaced path
        # (C:\Program Files\...), where double quotes are correct.
        import _lib
        cmd = _lib.pip_command('pyyaml')
        head, sep, _rest = cmd.partition(' -m pip install ')
        self.assertTrue(sep, f'unexpected shape: {cmd}')
        self.assertEqual(head.strip('\'"'), _lib.sys.executable)


class InstallTemplateHandCopyTest(unittest.TestCase):
    """The documented zip on-ramp: copy archive-template/, then run install.

    SETUP_FROM_ZIP.md tells people to do exactly this, so install's preflight has
    to accept bytes that are already sitting at a destination when they are
    pristine stock - the permissive half of `_acceptable` - while still refusing
    bytes the owner has started editing. Without a test here, tightening that set
    passes the whole suite while silently blocking every hand-copy user.

    (`_acceptable`'s third branch - "what the TEMPLATE ships at this path" -
    existed for README.md, the one operating file whose archive-template
    counterpart differed by design. README.md no longer ships, and any other
    archive-template file at an operating path would be a duplicate manifest
    entry, so that branch now only ever agrees with the source branch. It is left
    in place as the guard for the hand-copy path; nothing currently exercises it
    on its own.)
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.repo = _make_fake_repo(self.tmp / 'repo')

    def tearDown(self):
        self._tmp.cleanup()

    def test_install_accepts_a_pristine_hand_copied_template(self):
        archive = self.tmp / 'archive'
        archive.mkdir()
        # What the guide tells the user to do: copy the template across first.
        for rel in ('fha.yaml', 'places/places.yaml'):
            _write(archive / rel,
                   (self.repo / 'archive-template' / rel).read_text(encoding='utf-8'))

        with contextlib.redirect_stdout(io.StringIO()):
            rc = scaffold.run_install(archive, self.repo)

        self.assertEqual(rc.exit_code, EXIT_CLEAN)
        # The operating layer landed alongside the copy, and the stamp knows it.
        self.assertTrue((archive / 'SPEC.md').is_file())
        stamp = json.loads(
            (archive / '.plaintext-version').read_text(encoding='utf-8'))
        self.assertIn('fha.yaml', stamp['files'])

    def test_install_still_refuses_an_edited_operating_file(self):
        # The refusal side must survive the permissive branch: bytes that are
        # neither stock nor the template are the owner's own work.
        archive = self.tmp / 'archive'
        archive.mkdir()
        _write(archive / 'SPEC.md', '# my own notes, do not clobber\n')
        with self.assertRaises(scaffold.ScaffoldError) as caught:
            scaffold.run_install(archive, self.repo)
        self.assertIn('SPEC.md', str(caught.exception))


class UpdateToolsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.repo = _make_fake_repo(self.tmp / 'repo')
        self.archive = self.tmp / 'archive'
        scaffold.run_install(self.archive, self.repo)

    def tearDown(self):
        self._tmp.cleanup()

    def _stamp(self):
        return json.loads((self.archive / '.plaintext-version').read_text(encoding='utf-8'))

    def test_malformed_stamp_files_value_does_not_traceback(self):
        # A hand-mangled stamp whose `files` is valid JSON but not an object used
        # to raise AttributeError from .items() mid-run.
        stamp = self._stamp()
        stamp['files'] = 'not-an-object'
        _write(self.archive / '.plaintext-version', json.dumps(stamp))
        with contextlib.redirect_stdout(io.StringIO()):
            rc = scaffold.run_update_tools(self.archive, self.repo)
        self.assertIn(rc.exit_code, (EXIT_CLEAN, EXIT_WARNINGS))
        self.assertIsInstance(self._stamp()['files'], dict)

    def test_manifest_entry_with_a_null_field_refuses_plainly(self):
        # `path` alone is not the whole contract: repo_root / None is a
        # TypeError traceback mid-preflight rather than a refusal. An explicit
        # null is the case that matters - .get(k, fallback) returns None when the
        # key is THERE holding a null, so "absent" and "null" are not the same.
        pristine = (self.repo / 'manifest.json').read_text(encoding='utf-8')
        for field in ('src', 'category', 'sha256'):
            with self.subTest(field=field):
                _write(self.repo / 'manifest.json', pristine)
                manifest = json.loads(pristine)
                manifest['files'][0][field] = None
                _write(self.repo / 'manifest.json', json.dumps(manifest))
                with self.assertRaises(scaffold.ScaffoldError) as caught:
                    scaffold.load_manifest(self.repo)
                self.assertIn(field, str(caught.exception))

    def test_noop_when_current(self):
        rc = scaffold.run_update_tools(self.archive, self.repo)
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertFalse((self.archive / '.plaintext-backup').exists())

    def test_stock_change_overwrites_silently(self):
        # Upstream improved atool.py; the archive's copy is still pristine.
        _write(self.repo / 'tools' / 'atool.py', 'print("a tool v2")\n')
        scaffold._write_manifest(self.repo)
        rc = scaffold.run_update_tools(self.archive, self.repo)
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertEqual((self.archive / '.fha' / 'tools' /'atool.py').read_text(encoding='utf-8'),
                         'print("a tool v2")\n')
        self.assertFalse((self.archive / '.plaintext-backup').exists())
        # stamp now records the new checksum
        self.assertEqual(self._stamp()['files']['.fha/tools/atool.py'],
                         scaffold._sha256_file(self.repo / 'tools' / 'atool.py'))

    def test_customized_file_is_backed_up_then_updated(self):
        # Archive owner edited their atool.py; upstream also moved on.
        _write(self.archive / '.fha' / 'tools' /'atool.py', 'print("MY EDIT")\n')
        _write(self.repo / 'tools' / 'atool.py', 'print("a tool v2")\n')
        scaffold._write_manifest(self.repo)
        rc = scaffold.run_update_tools(self.archive, self.repo)
        self.assertEqual(rc, EXIT_CLEAN)
        # live file is the new stock
        self.assertEqual((self.archive / '.fha' / 'tools' /'atool.py').read_text(encoding='utf-8'),
                         'print("a tool v2")\n')
        # the edit survives in the backup
        backups = list((self.archive / '.plaintext-backup').rglob('atool.py'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding='utf-8'), 'print("MY EDIT")\n')

    def test_skeleton_files_are_never_touched(self):
        # The critical safety property: a user's fha.yaml/places.yaml is data, not
        # operating layer - update-tools must leave it exactly as-is, even if
        # upstream's template changed.
        _write(self.archive / 'fha.yaml', 'roots:\n  photos: D:/MyPhotos\n')
        _write(self.archive / 'places' / 'places.yaml', '- id: L-abc\n  name: MyTown\n')
        _write(self.repo / 'archive-template' / 'fha.yaml', 'roots:\n  photos: changed\n')
        scaffold._write_manifest(self.repo)
        scaffold.run_update_tools(self.archive, self.repo)
        self.assertIn('MyPhotos', (self.archive / 'fha.yaml').read_text(encoding='utf-8'))
        self.assertIn('MyTown', (self.archive / 'places' / 'places.yaml').read_text(encoding='utf-8'))
        # never even backed up
        self.assertFalse((self.archive / '.plaintext-backup').exists())

    def test_retired_file_moved_to_backup(self):
        # Inject a recorded tool that the manifest no longer lists.
        retired = self.archive / '.fha' / 'tools' /'oldtool.py'
        _write(retired, 'print("old")\n')
        stamp = self._stamp()
        stamp['files']['.fha/tools/oldtool.py'] = 'deadbeef'
        _write(self.archive / '.plaintext-version', json.dumps(stamp))
        rc = scaffold.run_update_tools(self.archive, self.repo)
        self.assertEqual(rc, EXIT_CLEAN)
        self.assertFalse(retired.exists())
        moved = list((self.archive / '.plaintext-backup').rglob('oldtool.py'))
        self.assertEqual(len(moved), 1)
        # and it's dropped from the refreshed stamp
        self.assertNotIn('.fha/tools/oldtool.py', self._stamp()['files'])

    def test_added_file_copied_in(self):
        _write(self.repo / 'tools' / 'newtool.py', 'print("new")\n')
        scaffold._write_manifest(self.repo)
        scaffold.run_update_tools(self.archive, self.repo)
        self.assertTrue((self.archive / '.fha' / 'tools' /'newtool.py').is_file())
        self.assertIn('.fha/tools/newtool.py', self._stamp()['files'])

    def test_dry_run_writes_nothing(self):
        _write(self.archive / '.fha' / 'tools' /'atool.py', 'print("MY EDIT")\n')
        _write(self.repo / 'tools' / 'atool.py', 'print("v2")\n')
        scaffold._write_manifest(self.repo)
        rc = scaffold.run_update_tools(self.archive, self.repo, dry_run=True)
        self.assertEqual(rc, EXIT_CLEAN)
        # the customized file is left exactly as the user had it; no backup made
        self.assertEqual((self.archive / '.fha' / 'tools' /'atool.py').read_text(encoding='utf-8'),
                         'print("MY EDIT")\n')
        self.assertFalse((self.archive / '.plaintext-backup').exists())

    def test_broken_clone_refused_before_any_mutation(self):
        # A file the manifest lists but the clone no longer ships must abort the
        # update before anything is copied or backed up.
        _write(self.archive / '.fha' / 'tools' /'atool.py', 'print("MY EDIT")\n')  # would be customized
        _write(self.repo / 'tools' / 'newtool.py', 'print("new")\n')
        scaffold._write_manifest(self.repo)            # manifest now lists newtool.py
        (self.repo / 'tools' / 'newtool.py').unlink()  # ...then the source vanishes
        with self.assertRaises(scaffold.ScaffoldError) as ctx:
            scaffold.run_update_tools(self.archive, self.repo)
        self.assertIn('missing', str(ctx.exception).lower())
        # the customized file was NOT moved to backup
        self.assertFalse((self.archive / '.plaintext-backup').exists())
        self.assertEqual((self.archive / '.fha' / 'tools' /'atool.py').read_text(encoding='utf-8'),
                         'print("MY EDIT")\n')

    def test_partial_failure_does_not_claim_success(self):
        # A per-file failure must not produce a success message or inflate the
        # summary counts (the output stays honest), and must surface the failure.
        _write(self.archive / '.fha' / 'tools' /'atool.py', 'print("MY EDIT")\n')  # customized
        _write(self.repo / 'tools' / 'atool.py', 'print("v2")\n')
        scaffold._write_manifest(self.repo)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch('scaffold.shutil.move', side_effect=OSError('locked')):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = scaffold.run_update_tools(self.archive, self.repo)
        self.assertEqual(rc, EXIT_WARNINGS)
        self.assertNotIn('has been backed up', out.getvalue())          # no false success
        self.assertIn('Done: 0 added, 0 updated, 0 backed up', out.getvalue())  # honest counts
        self.assertIn('could not be updated', err.getvalue())           # failure surfaced
        # the edit is intact (the move failed before touching it); nothing backed up
        self.assertEqual((self.archive / '.fha' / 'tools' /'atool.py').read_text(encoding='utf-8'),
                         'print("MY EDIT")\n')
        self.assertEqual(list((self.archive / '.plaintext-backup').rglob('atool.py')), [])

    def test_failed_update_keeps_edit_safe_on_retry(self):
        # Regression: a failed customized-file update must NOT record the edited
        # bytes as the installed baseline. If it did, the retry would see
        # disk == recorded, classify the file as pristine stock, and silently
        # overwrite the human's edit with no backup (data loss).
        _write(self.archive / '.fha' / 'tools' /'atool.py', 'print("MY EDIT")\n')
        _write(self.repo / 'tools' / 'atool.py', 'print("v2")\n')
        scaffold._write_manifest(self.repo)
        # Run 1: move fails.
        with mock.patch('scaffold.shutil.move', side_effect=OSError('locked')):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(scaffold.run_update_tools(self.archive, self.repo), EXIT_WARNINGS)
        self.assertEqual((self.archive / '.fha' / 'tools' /'atool.py').read_text(encoding='utf-8'),
                         'print("MY EDIT")\n')
        # Run 2: move works. The edit must be BACKED UP, not silently overwritten.
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(scaffold.run_update_tools(self.archive, self.repo), EXIT_CLEAN)
        self.assertEqual((self.archive / '.fha' / 'tools' /'atool.py').read_text(encoding='utf-8'),
                         'print("v2")\n')
        backups = list((self.archive / '.plaintext-backup').rglob('atool.py'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding='utf-8'), 'print("MY EDIT")\n')

    def test_failed_retired_move_is_retried_next_run(self):
        # A retired file whose move fails must stay recorded so the next run
        # re-detects and retries it (a successful move drops it from the stamp).
        retired = self.archive / '.fha' / 'tools' /'oldtool.py'
        _write(retired, 'print("old")\n')
        stamp = json.loads((self.archive / '.plaintext-version').read_text(encoding='utf-8'))
        stamp['files']['.fha/tools/oldtool.py'] = 'deadbeef'
        _write(self.archive / '.plaintext-version', json.dumps(stamp))
        with mock.patch('scaffold.shutil.move', side_effect=OSError('locked')):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(scaffold.run_update_tools(self.archive, self.repo), EXIT_WARNINGS)
        self.assertTrue(retired.exists())  # move failed, file still there
        # still recorded → still detectable as retired
        new_stamp = json.loads((self.archive / '.plaintext-version').read_text(encoding='utf-8'))
        self.assertIn('.fha/tools/oldtool.py', new_stamp['files'])
        # retry succeeds and moves it to backup
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(scaffold.run_update_tools(self.archive, self.repo), EXIT_CLEAN)
        self.assertFalse(retired.exists())
        self.assertEqual(len(list((self.archive / '.plaintext-backup').rglob('oldtool.py'))), 1)

    def test_no_version_stamp_treats_existing_as_customized(self):
        # An archive whose tools were hand-copied (no install) still must not lose
        # a hand-edit on update.
        (self.archive / '.plaintext-version').unlink()
        _write(self.archive / '.fha' / 'tools' /'atool.py', 'print("HAND EDIT")\n')
        scaffold.run_update_tools(self.archive, self.repo)
        backups = list((self.archive / '.plaintext-backup').rglob('atool.py'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding='utf-8'), 'print("HAND EDIT")\n')


class CmdErrorPathTest(unittest.TestCase):
    """The argparse bridges return friendly exit codes, never tracebacks."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.repo = _make_fake_repo(self.tmp / 'repo')

    def tearDown(self):
        self._tmp.cleanup()

    def test_update_missing_repo(self):
        rc = scaffold._cmd_update_tools(argparse.Namespace(
            repo=None, root=str(self.tmp), dry_run=False, verbose=False))
        self.assertEqual(rc, EXIT_FAILURE)

    def test_update_not_an_archive(self):
        # root points at a folder with no fha.yaml; no auto-detect either.
        empty = self.tmp / 'not-an-archive'
        empty.mkdir()
        # find_archive_root walks up from CWD; force the no-root branch by passing
        # a root that exists but isn't an archive - _cmd uses the explicit root, so
        # update runs and fails to find a manifest? No: it runs against that root.
        # Instead drop --root and patch find_archive_root to None.
        with mock.patch('scaffold.find_archive_root', return_value=None):
            rc = scaffold._cmd_update_tools(argparse.Namespace(
                repo=str(self.repo), root=None, dry_run=False, verbose=False))
        self.assertEqual(rc, EXIT_FAILURE)

    def test_update_explicit_root_must_be_an_archive(self):
        # A mistyped --root (a real folder, but not an archive) must be refused
        # before any operating-layer file is written into it.
        not_arch = self.tmp / 'not-an-archive'
        not_arch.mkdir()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = scaffold._cmd_update_tools(argparse.Namespace(
                repo=str(self.repo), root=str(not_arch), dry_run=False, verbose=False))
        self.assertEqual(rc, EXIT_FAILURE)
        self.assertIn('does not look like an archive', err.getvalue())
        self.assertEqual(list(not_arch.iterdir()), [])  # nothing written

    def test_install_bad_repo_is_friendly_exit(self):
        rc = scaffold._cmd_install(argparse.Namespace(
            archive_path=str(self.tmp / 'arch'),
            repo=str(self.tmp / 'no-such-repo'), dry_run=False))
        self.assertEqual(rc, EXIT_FAILURE)


def _make_flat_archive(root: Path, *, with_stamp: bool = True) -> Path:
    """A pre-.fha archive: tools/, docs/, design/ at the root + a flat stamp.

    Note `serve.cmd`: a real pre-.fha archive's launcher names `tools\\fha.py`
    directly and knows nothing about a vendor folder, and `fha`/`fha.cmd` may not
    exist at all - which is exactly what moving tools/ breaks.
    """
    _write(root / 'fha.yaml', 'roots:\n  photos: photos\n  documents: documents\n')
    _write(root / 'SPEC.md', '# SPEC\n')          # root rulebook, stays put
    _write(root / 'CLAUDE.md', '# CLAUDE\n')      # root rulebook, stays put
    _write(root / 'serve.cmd', '@echo off\npy -3 tools\\fha.py serve %*\n')  # stale
    _write(root / 'tools' / 'fha.py', 'print("fha")\n')
    _write(root / 'tools' / 'sub' / 'x.py', 'x = 1\n')
    _write(root / 'docs' / 'guide.md', '# guide\n')        # stays at the root
    _write(root / 'design' / 'styles.css', 'body{}\n')      # vendored (in stamp)
    _write(root / 'design' / 'custom.css', '/* mine */\n')  # skeleton (NOT in stamp)
    _write(root / '.claude' / 'skills' / 's.md', '# skill\n')  # stays at root
    _write(root / 'people' / '.gitkeep', '')                # a record, untouched
    if with_stamp:
        stamp = {
            'manifest_version': '1', 'spec_version': '1.2',
            'installed': '2026-01-01T00:00:00',
            'files': {
                'tools/fha.py': 'aaa', 'tools/sub/x.py': 'bbb',
                'docs/guide.md': 'ccc', 'design/styles.css': 'ddd',
                'SPEC.md': 'eee', '.claude/skills/s.md': 'fff',
            },
        }
        _write(root / '.plaintext-version', json.dumps(stamp))
    return root


def _make_launcher_repo(root: Path) -> Path:
    """A stand-in workshop holding just the current root launchers."""
    _write(root / 'serve.cmd', '@echo off\nif exist ".fha\\tools\\fha.py" (echo new)\n')
    _write(root / 'fha.cmd', '@echo off\nrem .fha probe\n')
    _write(root / 'fha', '#!/bin/sh\n# .fha probe\n')
    return root


def _scandir_denying(unreadable: Path):
    """An os.scandir stand-in that refuses to list `unreadable`.

    Injected one level below `os.walk`, which is also where pathlib's `rglob`
    reaches the disk - so the same fault reproduces the pre-fix behaviour (the
    folder simply looks empty) and exercises the post-fix `onerror` seam.
    chmod is no use: CI runs as root, and Windows has no equivalent.
    """
    real_scandir = os.scandir
    target = unreadable.resolve()

    def scandir(path='.'):
        try:
            denied = Path(path).resolve() == target
        except (TypeError, ValueError, OSError):
            denied = False
        if denied:
            err = PermissionError(13, 'Permission denied')
            err.filename = str(path)
            raise err
        return real_scandir(path)

    return scandir


class ManifestCoverageTest(unittest.TestCase):
    """A packing list drawn from a walk that skipped a folder is worse than none.

    `update-tools` reads a file the manifest does not name as retired
    upstream and moves the installed copy aside, so a short manifest does not
    merely ship a smaller download - it eventually takes those tools out of
    every archive that updates, from a run that reported success."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name) / 'repo'
        _make_fake_repo(self.repo)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_generate_manifest_refuses_and_names_the_folder(self) -> None:
        with mock.patch('os.scandir', new=_scandir_denying(self.repo / 'tools')):
            with self.assertRaises(scaffold.ScaffoldError) as caught:
                scaffold.generate_manifest(self.repo)
        message = str(caught.exception)
        self.assertIn('tools', message)
        self.assertIn('was NOT written', message)
        self.assertIn('write-manifest', message)

    def test_the_manifest_file_is_left_as_it_was(self) -> None:
        before = (self.repo / 'manifest.json').read_text(encoding='utf-8')
        with mock.patch('os.scandir', new=_scandir_denying(self.repo / 'tools')):
            with self.assertRaises(scaffold.ScaffoldError):
                scaffold._write_manifest(self.repo)
        self.assertEqual(
            (self.repo / 'manifest.json').read_text(encoding='utf-8'), before)

    def test_a_pytest_cache_left_by_a_dev_session_is_not_packaged(self) -> None:
        # `.pytest_cache/` is gitignored, so it never reached the committed
        # manifest - but the walk found it, and `write-manifest` run on a
        # machine that had run the suite added those files to the packing
        # list. A packing list must not depend on who generated it.
        _write(self.repo / 'tools' / '.pytest_cache' / 'CACHEDIR.TAG', 'junk\n')
        paths = {e['path'] for e in scaffold.generate_manifest(self.repo)['files']}
        self.assertFalse([q for q in paths if '.pytest_cache' in q], sorted(paths))

    def test_the_cli_prints_a_plain_message_and_exits_3(self) -> None:
        args = argparse.Namespace(repo=str(self.repo))
        err = io.StringIO()
        with mock.patch('os.scandir', new=_scandir_denying(self.repo / 'tools')):
            with contextlib.redirect_stderr(err):
                code = scaffold._cmd_write_manifest(args)
        self.assertEqual(code, EXIT_FAILURE)
        self.assertIn('ERROR:', err.getvalue())
        self.assertNotIn('Traceback', err.getvalue())


if __name__ == '__main__':
    unittest.main()
