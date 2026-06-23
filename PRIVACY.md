# Privacy & Example-Data Policy

This repository is the public **spec and tooling** for Plainfile.
It must never contain real, private family-history data.

- **Examples are fictional.** Use the Hartley family in `example-archive/`, or invent
placeholders.
Never use a real person's records.
- **No living-person data**, anywhere - issues, PRs, fixtures, or docs.
- **No DNA files, raw genealogy exports, personal photos, or identifiable family documents.**
- **Anonymize real scenarios.** If an issue needs a real research situation to make sense,
change the names, places, dates, and IDs first.
- **Your own archive is separate and private.** You build your real family archive in a
*separate, private repository* created from `archive-template/` - see the root `README.md` ("Repo, tools, and your archive").
Your data depends on this public spec; it never lives inside it.
- Maintainers may delete any issue, comment, or PR that exposes personal data.

The architecture is designed around this: the public repo holds the format and the generic tools; private family data lives only in your own private archive that uses them.
