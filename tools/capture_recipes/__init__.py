"""capture_recipes — site recipes for `fha capture` (TOOLING §13b).

Each recipe module exposes `detect(html, url) -> bool` and
`extract(html, url) -> dict` (the fields of `capture.RecipeResult`), plus
`SOURCE_NAME` and `PRIORITY`. `fha capture` discovers them at runtime and tries
them in ascending PRIORITY order, falling back to the generic recipe. The set is
*data* — adding a site is adding a module here, no change to `capture.py`.
"""
