# tests/fixtures/

The clean fixture is `../../example-archive/`. The folders here are smaller fixtures; the table lists the tests that read each one.

| Folder | Used by |
|---|---|
| `broken-W120/` | `test_brackets_spouse_w120.py`: an archive that should raise lint W120 |
| `broken-places/` | `test_places.py`: a places registry with known problems |
| `broken-W103/`, `broken-W110/` | no test reads these; each is a small archive built to raise its lint code |
| `bundle-folder/` | `test_process.py` |
| `capture-samples/` | `test_capture.py`, `test_capture_host.py`, `test_capture_ingest.py`, `test_browser_companion.py` and the browser-companion JS tests |
| `gedcom/` | `test_gedcom_import.py` |
| `legacy-export/` | `test_convert_mining.py` |
| `relate-golden/` | `test_relate_golden.py` (every `fha relate` answer on example-archive) and `test_kinship.py` (the relationship words); regenerate with `py -3.14 tests/test_relate_golden.py --write` and `py -3.14 tests/test_kinship.py --write` only when an answer is meant to change |
| `photo-fixture/` | `test_photoindex.py`, `test_process.py`, `test_originals_backup.py`, `test_reconcile.py`, `test_working_copy.py` |
| `working-copy/` | `test_working_copy.py` |
