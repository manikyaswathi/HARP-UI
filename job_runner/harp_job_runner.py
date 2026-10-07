#!/usr/bin/env python3
"""
HARP sweep job runner - the entry point of one TAPIS job.

The HARP server splits a sweep into many TAPIS jobs. Each job gets a small
JSON spec (URL-safe base64, as the first app argument) that lists the
parameter combinations it must execute. For every combination and repetition
this runner:

  1. copies the application work folder to a scratch directory,
  2. runs the application command with the combination's parameters,
  3. measures the walltime and collects the hardware details,
  4. appends one row to harp_profile.csv in the TAPIS output directory.

TAPIS archives the output directory to the storage location chosen in the
HARP UI, where the server merges the CSVs of all jobs.

The CSV columns match Post_Execution_Scripts/basic/DataScrapper.py so the
merged file can be fed to the existing build phase unchanged.

Only the Python standard library is required; psutil is used when present.
"""

import base64
import csv
import json
import os
import platform
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

PROFILE_FILE_NAME = "harp_profile.csv"
FAILURES_FILE_NAME = "harp_failures.csv"
SUMMARY_FILE_NAME = "harp_job_summary.json"


def decode_spec(arg):
    """The spec is passed as base64 JSON, a path to a JSON file, or raw JSON."""
    if os.path.isfile(arg):
        with open(arg) as f:
            return json.load(f)
    if arg.lstrip().startswith("{"):
        return json.loads(arg)
    padded = arg.strip() + "=" * (-len(arg.strip()) % 4)
    return json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))


def output_dir():
    """TAPIS mounts the job output directory at /TapisOutput in the container
    and exports its host path as _tapisExecSystemOutputDir."""
    for candidate in ("/TapisOutput", os.environ.get("_tapisExecSystemOutputDir")):
        if candidate and os.path.isdir(candidate) and os.access(candidate, os.W_OK):
            return candidate
    fallback = os.path.join(os.getcwd(), "harp_output")
    os.makedirs(fallback, exist_ok=True)
    return fallback


def system_details():
    uname = platform.uname()
    details = {
        "sys_name": uname.system,
        "sys_processor": uname.processor or uname.machine,
    }
    try:
        import psutil
        freq = psutil.cpu_freq()
        details["sys_phy_cores_count"] = psutil.cpu_count(logical=False)
        details["sys_tot_cores_count"] = psutil.cpu_count(logical=True)
        details["sys_cpufreq_mhz"] = max(freq.max, freq.min, freq.current) if freq else ""
        details["sys_phy_mem_bytes"] = psutil.virtual_memory().total
        details["sys_swap_mem_bytes"] = psutil.swap_memory().total
    except ImportError:
        cores = os.cpu_count()
        details["sys_phy_cores_count"] = cores
        details["sys_tot_cores_count"] = cores
        details["sys_cpufreq_mhz"] = ""
        details["sys_phy_mem_bytes"] = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
        details["sys_swap_mem_bytes"] = ""
    return details


def gpu_details():
    """GPUs visible to the job (Singularity needs --nv). Zero when there are none."""
    details = {"sys_gpu_count": 0, "sys_gpu_name": "none", "sys_gpu_mem_mb": 0}
    if not shutil.which("nvidia-smi"):
        return details
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=30).stdout
        gpus = [line.split(",") for line in out.strip().splitlines() if line.strip()]
        if gpus:
            details.update(sys_gpu_count=len(gpus), sys_gpu_name=gpus[0][0].strip(),
                           sys_gpu_mem_mb=int(float(gpus[0][1])))
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        pass
    return details


def build_command(template, params):
    # Format every token separately so parameter values never get re-split.
    return [token.format(**params) for token in shlex.split(template)]


def run_once(command, workdir, timeout):
    scratch = tempfile.mkdtemp(prefix="harp_run_")
    try:
        if workdir and os.path.isdir(workdir):
            shutil.copytree(workdir, scratch, dirs_exist_ok=True)
        start = time.perf_counter()
        try:
            proc = subprocess.run(command, cwd=scratch, capture_output=True, text=True, timeout=timeout)
            elapsed = time.perf_counter() - start
            return proc.returncode, elapsed, proc.stderr[-2000:]
        except subprocess.TimeoutExpired:
            return "timeout", time.perf_counter() - start, f"exceeded {timeout}s"
        except OSError as e:
            return "error", time.perf_counter() - start, str(e)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def append_rows(path, rows):
    if not rows:
        return
    fieldnames = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def run_job(spec, out_dir):
    job_name = spec["job_name"]
    command_template = spec["command"]
    workdir = spec.get("workdir") or os.environ.get("APP_HOME", "")
    repetitions = int(spec.get("repetitions", 1))
    timeout = spec.get("run_timeout_sec") or None
    sys_details = system_details()
    sys_details.update(gpu_details())

    profile_rows, failure_rows = [], []
    for combo in spec["combinations"]:
        params = combo["parameters"]
        command = build_command(command_template, params)
        for rep in range(repetitions):
            run_config = f"{job_name}.run-{combo['index']}.iteration-{rep}"
            print(f"[HARP] {run_config}: {' '.join(command)}", flush=True)
            status, walltime, stderr = run_once(command, workdir, timeout)
            if status == 0:
                row = {"run_config": run_config, "run_type": combo["run_type"]}
                row.update(sys_details)
                row.update({f"run_{k}": v for k, v in params.items()})
                row["walltime"] = round(walltime, 6)
                profile_rows.append(row)
            else:
                print(f"[HARP] {run_config} failed ({status}): {stderr}", flush=True)
                failure_rows.append({"run_config": run_config, "run_type": combo["run_type"],
                                     "status": status, "parameters": json.dumps(params),
                                     "stderr": stderr})

    append_rows(os.path.join(out_dir, PROFILE_FILE_NAME), profile_rows)
    append_rows(os.path.join(out_dir, FAILURES_FILE_NAME), failure_rows)
    summary = {"job_name": job_name, "succeeded_runs": len(profile_rows),
               "failed_runs": len(failure_rows), "host": platform.node(),
               "tapis_job_uuid": os.environ.get("_tapisJobUUID", "")}
    with open(os.path.join(out_dir, SUMMARY_FILE_NAME), "w") as f:
        json.dump(summary, f, indent=2)
    print(f"[HARP] done: {summary}", flush=True)
    return summary


def main(argv):
    if len(argv) < 2:
        print("usage: harp_job_runner.py <base64-spec | spec.json>", file=sys.stderr)
        return 2
    out_dir = output_dir()
    print(f"[HARP] writing results to {out_dir} (TAPIS output dir on host: "
          f"{os.environ.get('_tapisExecSystemOutputDir', 'not set')})", flush=True)
    summary = run_job(decode_spec(argv[1]), out_dir)
    # Fail the TAPIS job only when nothing could be profiled.
    return 0 if summary["succeeded_runs"] > 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
