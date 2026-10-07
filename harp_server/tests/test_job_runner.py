import base64
import csv
import json
import os

import harp_job_runner

EULER_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "examples", "01-eulers_number"))


def _spec(**over):
    spec = {"job_name": "euler-SD-0000", "command": "python3 calc_e.py {method} {n} {precision}",
            "workdir": EULER_DIR, "repetitions": 2, "run_timeout_sec": 60,
            "combinations": [
                {"index": 0, "run_type": "SD", "parameters": {"method": "pow", "n": 10, "precision": 64}},
                {"index": 1, "run_type": "SD", "parameters": {"method": "bogus", "n": 10, "precision": 64}},
            ]}
    spec.update(over)
    return spec


def test_runner_profiles_successful_runs_and_records_failures(tmp_path):
    summary = harp_job_runner.run_job(_spec(), str(tmp_path))
    assert summary["succeeded_runs"] == 2 and summary["failed_runs"] == 2
    with open(tmp_path / "harp_progress.json") as f:
        progress = json.load(f)
    assert (progress["runs_done"], progress["runs_failed"], progress["runs_total"], progress["current"]) == (4, 2, 4, None)

    with open(tmp_path / "harp_profile.csv") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 2
    cols = list(rows[0])
    for c in ("run_config", "run_type", "sys_name", "sys_tot_cores_count", "run_method", "run_n", "walltime"):
        assert c in cols
    assert cols[-1] == "walltime"
    assert rows[0]["run_config"] == "euler-SD-0000.run-0.iteration-0"
    assert float(rows[0]["walltime"]) > 0

    with open(tmp_path / "harp_failures.csv") as f:
        failures = list(csv.DictReader(f))
    assert {r["run_config"] for r in failures} == {"euler-SD-0000.run-1.iteration-0", "euler-SD-0000.run-1.iteration-1"}


def test_spec_decoding_accepts_urlsafe_base64_without_padding():
    raw = json.dumps(_spec()).encode()
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    assert harp_job_runner.decode_spec(encoded)["job_name"] == "euler-SD-0000"


def test_main_exit_code_reflects_whether_anything_was_profiled(tmp_path, monkeypatch):
    monkeypatch.setattr(harp_job_runner, "output_dir", lambda: str(tmp_path))
    bad = _spec(combinations=[{"index": 0, "run_type": "SD", "parameters": {"method": "x", "n": 1, "precision": 1}}])
    assert harp_job_runner.main(["runner", json.dumps(bad)]) == 1
    assert harp_job_runner.main(["runner", json.dumps(_spec())]) == 0


def test_parameter_values_are_not_shell_split():
    cmd = harp_job_runner.build_command("prog --name {name}", {"name": "a b; rm -rf /"})
    assert cmd == ["prog", "--name", "a b; rm -rf /"]
