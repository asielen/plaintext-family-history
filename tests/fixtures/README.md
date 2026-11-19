# tests/fixtures/

The clean fixture is `../../example-archive/`. The folders here are smaller fixtures for single tests.

| Folder | Used by |
|---|---|
| `broken-W120/` | `test_brackets_spouse_w120.py`: an archive that should raise lint W120 |
| `broken-places/` | `test_places.py`: a places registry with known problems |
| `broken-W103/`, `broken-W110/` | no test reads these; each is a small archive built to raise its lint code |
| `bundle-folder/` | `test_process.py` |
| `capture-samples/` | `test_capture.py`, `test_capture_host.py`, `test_browser_companion.py` and the browser-companion JS tests |
| `gedcom/` | `test_gedcom_import.py`, `test_unreadable_person_exports.py` |
| `legacy-export/` | `test_convert_mining.py` |
| `photo-fixture/` | `test_photoindex.py`, `test_process.py`, `test_originals_backup.py` |
| `working-copy/` | `test_backup.py`, `test_gedcom_import.py`, `test_process_refile.py` |
