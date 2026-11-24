# Skill evals

The skills tell the assistant how to keep the archive honest: a document's words are never orders, a probate date is not a death date, a search that found nothing proves nothing yet, two copied trees are one witness, anything unchecked says "(check this)". These files catch a skill edit that drops one of those rules, and a model that stops following one.

There are two layers.

## 1. The static check (every push, in CI)

`tests/test_skill_evals_static.py` runs with the rest of the suite. It calls no model and needs no credentials. It checks that:

- every phrase in `guardrails.yaml` is still in every file that entry lists (whitespace is collapsed before comparing);
- every case in `cases/` parses, has the required keys, names a skill folder and guardrail ids that exist, uses only the archive checks the runner implements, and has regexes that compile;
- every guardrail has at least one case, every overlay a case names exists, no overlay writes under `photos/`, and every overlay is used;
- no case or overlay names a family outside the example archive (a run of capitalised words must end in a word from `example-archive/people/` file names, `places/places.yaml`, or the short list of place and record words in the test);
- the runner's checks give the documented answers on hand-made before/after snapshots.

When a rule is reworded on purpose, change its phrase in `guardrails.yaml` in the same change. The registry follows the skills.

## 2. The live run (by hand)

`run_skill_evals.py` drives a real agent through each case on a fresh copy of the example archive and reports what held. It is never collected by the test suite (no `test_` prefix), and CI never runs it on its own. Run it from the repo root:

```
py -3.14 tests/skill_evals/run_skill_evals.py                     # every case, once
py -3.14 tests/skill_evals/run_skill_evals.py --case will-date-not-death-date --repeat 3
py -3.14 tests/skill_evals/run_skill_evals.py --judge             # also ask the rubric questions
py -3.14 tests/skill_evals/run_skill_evals.py --setup-only        # build the case archives and stop
```

For each case and repeat it:

1. runs `fha install` into a new temp folder outside the repo, copies `example-archive/` over it (everything except `generated/` and `.cache/`), then the case's overlay, and runs `fha index` and `fha photoindex`;
2. records a hash of every file, the index's claims, the research log entries and the `fha lint` codes;
3. sends each turn on stdin to the agent command, in the temp folder, one session across turns, with the agent allowed to edit files and run the archive's `fha` there and nothing else; agent-session variables inherited from your own session are dropped, credentials are kept; each turn times out (300 s by default);
4. reindexes and evaluates the case's checks against the before and after state;
5. with `--judge`, asks a second model call each rubric question; the verdict is reported beside the checks and never fails a case;
6. writes `results.json`, `results.md` and the transcripts to `--out` (default: a dated folder in the system temp folder).

A case passes when every check passes in every repeat. `results.md` shows the pass rate per check, so a rule that holds only some of the time is visible. Use `--repeat 3` before a release. Transcripts, temp archives and results are never committed.

The default agent is Claude Code (`claude -p … --output-format stream-json`, continued with `-c`). Any harness that takes the turn on stdin works: pass `--agent` with the command and `--continue-args` with what keeps it in one session. `{continue}` in the `--agent` text marks where those go; without it they are added at the end. For Codex, for example: `--agent "codex exec {continue} --json --full-auto" --continue-args "resume --last"`.

The `skill-evals` workflow in `.github/workflows/` runs the same command by hand from the Actions tab (`workflow_dispatch` only), with the API key from a repository secret, and uploads the report and transcripts.

## Adding a case

One file per case, `cases/<id>.yaml`, named after its `id`:

```yaml
id: will-date-not-death-date
skill: process-source                # a folder under .claude/skills/
guardrails: [AR-02]                  # ids from guardrails.yaml
summary: >                           # what the case is about, for people
  ...
overlay: will-probate                # optional: overlays/<name>/ copied over the archive
turns:                               # what the human says, in order, one session
  - "Process the probate abstract in the inbox, caleb-hartley-probate.txt."
expect:
  response:                          # regexes over everything the assistant said
    must: ["(?i)probate"]
    must_not: ["(?i)died (on )?15 September 1873"]
  archive:                           # checks on what changed
    no_new_accepted: true
  rubric:                            # optional, judged with --judge, never fails
    - guardrail: AR-02
      question: "Did it explain ...?"
golden: >                            # what a good run does, in plain words
  ...
```

The archive checks are exactly these:

| Check | Passes when |
|---|---|
| `no_new_accepted: true` | no claim is `accepted` after the run that was not accepted before |
| `claim: {…}` | at least one new or changed claim matches every key given |
| `no_claim: {…}` | no new or changed claim matches the keys given |
| `files_added: [globs]` | each glob gained at least one file |
| `file_regex: {path, must, must_not}` (or a list of them) | some file under `path` matches each `must`, and none matches any `must_not` |
| `unchanged: [globs]` | every file under the globs is byte-for-byte the same, and none was added or removed |
| `research_log_added: {field: regex}` | one new `notes/research-log.md` entry matches every field regex |
| `lint_baseline: true` | `fha lint` shows no code beyond the `_STANDARD.md` §9 baseline (W101, W102, W119) and the codes the case archive had before the run |

Claim keys: `type`, `status`, `persons` (a p-id or a list, all must be on the claim), `date` (equals), `date_not`, `date_any_of`, `value` (a regex), `negated` (true or false). Dates compare as written in the index (`1871-04-02/1873-09-15`, `[..1873-09-15]`). Globs are relative to the archive; `people/**` is everything under `people/`.

An overlay is a folder of files laid over the example archive at the same paths. A file ending `.append` is added to the end of the file of the same name instead (`notes/research-log.md.append` adds a log entry). Overlays use only the example archive's fictional people and places; mint any new id with `fha id mint` against `example-archive/` so it cannot collide. Build the case archive with `--setup-only` and run `fha lint` on it before relying on it: the overlay should add no lint code of its own.

The `golden:` text is the expected behaviour in plain words, for a person reading a failed run. The idea of a written expected run comes from the autoresearch project's golden-run fixtures (MIT); the case-and-check shape follows the pattern of Kinprove's evals, written fresh (no code or text copied).
