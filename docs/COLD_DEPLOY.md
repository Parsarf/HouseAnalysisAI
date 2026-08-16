# ACQ cold deploy

This runbook provisions a clean host and starts the application without using
local build artifacts.

1. Install Docker Engine/Compose, Git, and a current Node.js runtime.
2. Clone the repository and enter it:

   ```sh
   git clone https://github.com/Parsarf/HouseAnalysisAI.git
   cd HouseAnalysisAI
   ```

3. Copy `.env.example` to `.env`, replace the session secret and password hash,
   and set the extraction provider key, base URL, and model names deliberately.
4. Start Postgres for local validation:

   ```sh
   docker compose up -d db
   ```

5. Install Python dependencies and apply migrations:

   ```sh
   python3 -m venv .venv
   . .venv/bin/activate
   pip install -e '.[dev]'
   alembic upgrade head
   ```

6. Build and serve the frontend:

   ```sh
   cd web && npm ci && npm run build && cd ..
   uvicorn api.app:app --host 0.0.0.0 --port 8000
   ```

7. In a second shell, start the worker with the same environment:

   ```sh
   . .venv/bin/activate
   python -m pipeline.run_worker
   ```

8. Verify `GET /healthz`, log in, upload one non-sensitive PDF, confirm its
   pre-flight estimate, start the batch, and verify the deal page and export.

The first deploy should be timed. A production launch is not considered
verified until this procedure completes on an empty host in under 15 minutes,
with provider credentials supplied through the environment rather than source.
