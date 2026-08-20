"""
test_tools_utf8_stdout.py - issue #64: every tool that prints non-ASCII text
must call `_lib.configure_utf8_stdout()` so a Windows cp1252 console/redirect
default cannot mis-encode it (the sibling bug to the `fha doctor` fix in
PR #60, where `subprocess.run(..., text=True)` without `encoding='utf-8'`
turned `fötos/` into `fÃ¶tos/`).

`lint.py` was the issue's named target and gets a full redirected-output
regression test in test_lint.py (`LintStdoutIsValidUtf8Tests`), reproducing
the issue's own repro almost exactly: W125's message text embeds a literal
ellipsis (`` `spouse: [P-…, P-…]` ``), which cp1252 encodes as the single
byte 0x85 - not valid UTF-8 on its own. `xref.py` gets the same end-to-end
treatment in test_xref.py (`XrefStdoutIsValidUtf8Tests`), this time with a
data-carried non-ASCII value (a place name) rather than a fixed string, to
cover the other shape the bug takes.

For the other 9 modules in scope - cooccur, id, index, places, reconcile,
relate, report, serve - eleven full subprocess regression tests would mostly
re-prove the one already-covered mechanism (`sys.stdout.reconfigure`) rather
than add confidence; what actually varies module to module is just "is the
call wired up, and wired up early enough that nothing can print before it
runs". That is a source-inspection check, in the same spirit as this
codebase's other structural invariants (e.g.
tests/test_scaffold.py::ManifestChecksumMatchesGitBlobTests, which pins a
generated artifact against its source rather than re-deriving it).

`fha.py` is checked separately (`FhaConfiguresUtf8StdoutTests`): unlike
every other tool module it imports NOTHING from `_lib` at module level, by
design - so a missing PyYAML doesn't break `fha install`/`fha doctor` before
their own guarded entry points get a turn (see fha.py's `_intercept_doctor`
and `_intercept_scaffold` docstrings). Its call necessarily lives inside
`main()`, as the first statement run, rather than at import time.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / 'tools'

# The issue's scope, minus lint.py and xref.py (each has its own end-to-end
# regression test elsewhere) and minus fha.py (checked separately below,
# different architecture). Also minus normalize_links.py / stubs.py /
# views.py, which genuinely still need this fix - they were mid-review in
# other open PRs (#89/#90) at the time of this change and were deliberately
# left untouched to avoid colliding with them; site.py and process.py in
# that same set already call configure_utf8_stdout().
MODULE_LEVEL_CALL_MODULES = (
    'cooccur', 'id', 'index', 'places', 'reconcile', 'relate', 'report',
    'serve',
)


class ConfigureUtf8StdoutIsWiredUpTests(unittest.TestCase):
    """Shared invariant across the plain module-level tools: import the
    symbol from `_lib`, then call it near the top - before the module's
    first top-level `def`/`class`, i.e. before any command logic can run
    and print something non-ASCII."""

    def _source(self, module_name: str) -> str:
        path = TOOLS / f'{module_name}.py'
        self.assertTrue(path.exists(), f'{path} does not exist')
        return path.read_text(encoding='utf-8')

    def test_each_module_imports_configure_utf8_stdout_from_lib(self) -> None:
        for name in MODULE_LEVEL_CALL_MODULES:
            with self.subTest(module=name):
                src = self._source(name)
                self.assertRegex(
                    src, r'from _lib import[\s\S]*?\bconfigure_utf8_stdout\b',
                    f'{name}.py does not import configure_utf8_stdout from _lib')

    def test_each_module_calls_configure_utf8_stdout_before_its_first_def_or_class(self) -> None:
        for name in MODULE_LEVEL_CALL_MODULES:
            with self.subTest(module=name):
                src = self._source(name)
                call_match = re.search(r'^configure_utf8_stdout\(\)\s*$', src, re.M)
                self.assertIsNotNone(
                    call_match, f'{name}.py never calls configure_utf8_stdout() '
                    'as a bare module-level statement')
                first_def_match = re.search(r'^(def |class )\w', src, re.M)
                if first_def_match is not None:
                    self.assertLess(
                        call_match.start(), first_def_match.start(),
                        f'{name}.py calls configure_utf8_stdout() after its '
                        'first top-level def/class - some output could '
                        'already have happened by then')


class FhaConfiguresUtf8StdoutTests(unittest.TestCase):
    """fha.py's own version of the same invariant, adapted to its
    deferred-import architecture (see module docstring)."""

    def setUp(self) -> None:
        self.src = (TOOLS / 'fha.py').read_text(encoding='utf-8')

    def test_imports_configure_utf8_stdout_from_lib(self) -> None:
        self.assertRegex(
            self.src, r'from _lib import[\s\S]*?\bconfigure_utf8_stdout\b',
            'fha.py does not import configure_utf8_stdout from _lib')

    def test_main_calls_it_before_any_command_can_dispatch_or_print(self) -> None:
        main_match = re.search(r'^def main\(', self.src, re.M)
        self.assertIsNotNone(main_match, 'fha.py has no top-level main()')
        after_main = self.src[main_match.start():]

        call_match = re.search(r'\bconfigure_utf8_stdout\(\)', after_main)
        self.assertIsNotNone(
            call_match, 'main() never calls configure_utf8_stdout()')

        # Nothing that can print a command's own output - the unknown-command
        # path, any early-dispatch _intercept_*, or the bulk lazy tool-module
        # imports that follow - may run before the call.
        markers = (
            '_first_command_token(',
            '_intercept_id_check(',
            '_intercept_doctor(',
            '_intercept_scaffold(',
            '_intercept_gedcom_import(',
            '_intercept_claim_new(',
            '_intercept_process_refile(',
            'from id import register',
        )
        for marker in markers:
            with self.subTest(marker=marker):
                marker_match = re.search(re.escape(marker), after_main)
                if marker_match is not None:
                    self.assertLess(
                        call_match.start(), marker_match.start(),
                        f'fha.py reaches {marker!r} before calling '
                        'configure_utf8_stdout()')


if __name__ == '__main__':
    unittest.main()
