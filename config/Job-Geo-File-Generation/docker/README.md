# Job geo-file-generation — Docker

The image is built from the sibling repository Dockerfile:

`../rer-dsp-job-geo-file-generation/Dockerfile` (path configurable via `DSP_JOB_GEO_FILE_GENERATION_PATH`).

The `dsp-job-geo-file-generation` service uses Compose profile `geo-file`. It publishes the
territorial download files (levels 2 and 3) to the object storage so that `/downloads/file`
does not have to query the WFS. Batch metadata lives in `dsp-db` schema `geo_file_generation`
(tables `BATCH_*` exclusive to this job). The migration job keeps its own schema
`data_migration`.

The bucket must already exist: the job never creates it. Without a bucket it logs the error,
publishes nothing and the JVM exits with success (so the stack keeps running and the
territorial flags stay on for the next cycle). The Spring Batch metadata in schema
`geo_file_generation` records `exit_code = OBJECT_STORAGE_NOT_READY` on
`batch_job_execution` — query that column to see cycles where generation was skipped due
to storage, distinct from `COMPLETED` (all files published) or `PUBLISH_*` (partial failures).

## Entrypoint

[`entrypoint.sh`](entrypoint.sh) is copied into the image as `/geo-file-entrypoint.sh`:

| `DSP_GEO_FILE_GENERATION_EXECUTION_MODE` | Behaviour |
| --- | --- |
| `continuous` (default) | `supercronic` on `DSP_GEO_FILE_GENERATION_CRON` |
| `once` | Runs `java -jar /app/app.jar` and exits — used by `compose run --rm` |

| Variable | Notes |
| --- | --- |
| `DSP_GEO_FILE_GENERATION_CRON` | 5-field cron (e.g. `0 2 * * *`). Schedule it **after** the migration window (`DSP_MIGRATION_CRON`), because the migration is what raises the flags. |
| `DSP_GEO_FILE_GENERATION_TZ` | IANA timezone for wall clock. Defaults to `DSP_MIGRATION_TZ`. |

The JAR stays one-shot. Overlap: `flock` in the supercronic wrapper — the first full
generation can outlive its window, and two cycles would publish the same keys and race on
the flags. A failed JAR does not stop the continuous container.

## Configuration

`./config.sh` collects the endpoint, bucket, region, credentials, path-style and the cron,
and writes them to `application/application.yaml` and to `.env`. An empty endpoint leaves
the job disabled and downloads keep going straight to the WFS.

The backend reads the same bucket (`DSP_OBJECT_STORAGE_*` in `.env`) to serve the file and
to report `lastFileGenerated` from the object metadata `generated-at`.

## Commands

**Scheduled service** (`continuous`):

```bash
docker compose --env-file .env --profile geo-file up -d --build dsp-job-geo-file-generation
```

**One-time now** (`once`):

```bash
docker compose --env-file .env --profile geo-file run --rm --build \
  -e DSP_GEO_FILE_GENERATION_EXECUTION_MODE=once dsp-job-geo-file-generation
```

The image copies `application.yaml`, `downloadThemesConfig.json` and the entrypoint at build time.
