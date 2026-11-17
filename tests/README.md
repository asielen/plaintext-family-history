# tests/

`../example-archive/` is the golden fixture - it must lint with no errors under the current rules, and its only warnings are the documented baseline (`TOOLING.md` §15). `test_samples_conformance.py` checks that it, and `../archive-template/`, use no key `SPEC.md` does not name.
Intentionally **broken** fixtures (one per lint code) will live here under `fixtures/broken-*/` once the tools are implemented, each asserting that its error code fires.
