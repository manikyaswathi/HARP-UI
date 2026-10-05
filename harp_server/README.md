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
2. **Register the TAPIS app** from `JSON_Templates/harp_sweep_app.json`, filling in `id` and
   `containerImage`. Register it **on each tenant you use**, for example
   `client.apps.createAppVersion(**app_def)`.
3. **Register the systems and credentials** as in `Notebooks/Executing_HARP_using_TAPIS*.ipynb`.
4. **Run the server**:
   ```bash
   pip install -r harp_server/requirements.txt
   cd harp_server && uvicorn harp_server.app:app --host 0.0.0.0 --port 8000
   ```
   Environment variables:
   - `HARP_SERVER_DATA`: where campaign state is kept. Default `~/.harp_server`.
   - `HARP_POLL_SECONDS`: how often the server polls TAPIS. Default `15`.

## Using the UI

1. Log in with your TAPIS tenant, for example `https://icicle.tapis.io`.
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

If the server restarts, a running campaign pauses as `WAITING_FOR_LOGIN` and resumes
when you log in again.

## Tests

```bash
cd harp_server && python3 -m pytest -q tests
```
The tests use an in-memory fake of TAPIS (`tests/fake_tapis.py`). The job runner is tested by
really running the Euler example.
