# HARP sweep server — the generate phase as many TAPIS jobs

The original generate phase runs a whole Cheetah campaign inside **one**
container, so one TAPIS job runs every sweep combination on one machine.
The sweep server splits the sweep into **many TAPIS jobs**, spreads them across
several execution systems (different hardware), follows them until they all end,
and merges the profiling CSVs into one file in a storage location you choose in
the UI. The server reaches every system and file through TAPIS
(Systems, Files, Apps and Jobs). It never touches a cluster file system
directly.

```
 Browser UI ──► HARP sweep server ──► TAPIS Jobs ──► job on system A ─┐
   (login,        expand combos,        (one job       job on system B ─┼─► harp_profile.csv
    sweep form,   split into jobs,       per chunk)    job on system C ─┘   archived by TAPIS to
    storage       throttle per system,                                      <storage>/<campaign>/jobs/<job>/
    picker)       poll status          ◄── TAPIS Files: merge all CSVs ──► <storage>/<campaign>/<app>_<timestamp>.csv
```

## Pieces

| Path | What it does |
|---|---|
| `harp_server/plan.py` | Turns the profiling page's plan into sweeps. Reads each app's command and parameters from its TAPIS notes, fills in each job's hardware from the queue (cores = queue max, 4 GB per core, runs × timeout + 10 min, 1 node, `--nv` on GPU queues, per-queue job limit shared between configurations) and refuses anything over the queue's TAPIS limits. |
| `harp_server/sweep.py` | Validates the spec, expands every parameter combination (SD / FS / test_data) and splits it into jobs. **split** spreads the jobs round-robin over the systems (by weight). **replicate** runs every job on every system so you can profile across hardware. |
| `harp_server/campaign.py` | Submits jobs without exceeding each system's *max concurrent jobs*, polls TAPIS for job status, reads each running job's progress through TAPIS Files, merges the CSVs when every job has ended, and lets you cancel a sweep. Each campaign's state is saved as a JSON file, so it survives a server restart. |
| `harp_server/tapis_gateway.py` | The only code that talks to TAPIS (tapipy): login, systems and queues, apps, files, jobs. |
| `harp_server/app.py` | The REST API, and serves `profiling.html` (the UI) at `/`. |
| `../job_runner/harp_job_runner.py` | Runs inside each job's container. It executes its combinations × repetitions, measures walltime and hardware, and writes `harp_profile.csv` to the TAPIS output directory. |

The merged CSV has the same columns as `Post_Execution_Scripts/basic/DataScrapper.py`
(`run_config, run_type, sys_*, run_<param>, walltime`). It is named `<application>_<timestamp>.csv`,
which is the name the existing **build** phase looks for. Runs that fail go to
`harp_failures.csv` instead, so they never pollute the training data. A
`manifest.json` lists every job with its TAPIS UUID, system and status.

## Setup

1. **Build and push the job container** for your application (from the repo root):
   ```bash
   docker build -f DockerFiles/Dockerfile_App_EulerNumber_Sweep -t <hub>/harp-sweep-eulernumber:1.0.0 .
   docker push <hub>/harp-sweep-eulernumber:1.0.0
   ```
   For your own application, start from `DockerFiles/Dockerfile_App_Template_Sweep`.
2. **Register the TAPIS app** with `Notebooks/Create_HARP_Sweep_App_TAPIS.ipynb` (tapipy). It
   creates the app, or updates it if it already exists, and can run one small test job. The GitHub
   workflow `.github/workflows/build-sweep-image.yml` builds and pushes the image to
   `ghcr.io/<owner>/harp-sweep-eulernumber:1.0.0`, so you don't need Docker locally. Make that
   package public once so the clusters can pull it.
3. **Register the systems and credentials** as in `Notebooks/Executing_HARP_using_TAPIS*.ipynb`.
4. **Just for you, on your laptop (macOS or Linux):** you need Python 3.11 or 3.12 and this repository.
   ```bash
   ./harp_server/scripts/run_local.sh      # first run installs fastapi, uvicorn and tapipy into harp_server/.venv
   ```
   Open `http://localhost:8000`. It listens on this machine only, so plain http is fine. Keep the laptop awake
   while sweeps run (e.g. `caffeinate -i ./harp_server/scripts/run_local.sh` on a Mac); if it sleeps or you stop
   the server, the jobs already in TAPIS keep running, and after you start it again and log in, the server
   picks the sweeps back up. State is kept in `~/.harp_server`.

5. **For other people, run the server over HTTPS** (it receives TAPIS passwords, so it must use HTTPS):
   ```bash
   pip install -r harp_server/requirements.txt
   cd harp_server
   # testing only: a self-signed certificate (browsers will show a warning)
   ./scripts/make_dev_cert.sh certs <server-hostname>
   HARP_SSL_CERT=certs/cert.pem HARP_SSL_KEY=certs/key.pem python3 -m harp_server.serve
   ```
   Open `https://<server-hostname>:8443`. For a server other people use, get a real
   certificate (from your institution or Let's Encrypt) and point `HARP_SSL_CERT` / `HARP_SSL_KEY`
   at it. If HTTPS is handled by nginx, Caddy or Apache in front of the server, run
   `HARP_BEHIND_PROXY=1 HARP_PORT=8000 python3 -m harp_server.serve` and have the proxy forward
   to port 8000 with an `X-Forwarded-Proto` header.

   The server refuses logins over plain `http://` except from `localhost` (for development).
   Responses carry HSTS, a strict Content-Security-Policy and no-store caching for the API.

   Environment variables:
   - `HARP_SSL_CERT`, `HARP_SSL_KEY`: certificate and key (PEM).
   - `HARP_HOST`, `HARP_PORT`: address to listen on. Default `0.0.0.0:8443`.
   - `HARP_ALLOW_HTTP=1`: accept logins over plain HTTP. Never use this on a shared server.
   - `HARP_SERVER_DATA`: where campaign state is kept. Default `~/.harp_server`.
   - `HARP_POLL_SECONDS`: how often the server polls TAPIS. Default `15`.

## Using the UI

Open `https://<server-hostname>:8443/`. The whole UI is one page, `profiling.html` (repository root), with four tabs:

| Tab | What it does |
|---|---|
| 1 Configure | Pick a TAPIS app from the dropdown, set up a sweep (run type, repetitions, timeout, single or list values per parameter), tick TAPIS systems and queues and open each one to set its hardware configurations: cores per job, memory and max minutes, with the queue's TAPIS limits shown. Add several configurations to profile one queue with different hardware. Add the sweep to the plan; repeat for more sweeps or apps. The plan is kept in the browser until it is submitted. |
| 2 Review & submit | The server checks every sweep against TAPIS and shows exactly what each job will ask for. Pick the results folder (any TAPIS system) and an allocation per system, confirm, submit. Nothing is submitted unless every sweep is valid. |
| 3 Jobs | All sweeps with progress and cancel; every TAPIS job with its runs done (e.g. `2 / 5`), sortable and filterable. Refreshes while jobs run. |
| 4 Profiled data | Pick an app from the dropdown: every execution with its parameters, hardware and time; median time by run type, hardware and parameter value; CSV export. **Build an estimator** runs the build phase from here (below). |

Log in with your TAPIS tenant (for example `https://icicle.tapis.io`) and your TAPIS username and password. The
page sends them over HTTPS to this server, which gets a token with `tapipy`, drops the password straight away and
keeps the token on the server. The browser only gets a session cookie that JavaScript cannot read. When the token
expires, running sweeps pause (`WAITING_FOR_LOGIN`) and resume as soon as you log in again.

**Runs done while a job runs.** The job runner rewrites `harp_progress.json` (runs done, failed, total,
current run) in the job's output folder after every run. While a job is RUNNING, the server asks TAPIS for the
job's `execSystemOutputDir` (`getJob`) and reads that file through TAPIS Files on the execution system. When the
job ends it reads the archived `harp_job_summary.json` for the final count. The runner also rewrites
`harp_profile.csv` after every run, so runs that finished are kept even if the job hits its time limit.

### Build an estimator (the build phase, from the Profiled data tab)

1. **Pool sweeps.** Tick the app's sweeps to learn from. Training needs SD, FS and test_data runs (at least 2, 4
   and 1), so one sweep of each run type, or sweeps that mix them.
2. **Standardize.** The server reads those sweeps' profiling rows through TAPIS and writes them in the format the
   pipeline reads (`run_config, run_type, sys_*, run_<param>, walltime`). It adds what each job was given
   (`sys_alloc_cores`, `sys_alloc_mem_mb`) so the models can learn from the hardware configurations, turns
   true/false into 1/0, and leaves out runs without a time and columns that are not filled in every run (the
   pipeline cannot handle gaps). The page shows the run counts per type, what the models will learn from, and
   what was left out; **Download training CSV** gives you the file.
3. **Build.** The server runs the pipeline's own build modules on this machine (`pipeline/modules/pipeline.py`:
   `data_preprocessor`, which removes outliers, scales and runs PCA, then `model_trainer`, which trains LR, NN and DTR
   on SD, SD+25FS, SD+50FS and SD+75FS, with and without padding). The dataset, the PCA dataset, `model_commons.csv` and
   every model (`.pkl`, `.h5`) are copied through TAPIS to `<folder>/builds/<app>_<timestamp>/`. The page shows
   each model's average error and how often it predicts too low, best first.

The server runs the build with its own Python (`HARP_BUILD_PYTHON` to use another), which needs
`requirements-build.txt` (pandas < 3, scikit-learn, TensorFlow); `scripts/run_local.sh` installs it. Builds run one
at a time and are kept in `~/.harp_server/builds/`.

### API

| Route | Used for |
|---|---|
| `POST /api/login`, `POST /api/logout`, `GET /api/me` | Session (TAPIS token kept on the server). |
| `GET /api/tapis/apps` | Your TAPIS apps with their sweep parameters (from the app notes). |
| `GET /api/tapis/exec-systems` | Systems you can run on, with their queues and limits (`batchLogicalQueues`). |
| `GET /api/tapis/systems` | Every system, for the results folder (storage-only systems too). |
| `POST /api/plan/preview` | Check a plan: per sweep, its summary and each job's resolved hardware, or why it is refused. |
| `POST /api/plan/submit` | Submit a plan, all or nothing. |
| `GET /api/campaigns`, `POST /api/campaigns/<id>/cancel` | Sweeps and cancelling one. |
| `GET /api/jobs` | Every TAPIS job, with hardware and runs done. |
| `GET /api/apps/<id>/profile`, `GET /api/apps/<id>/profile.csv` | An app's profiled data, merged across its sweeps. |
| `POST /api/apps/<id>/training-data`, `GET /api/apps/<id>/training-data.csv?sweeps=a,b` | Pool and standardize sweeps for the build phase. |
| `POST /api/apps/<id>/builds`, `GET /api/builds?app_id=` | Build models from those sweeps; builds with their steps and scores. |

A plan looks like:

```json
{"storage": {"system_id": "pitzer", "path": "/fs/scratch/PAS2271/harp_runs"},
 "allocations": {"pitzer": "-A PAS2271"},
 "sweeps": [{"app_id": "harp-sweep-euler", "name": "sd", "run_type": "SD", "repetitions": 3, "timeout_min": 20,
             "params": {"method": ["pow", "factorial"], "n": ["10", "100"]},
             "targets": [{"system_id": "pitzer", "queue": "serial"},
                         {"system_id": "pitzer", "queue": "serial", "cores": 8}]}]}
```
Leave `cores`, `memory_mb` or `max_minutes` out to use the defaults from the queue.

`DEMO/ui/` has an offline copy of the page with sample data (it mirrors these rules in JavaScript).

## Tests

```bash
cd harp_server && python3 -m pytest -q tests
```
The tests use an in-memory fake of TAPIS (`tests/fake_tapis.py`). The job runner is tested by
really running the Euler example.
