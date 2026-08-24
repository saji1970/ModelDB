# Third-Party Notices

MDC is licensed under the Apache License, Version 2.0 (see [LICENSE](LICENSE)
and [NOTICE](NOTICE)). It depends on the following open-source packages,
each under its own license, verified against the license metadata each
package actually publishes:

| Package | License | Used for |
|---|---|---|
| [DuckDB](https://duckdb.org/) | MIT | The storage backend (WARM/COLD/ARCHIVE tiers, schema registry persistence) |
| [Pydantic](https://docs.pydantic.dev/) | MIT | Every data model - operations, records, index entries, API request/response bodies |
| [FastAPI](https://fastapi.tiangolo.com/) | MIT | The REST API and the Storage Explorer web UI |
| [Uvicorn](https://www.uvicorn.org/) | BSD-3-Clause | The ASGI server FastAPI runs on |
| [Typer](https://typer.tiangolo.com/) | MIT | The `mdc` CLI |
| [Rich](https://github.com/Textualize/rich) | MIT | CLI console output (tables, formatted text) |
| [PyYAML](https://pyyaml.org/) | MIT | Loading the schema/ontology YAML config files |

Development-only (not shipped in the installed package):
[pytest](https://pytest.org/) (MIT) and [httpx](https://www.python-httpx.org/)
(BSD-3-Clause), used for the test suite.

None of the above are modified or redistributed as part of this
repository - they are used as-is via their published packages. This file
does not cover their own transitive dependencies (Starlette, AnyIO,
Pydantic-core, etc.); consult each package's own distribution for those.

No license here grants, or should be read as implying, any trademark
rights to the names above - see each project's own license for its
trademark terms.
