The ABRA Flexi (FlexiBee) extractor pulls accounting/ERP records out of an ABRA Flexi instance over its REST API into Keboola Storage.

It is generic over evidence types: each configuration row selects one evidence (e.g. `faktura-vydana`, `adresar`, `banka`) and produces one output table. Reference fields are flattened (e.g. `mena`, `mena_ref`, `mena_showAs`) and the record `id` is used as the primary key.

Loading can be full or incremental. Set a date window on the `lastUpdate` field — using relative expressions such as `5 days ago` or absolute dates — to fetch only recently changed records; rows are then upserted incrementally on `id`.

Connection details (base URL, company, username, password) are shared at the configuration level; a Test Connection action verifies them and an evidence picker lists the available evidence types.
