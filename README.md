ABRA Flexi
=============

Description

**Table of Contents:**

[TOC]

Functionality Notes
===================

Prerequisites
=============

Ensure you have the necessary API token, register the application, etc.

Features
========

| **Feature**             | **Description**                               |
|-------------------------|-----------------------------------------------|
| Generic UI Form         | Dynamic UI form for easy configuration.       |
| Row-Based Configuration | Allows structuring the configuration in rows. |
| OAuth                   | OAuth authentication enabled.                 |
| Incremental Loading     | Fetch data in new increments.                 |
| Backfill Mode           | Supports seamless backfill setup.             |
| Date Range Filter       | Specify the date range for data retrieval.    |

Supported Endpoints
===================

If you need additional endpoints, please submit your request to
[ideas.keboola.com](https://ideas.keboola.com/).

Configuration
=============

Param 1
-------
Details about parameter 1.

Param 2
-------
Details about parameter 2.

Output
======

Provides a list of tables, foreign keys, and schema.

Each evidence is written to its own table.

**Primary key.** Leave the *Primary key* field empty (the default) and the component
auto-detects the key from the evidence metadata — `id` on standard evidences, the evidence's own
key column on derived ones (e.g. `idUcetniDenik` for `ucetni-denik`). If none of the candidate
columns is unique across the fetched records — or the evidence exposes no identifier at all
(report views such as `rozvaha-po-uctech`) — the table is loaded without a primary key rather
than silently overwriting rows. To override the detection, pick columns from the list (populated
from the selected evidence) or type your own. Changing the primary key of a table that already
exists in Storage requires dropping the output table first.

**Load type & date window.** *Load type* controls how the table is written to Storage:
*Incremental load* upserts on the primary key; *Full load* overwrites the table on every run.
*Date field* selects which date/datetime column the *Date Start* / *Date End* window filters on
(default `lastUpdate`; pick another from the list or type your own). The window is applied on every
run for both load types — there is no automatic watermark, so bound an incremental load with a
relative *Date Start* (e.g. "2 days ago") to pull a rolling window of recent changes.

Because the window is re-evaluated from scratch on every run and no watermark is stored, a
relative *Date Start* only sees records whose *Date field* falls inside the window at run time.
Records changed while the configuration is paused, or while the source is unreachable for longer
than the window, are not re-fetched later and nothing reports the gap. After an outage or a long
pause, widen *Date Start* for one run — or run an occasional full load — to backfill anything the
rolling window skipped.

When an **incremental** run returns no records (e.g. its window matched nothing new), no table is
written and the existing table in Storage is left unchanged. A **full** load that returns no
records still overwrites its table — emptying it — because a full load always replaces Storage
with exactly what the source returned.

Development
-----------

To customize the local data folder path, replace the `CUSTOM_FOLDER` placeholder with your desired path in the `docker-compose.yml` file:

~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    volumes:
      - ./:/code
      - ./CUSTOM_FOLDER:/data
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Clone this repository, initialize the workspace, and run the component using the following
commands:

~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
git clone  component-ex-flexibee
cd component-ex-flexibee
docker-compose build
docker-compose run --rm dev
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Run the test suite and perform lint checks using this command:

~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
docker-compose run --rm test
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Integration
===========

For details about deployment and integration with Keboola, refer to the
[deployment section of the developer
documentation](https://developers.keboola.com/extend/component/deployment/).
