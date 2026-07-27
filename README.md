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

Each evidence is written to its own table. The primary key follows the *Primary key* row
setting: `auto` (default) takes it from the evidence metadata — `id` on standard evidences,
the evidence's own key column on derived ones (e.g. `idUcetniDenik` for `ucetni-denik`) —
while `custom` uses the columns you select and `none` writes the table without a primary key
(the only option for report evidences such as `rozvaha-po-uctech`, which expose no record
identifier). Changing the primary key of a table that already exists in Storage requires
dropping the output table first.

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
