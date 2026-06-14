# tools/

The `fha` command suite will live here once implemented.
Until then, **`TOOLING.md` (repo root) is the source of truth** for what each tool must do, in enough detail to build them from scratch.

These tools are **generic**: they operate on any spec-conforming archive and contain no family data.
They are the "replaceable glue" of the philosophy — disposable, regenerable from the spec, and safe to publish.
Building them is the first milestone; see `TOOLING.md` §15 for the build order.
The first target is `fha lint` running clean against `../example-archive/`.
