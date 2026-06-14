# tests/

`../example-archive/` is the **clean** golden fixture — it must lint clean under the current rules.
Intentionally **broken** fixtures (one per lint code) will live here under `fixtures/broken-*/` once the tools are implemented, each asserting that its error code fires.
