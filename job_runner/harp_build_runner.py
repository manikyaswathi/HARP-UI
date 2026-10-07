#!/usr/bin/env python3
"""
HARP build job: train the estimator models from a standardized profiling CSV.

Runs inside the HARP build container as a TAPIS job (DockerFiles/Dockerfile_HARP_Build),
or directly on the HARP server for "this computer" builds. It runs the HARP
pipeline's own build modules (pipeline/modules/pipeline.py):

  data_preprocessor  outlier removal, scaling and PCA        -> full_dataset_pca.csv
  model_trainer      LR / NN / DTR on SD, SD+25FS, ...        -> models/, model_commons.csv

Usage:
  harp_build_runner.py <spec> [--input CSV] [--output DIR]

<spec> is URL-safe base64 JSON (or a JSON file or raw JSON):
  {"name": "euler_number", "dataset_file": "euler_number_2026_10_07_12_00_00.csv",
   "models": ["LR", "NN", "DTR"], "training_sets": ["SD", "SD+25FS", "SD+50FS", "SD+75FS"]}

The CSV is found at --input, or in the TAPIS input folder (/TapisInput or
$_tapisExecSystemInputDir) under dataset_file. Results go to --output, or the
TAPIS output folder, which TAPIS archives to the storage folder you chose:
  <dataset_file>, pipeline_config.json, full_dataset_pca.csv, model_commons.csv,
  models/*, harp_build_summary.json, and harp_progress.json while it runs.
"""

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PIPELINE_HOME = os.environ.get("HARP_PIPELINE_HOME") or next(
    (p for p in ("/opt/harp/pipeline", os.path.join(os.path.dirname(HERE), "pipeline")) if os.path.isdir(p)), "")
SUMMARY_FILE_NAME = "harp_build_summary.json"
PROGRESS_FILE_NAME = "harp_progress.json"
STEPS = ["preprocess", "train"]


def decode_spec(arg):
    if os.path.isfile(arg):
        with open(arg) as f:
            return json.load(f)
    if arg.lstrip().startswith("{"):
        return json.loads(arg)
    padded = arg.strip() + "=" * (-len(arg.strip()) % 4)
    return json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))


def find_input(name, given=None):
    for candidate in ([given] if given else []) + [
            os.path.join(d, name) for d in ("/TapisInput", os.environ.get("_tapisExecSystemInputDir") or "", os.getcwd()) if d]:
        if candidate and os.path.isfile(candidate):
            return candidate
    raise SystemExit(f"[HARP] training data {name} not found (looked in /TapisInput, $_tapisExecSystemInputDir and here)")


def output_dir(given=None):
    for candidate in ([given] if given else []) + ["/TapisOutput", os.environ.get("_tapisExecSystemOutputDir")]:
        if candidate:
            try:
                os.makedirs(candidate, exist_ok=True)
            except OSError:
                continue
            if os.access(candidate, os.W_OK):
                return candidate
    fallback = os.path.join(os.getcwd(), "harp_output")
    os.makedirs(fallback, exist_ok=True)
    return fallback


def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


def run(spec, input_csv, out):
    name, dataset_file = spec["name"], spec["dataset_file"]
    progress = {"step": None, "steps": {s: "waiting" for s in STEPS}, "updated_at": None}

    def report(step=None, state=None):
        if step:
            progress["step"], progress["steps"][step] = step, state
        progress["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        write_json(os.path.join(out, PROGRESS_FILE_NAME), progress)

    work = tempfile.mkdtemp(prefix="harp_build_")
    app_dir = os.path.join(work, "TARGET_APP", name)
    store = os.path.join(work, "HARP_STORE")
    os.makedirs(app_dir)
    shutil.copy(input_csv, os.path.join(app_dir, dataset_file))
    config = {"application": name, "application_category": "basic", "cheetah_app_directory": name,
              "modules_to_run": ["build"], "runs": [], "project_account": "NA"}
    cfg = os.path.join(app_dir, "pipeline_config.json")
    write_json(cfg, config)
    env = {**os.environ, "TARGET_APP": os.path.join(work, "TARGET_APP"), "HARP_STORE": store,
           "PIPELINE_HOME": PIPELINE_HOME, "TF_CPP_MIN_LOG_LEVEL": "2", "PYTHONUNBUFFERED": "1",
           "HARP_MODELS": ",".join(spec.get("models") or []), "HARP_TRAINING_SETS": ",".join(spec.get("training_sets") or [])}
    app_home = os.path.join(store, "applications", "basic", name)
    summary = {"name": name, "ok": False, "error": None, "files": []}
    report()
    try:
        for step, module, expected in (("preprocess", "data_preprocessor", os.path.join(app_home, "train", "full_dataset_pca.csv")),
                                       ("train", "model_trainer", os.path.join(app_home, "model_commons.csv"))):
            report(step, "running")
            print(f"[HARP] {step}: pipeline.py --module {module}", flush=True)
            p = subprocess.run([sys.executable, os.path.join(PIPELINE_HOME, "modules", "pipeline.py"), "--module", module,
                                "--config", cfg], cwd=os.path.join(PIPELINE_HOME, "modules"), env=env)
            if p.returncode != 0 or not os.path.isfile(expected):
                # the pipeline prints errors and can still exit 0, so check what it wrote
                report(step, "failed")
                raise RuntimeError(f"{module} failed" + ("" if p.returncode else f": wrote no {os.path.basename(expected)}"))
            report(step, "done")
        outputs = [(os.path.join(app_dir, dataset_file), dataset_file), (cfg, "pipeline_config.json"),
                   (os.path.join(app_home, "train", "full_dataset_pca.csv"), "full_dataset_pca.csv"),
                   (os.path.join(app_home, "model_commons.csv"), "model_commons.csv")]
        models = os.path.join(app_home, "models")
        outputs += [(os.path.join(models, f), f"models/{f}") for f in sorted(os.listdir(models))]
        os.makedirs(os.path.join(out, "models"), exist_ok=True)
        for src, rel in outputs:
            shutil.copy(src, os.path.join(out, rel))
        summary.update(ok=True, files=[rel for _, rel in outputs])
    except RuntimeError as e:
        summary["error"] = str(e)
    finally:
        write_json(os.path.join(out, SUMMARY_FILE_NAME), summary)
        shutil.rmtree(work, ignore_errors=True)
    print(f"[HARP] build {'done' if summary['ok'] else 'failed'}: {summary['error'] or len(summary['files'])} files", flush=True)
    return summary


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("spec")
    ap.add_argument("--input")
    ap.add_argument("--output")
    args = ap.parse_args(argv)
    spec = decode_spec(args.spec)
    out = output_dir(args.output)
    print(f"[HARP] writing results to {out}", flush=True)
    return 0 if run(spec, find_input(spec["dataset_file"], args.input), out)["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
