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
| `harp_server/sweep.py` | Validates the spec, expands every parameter combination (SD / FS / test_data) and splits it into jobs. **split** spreads the jobs round-robin over the systems (by weight). **replicate** runs every job on every system so you can profile across hardware. |
| `harp_server/campaign.py` | Submits jobs without exceeding each system's *max concurrent jobs*, polls TAPIS for job status, merges the CSVs when every job has ended, and lets you cancel or resubmit failed jobs. Each campaign's state is saved as a JSON file, so it survives a server restart. |
| `harp_server/tapis_gateway.py` | The only code that talks to TAPIS. It also runs the access checks: storage gets mkdir → write → read → delete, and each execution system gets its credentials, queue and app checked. |
| `harp_server/app.py` + `static/` | The REST API and the web UI. |
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
4. **Run the server over HTTPS** (it receives TAPIS passwords, so it must use HTTPS):
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

**The profiling page** (`profiling.html` at the repository root, served at `/profiling`) has four tabs:

| Tab | What it does |
|---|---|
| 1 Configure | Pick a TAPIS app, set up a sweep (run type, repetitions, timeout, single or list values per parameter), tick TAPIS systems and queues (queue limits come from the system's `batchLogicalQueues`), open each queue to set its hardware configurations, and add it to the plan. A configuration is cores per job (default: the queue's max), memory (default 4 GB per core) and max minutes (default: runs per job × timeout + 10), all capped at the queue's limits, always on 1 node. Add several configurations to profile one queue with different hardware; each becomes its own TAPIS target, and the profiled data has a `hardware` column naming it. Repeat for more sweeps or apps. The plan is kept in the browser until it is submitted. |
| 2 Review & submit | Every sweep x queue in the plan, checked by the server; results folder, allocation, confirm, submit. Nothing is submitted unless every sweep is valid. |
| 3 Jobs | All sweeps with progress and cancel; every TAPIS job with its runs done (e.g. `2 / 5`), sortable and filterable. Refreshes while jobs run. |
| 4 Profiled data | Pick an app from the dropdown: every execution with its parameters, system and time; median time by run type, system and parameter value; CSV export. |

**Runs done while a job runs.** The job runner rewrites `harp_progress.json` (runs done, failed, total,
current run) in the job's output folder after every run. While a job is RUNNING, the server asks TAPIS for the
job's `execSystemOutputDir` (`getJob`) and reads that file through TAPIS Files on the execution system. When the
job ends it reads the archived `harp_job_summary.json` for the final count. The runner also rewrites
`harp_profile.csv` after every run, so runs that finished are kept even if the job hits its time limit.

`/runs` and `/profile-data` redirect to the Jobs and Profiled data tabs.

API used by these pages: `/api/tapis/exec-systems` (systems with queues), `/api/campaigns/batch/preview`,
`/api/campaigns/batch` (all-or-nothing submit), `/api/jobs`, `/api/apps/<id>/profile`.
`DEMO/ui/` has offline copies with sample data.


The iScheduler profiling page (`profiling.html` at the repo root) is served at `/profiling`. It logs in to
TAPIS through this server and lists your TAPIS apps; an app's sweep parameters come from the `harp`
block in its TAPIS `notes` (the app notebook writes it). The page below is the full sweep console at `/`.


1. Log in with your TAPIS tenant (for example `https://icicle.tapis.io`) and your TAPIS username
   and password. The page sends them over HTTPS to the HARP server. The server gets a token with
   `tapipy` (`get_tokens()`), drops the password from memory straight away, and keeps the token on
   the server. The browser gets only a session cookie that JavaScript cannot read, and never sees
   the token. You can paste an existing access token instead; the server checks it with TAPIS
   (`get_userinfo`) before trusting it.
   The header shows how long the session stays valid, with a **Renew token** button in its last
   30 minutes. When it expires, running campaigns pause (`WAITING_FOR_LOGIN`) and resume as soon as
   you log in again.
2. **Application:** fill in the command template, e.g. `python3 calc_e.py {method} {n} {precision}`,
   and the work folder inside the container.
3. **Sweep parameters:** add one block per run type, with one `name = v1, v2` line per parameter.
4. **Hardware:** add one row per TAPIS execution system. For each, set the queue, resources,
   scheduler options (e.g. `-A PAS0000`) and *max concurrent jobs*. Keep that at or below the
   queue's per-user job limit.
5. **Storage:** pick a TAPIS system and folder (use **Browse…**).
6. **Test access** checks every location through TAPIS. **Preview jobs** shows how the sweep
   will be split. **Launch sweep** starts the campaign.
7. The **Campaigns** tab tracks every job. When everything has ended, the campaign turns
   **DONE** and you get a notification and a **Download CSV** button. The file is already saved
   on the storage system.

If the server restarts or your token expires, a running campaign pauses as
`WAITING_FOR_LOGIN` and resumes when you log in again.

## Tests

```bash
cd harp_server && python3 -m pytest -q tests
```
The tests use an in-memory fake of TAPIS (`tests/fake_tapis.py`). The job runner is tested by
really running the Euler example.
