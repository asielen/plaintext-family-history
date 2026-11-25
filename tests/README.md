# tests/

`../example-archive/` is the golden fixture. It must lint with no errors under the current rules, and its only warnings are the documented baseline (`TOOLING.md` §15). `test_samples_conformance.py` checks that it, and `../archive-template/`, use no key `SPEC.md` does not name.

`fixtures/` holds the smaller fixtures, including the deliberately broken archives; `fixtures/README.md` lists them.

`skill_evals/` holds the skill evals: the pinned rules every skill must keep, one behavioural case per rule, and the live runner run by hand. `skill_evals/README.md` explains both layers.
