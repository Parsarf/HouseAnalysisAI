# ACQ deployment: Vercel + Railway + PostgreSQL

The application is provider-agnostic. Vercel hosts only the Vite SPA;
Railway runs the API and the independent worker from the same repository.
PostgreSQL and document storage are external services.

## Railway

1. Create a Railway project and add a PostgreSQL service.
2. Add an API service from this GitHub repository. Railway uses the root
   `Dockerfile` and `railway.json`; expose its generated public domain.
3. Add a second service from the same repository for the worker. Use the same
   Dockerfile and override its start command to:

   ```sh
   python -m pipeline.run_worker
   ```

4. Set these variables on both services:

   ```text
   ACQ_ENV=production
   ACQ_DATABASE_URL=<Railway PostgreSQL connection URL using psycopg>
   ACQ_SESSION_SECRET=<long random value>
   ACQ_AUTH_PASSWORD_HASH=<argon2 hash>
   ACQ_STORAGE_BACKEND=s3
   ACQ_S3_ENDPOINT=<S3-compatible endpoint>
   ACQ_S3_REGION=<region>
   ACQ_S3_BUCKET=<bucket>
   ACQ_S3_ACCESS_KEY_ID=<access key>
   ACQ_S3_SECRET_ACCESS_KEY=<secret key>
   ACQ_EXTRACTION_API_KEY=<provider key>
   ACQ_EXTRACTION_BASE_URL=<OpenAI-compatible provider URL>/v1
   ACQ_EXTRACTION_CHEAP_MODEL=<cheap model>
   ACQ_EXTRACTION_FRONTIER_MODEL=<frontier model>
   ACQ_EXTRACTION_TIMEOUT_SECONDS=180
   ```

   For S3-compatible storage, use `ACQ_STORAGE_BACKEND=s3` and provide
   `ACQ_S3_ENDPOINT`, `ACQ_S3_REGION`, `ACQ_S3_BUCKET`,
   `ACQ_S3_ACCESS_KEY_ID`, and `ACQ_S3_SECRET_ACCESS_KEY` instead of relying
   on a local volume.

5. Separate Railway services do not share their container filesystems. Do not
   use `ACQ_STORAGE_BACKEND=filesystem` for a split API/worker deployment unless
   your infrastructure provides a genuinely shared filesystem mounted at the
   same path in both services. Railway volumes attach to an individual service,
   so the recommended Railway configuration is S3-compatible object storage:

   ```text
   ACQ_STORAGE_BACKEND=s3
   ACQ_S3_ENDPOINT=<S3-compatible endpoint>
   ACQ_S3_REGION=<region>
   ACQ_S3_BUCKET=<bucket>
   ACQ_S3_ACCESS_KEY_ID=<access key>
   ACQ_S3_SECRET_ACCESS_KEY=<secret key>
   ```

   Set the same values on both the API and Worker services. After changing an
   existing filesystem deployment, re-upload failed documents so their stored
   references are migrated to object storage.
6. Run migrations once against PostgreSQL:

   ```sh
   ACQ_DATABASE_URL='<connection URL>' alembic upgrade head
   ACQ_DATABASE_URL='<connection URL>' alembic check
   ```

7. Configure the API service health check as `/readyz`. `/healthz` is a
   process check; `/readyz` verifies PostgreSQL connectivity.
8. Set `ACQ_CORS_ORIGINS=https://<your-vercel-domain>` and
   `ACQ_COOKIE_SAMESITE=none` when the SPA and API use different domains.
9. Verify the worker logs show the queue loop and process a test job.

## Vercel

1. Import the repository and set Root Directory to `web`.
2. Use the detected Vite project settings: `npm ci` followed by `npm run build`.
3. Set `VITE_API_BASE_URL=https://<your-railway-api-domain>`.
4. `web/vercel.json` provides the SPA fallback so refreshing `/properties` or
   `/properties/<id>` returns `index.html`.
5. Deploy, log in, and verify that browser requests reach the Railway API.
   Do not put database, provider, or storage secrets in Vercel variables.

## Smoke test and operations

Run `scripts/smoke_test.sh` with `ACQ_BASE_URL`, `ACQ_PASSWORD`, and
`ACQ_PDF`. The test covers health, readiness, login, upload, estimate, and
batch start. The worker and result checks require a real PDF and a running
worker/provider.

Before a production migration, take a backup. Roll back application code by
redeploying the previous commit; roll back a migration only with a reviewed
down migration and a verified backup. Test `scripts/backup.sh` and
`scripts/restore.sh` in a separate target directory before relying on them.
