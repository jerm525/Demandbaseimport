# Salesforce → Demandbase Opportunity Sync

Daily batch job (scheduled via Windows Task Scheduler) that reads changed
Opportunity rows from a SQL Server view, maps them to Demandbase's import
format, submits them as a CSV import job, and writes audit rows for every
job and every record. Never calls the Salesforce API directly.

**Do not go live with this codebase until you've resolved every item in
["Flagged items — resolve before go-live"](#flagged-items--resolve-before-go-live)
below.** Several of them are placeholders that will produce wrong or
non-functional behavior if left unconfirmed.

---

## 1. Setup

### 1.1 Python version

Python 3.12.x


### 1.2 Install dependencies

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

### 1.3 Secrets (never in source control)

Set these as OS/user environment variables (or wire in your organization's
secrets manager and adjust `config.py`'s `get_sql_connection_string` /
`get_demandbase_token` to read from it instead — both already read fresh on
every call, so swapping the backend doesn't require touching call sites):

| Variable | Purpose |
|---|---|
| `DEMANDBASE_SYNC_SQL_CONN_STR_DEV` / `_TEST` / `_PROD` | Full ODBC connection string for that environment |
| `DEMANDBASE_API_TOKEN_DEV` / `_TEST` / `_PROD` | Demandbase bearer token for that environment |
| `DEMANDBASE_SYNC_ALERT_WEBHOOK_URL` (optional) | Only used if `notifier.channel: webhook` |
| `DEMANDBASE_SYNC_ALERT_EMAIL_TO` (optional) | Only used if `notifier.channel: email` |

The Demandbase token is cached 

### 1.4 Field mapping — required before first run

`field_mapping.yaml` ships with placeholder `source_field` values
(`CHANGEME__...`) because this was built without a live connection to the
source view. Before running anywhere:

```powershell
python scripts/introspect_view_schema.py --env dev
```

This prints the real column names in
`[marketing_bi].[EDA-268].[vw__demandbase_Opportunity_import]`. Replace every
`CHANGEME__...` in `field_mapping.yaml` with the matching real column. The
app validates the mapping against the live view schema at the start of every
run and refuses to start if anything still doesn't match (see
`mapping.validate_mapping`) — so a leftover placeholder fails loudly, not
silently.

### 1.5 Environments

```powershell
python run.py --env dev
python run.py --env test
python run.py --env prod
```

Each environment has its own config file under `config/` (`dev.yaml`,
`test.yaml`, `prod.yaml`) with its own staging/archive/error folders and log
paths, so a broken dev run can never touch prod data. Falls back to the
`DEMANDBASE_SYNC_ENV` environment variable, then `dev`, if `--env` is omitted
— it never silently defaults to `prod`.

### 1.6 Windows Task Scheduler

Schedule `python run.py --env prod` to run once daily. In the task's
**Settings** tab, enable *"If the task is already running, do not start a
new instance"* — this is a scheduling-level safeguard in addition to (not a
replacement for) the application-level check that blocks a second run from
doing new work while a job is `IN_PROGRESS` (see §3.2). 
exe path: .venv/Scripts/python.exe

---

## 2. Project layout

```
run.py                          Entrypoint (Task Scheduler target)
field_mapping.yaml               Field mapping config (EDIT per §1.4 before go-live)
config/{dev,test,prod}.yaml      Per-environment, non-secret config
scripts/introspect_view_schema.py   Prints real view columns
src/demandbase_sync/
  config.py                      Config loading, secrets accessors
  db.py                          SQL extraction (§4 self-healing query), schema introspection
  mapping.py                     Field mapping load/validate/apply
  transforms.py                  Named, extensible transform library
  csv_writer.py                  CSV generation
  demandbase_client.py           Job creation, CSV upload, polling (injectable poller)
  audit.py                       Writes to tbl__demandbase_jobs / tbl__demandbase_records
  archival.py                    Archive/error moves + 90-day retention purge
  notifier.py                    Pluggable FAILED/PARTIAL_SUCCESS alerting
  logging_setup.py               JSON-lines per-run log + rotating all-runs error log
  orchestrator.py                Ties it all together (§8/§8.5 sequencing)
tests/                           pytest suite covering TC1-TC11 (see §4 below)
staging/ archive/ error/ logs/   Working folders (per-environment subfolders)
```

---

## 3. How it works

### 3.1 Change detection

Implements the exact self-healing query from the build prompt: a record is
selected if it has never had a `RecordStatus='SUCCESS'` row, or if it's been
modified/created since its last successful sync. A record with only failed
prior attempts is automatically retried — there's no separate "permanently
failed" exclusion list. `MaxSourceModifiedDate` on the jobs table is used
only as an optional pre-filter for query performance; it never gates
inclusion by itself.

One addition beyond the prompt's literal SQL: the query also selects a
derived `SyncActionHint` (INSERT/UPDATE) column, read off the same LEFT JOIN
that already determines inclusion — this doesn't change the WHERE logic at
all, it just avoids a second round-trip to know which SyncAction to audit.

### 3.2 Restart / idempotency

- On every run, the app first checks for a `Status='IN_PROGRESS'` job row.
  If found, it **resumes polling that job** instead of doing any new work —
  this blocks a concurrent/duplicate run (TC9) and lets a crash-mid-poll run
  be safely resumed (TC6).
- To make that resume actually able to write correct audit rows (Demandbase
  target fields don't carry `LastModifiedDate`/`SyncAction`, so that
  information would otherwise be lost once a record is mapped into the
  outbound CSV), each batch also writes a small manifest JSON file
  (`staging/manifest_<JobId>.json`) alongside its CSV. This is an internal
  implementation detail, not a database schema change.
- A crash after batch N of a multi-batch run completes, but before batch
  N+1 starts, needs no separate "resume from batch N" bookkeeping: the next
  run's self-healing query naturally excludes everything already marked
  `SUCCESS` in batches 1..N (TC11).

### 3.3 Batching (§8.5)

`demandbase.max_records_per_batch` (config, currently a **placeholder — see
flagged items**) caps how many records go into a single CSV/job. A change
set larger than that splits into sequential batches, each going through the
full create-job → generate-CSV → submit → poll → audit cycle independently.
This only really bites on the first run (no prior successful job caps
nothing) or a large backfill.

### 3.4 Partial failure / per-record detail

The Demandbase status endpoint is not yet confirmed to return per-record
detail. If it does (`record_errors` on the poll result), individual records
are marked `SUCCESS`/`FAILED` accordingly. **If a job comes back
`PARTIAL_SUCCESS` with no per-record detail, every sent record in that batch
is conservatively marked `FAILED`** so they're retried by the next run's
self-healing query, rather than risk marking an actually-failed record
`SUCCESS` and never syncing it. This trades some duplicate re-sends for
correctness — revisit once the real polling contract is confirmed (flagged
below).

### 3.5 Logging

Each run writes a JSON-lines log file at `logs/<env>/runs/run_<run_id>.jsonl`,
with every line taggable by the batch's `JobId` once one exists. `ERROR`-level
lines are also mirrored into a rotating `logs/<env>/all_runs_errors.log` for
quick cross-day scanning.

### 3.6 Alerts

`notifier.channel` in config defaults to `noop` (alerts are logged at
`ERROR` level only). Set it to `webhook` or `email` and provide the
corresponding environment variable once a real channel is confirmed
(flagged below) — `WebhookNotifier`/`EmailNotifier` are stubbed and ready to
fill in.

---

## 4. Running the tests

```powershell
pip install -r requirements.txt
pytest
```

`tests/` covers all 13 scenarios (TC1–TC11 explicitly, plus dedicated unit
tests for the transform library and mapping validation) from the build
prompt's test matrix, all mocked (`tests/conftest.py`'s `FakeConnection`
stands in for SQL Server; `tests/test_orchestrator.py`'s
`FakeDemandbaseClient` stands in for the Demandbase API). No test hits a
real database or a real HTTP endpoint.

> **Note on how this suite was verified during development:** this project
> was built in a sandbox with no network access to PyPI, so `pydantic` and
> `tenacity` could not actually be installed to execute `pytest` there. All
> 45 tests were instead run against small local shims of pydantic's and
> tenacity's public APIs (not shipped, and not part of `requirements.txt`)
> to validate the actual application logic before delivery, and all passed.
> **Please re-run `pytest` for real, with the real dependencies installed,
> before trusting this in production** — a hand-rolled shim can miss edge
> cases (see §14 caveat below) that the real libraries would catch or behave
> differently on.

---

## 5. Reading logs and audit tables

- **Was today's run OK?** Check the latest file in `logs/<env>/runs/` for
  that run's summary line, or query `tbl__demandbase_jobs` for today's
  `RunStartTime` and check `Status`.
- **Which records failed and why?** `SELECT * FROM tbl__demandbase_records
  WHERE JobId = '<JobId>' AND RecordStatus = 'FAILED'` — `ErrorMessage` has
  the reason (required-field-null, transform failure, or "reported as
  failed by Demandbase").
- **Cross-day error scan:** `logs/<env>/all_runs_errors.log` (rotating,
  ERROR-level only, JSON lines).

---

## 6. Runbook: FAILED or PARTIAL_SUCCESS runs

**FAILED job:**
1. Check the run's log file and `tbl__demandbase_jobs.Status='FAILED'` row
   for the error (job creation rejected, upload rejected, or polling
   timeout).
2. The batch's CSV is in `error/<env>/`, not `archive/<env>/`.
3. No records in that batch were marked `SUCCESS` and the watermark was not
   advanced — the next scheduled run will automatically retry every record
   in that batch via the self-healing query. No manual re-queue is needed
   unless the failure is due to something that will recur (e.g. an expired
   token, a schema drift, a Demandbase-side outage) — fix that first, or the
   next run just fails again.
4. If a job is stuck `IN_PROGRESS` for far longer than expected (e.g. the
   process crashed instead of exiting cleanly), the next scheduled run will
   automatically try to resume polling it. If it's genuinely stuck (job
   doesn't exist on Demandbase's side anymore, or its staging manifest/CSV
   were deleted), the run will mark it `FAILED` and alert rather than
   guessing — you may need to manually correct that job's row.

**PARTIAL_SUCCESS job:**
1. Query `tbl__demandbase_records` for that `JobId` with
   `RecordStatus='FAILED'` to see which Opportunities failed and why.
2. Common causes: a required mapped field was null on the source side
   (fix the data or the mapping's `required`/`default`), a value violates a
   Demandbase constraint (see the "picklist/enum constraints" flagged item
   below), or (if per-record detail isn't available) a
   conservative-fallback `FAILED` from an unattributed partial failure.
3. Fixed records will be automatically picked up and retried by the next
   scheduled run's self-healing query — no manual re-queue needed.

---

## 7. Flagged items — resolve before go-live

Everything below was called out in the build prompt as something not to
silently resolve. None of these block the code from running end-to-end
against mocks, but several will produce wrong behavior against the real
Demandbase API or the real business data until confirmed:

2. **The Demandbase polling/status endpoint URL and response shape are not
   confirmed.** `demandbase_client.HttpStatusPoller` implements a plausible
   `GET {base_url}/import/v1/job/{id}` guess purely so the pipeline has
   something to run against — it is clearly marked `*** UNCONFIRMED
   ENDPOINT ***` in code and must be corrected (or replaced) once Demandbase
   confirms the real contract. Because polling goes through the injectable
   `StatusPoller` protocol, this is a contained, one-class change.
6. **Demandbase's actual per-job upload limits** (record count and/or file
   size), and whether job creation is itself rate-limited, are unknown.
   `demandbase.max_records_per_batch` defaults to `5000` as a conservative
   placeholder (config-only change, no redeploy needed) and
   `demandbase.inter_batch_delay_seconds` defaults to `0` and is ready to
   set if job creation turns out to be rate-limited.
7. **Sign-off that a run may produce more than one Demandbase job when it
   batches.** This changes the "one job per run" framing used elsewhere and
   should be confirmed with the Marketing Technology Owner before relying on
   it in production, per the build prompt.
8. **The initial/go-live watermark** (`watermark.initial_watermark_utc` in
   each environment's config, currently `2000-01-01T00:00:00Z`) needs a real
   business decision — how far back should the very first run reach into
   history?
9. **The alert channel/recipient** is undecided; `notifier.channel` defaults
   to `noop` (log-only). Set it to `webhook` or `email` once decided.
10. **Whether a run should still create an empty Demandbase job when there
    are zero changed records** is a config flag
    (`demandbase.create_job_on_empty_changeset`, default `False`), not a
    hardcoded behavior — confirm the desired default before go-live.
12. **The verification test run described in §4** used hand-written local
    shims of pydantic/tenacity instead of the real libraries, because this
    build environment had no PyPI access. Re-run `pytest` with the real
    dependencies installed before trusting the suite's results in
    production.
