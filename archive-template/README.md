# Archive Template

Copy this folder's contents to start your own family archive.
This is the empty skeleton a real archive grows from — `inbox/` for new material, `sources/` for evidence records, `people/` for person records, `places/places.yaml`, `notes/`.

**Your archive is a separate, private repository** — never commit real family data to the public spec repo.
See the repo root `README.md` ("Repo, tools, and your archive") for how the public spec/tools and your private archive relate.

After copying:
1. Edit `fha.yaml` to point at where your photos and documents live.
2. Bring in the **operating layer** from the public repo. The planned easy way —
   `fha install <this-archive>` once from your clone of the public repo, copying the
   `tools/` folder plus `SPEC.md`, `TOOLING.md`, `AGENTS.md`, `AGENTS_TOOLING.md`, `CLAUDE.md`
   and nothing else, with `fha update-tools` later pulling improvements and backing up
   anything you've customized — is not built yet (`BUILD.md` M9.1–M9.2, TOOLING.md §13c).
   Until then, copy those files by hand.
3. Open in your AI agent and start processing `inbox/` items.
