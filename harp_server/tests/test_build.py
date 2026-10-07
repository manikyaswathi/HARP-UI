"""Build phase from the page: pool sweeps -> standardize -> HARP pipeline build -> TAPIS."""
import base64
import json
import os
import random
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from harp_server.app import create_app
from harp_server.build import build_name, standardize, wait_for
from fake_tapis import FakeGateway
from specs import euler_spec


def build_python():
    """A Python with pandas, scikit-learn and TensorFlow (HARP_BUILD_PYTHON), or None to skip."""
    py = os.environ.get("HARP_BUILD_PYTHON") or sys.executable
    ok = subprocess.run([py, "-c", "import tensorflow, sklearn, pandas"], capture_output=True).returncode == 0
    return py if ok else None


@pytest.fixture
def ctx(tmp_path):
    gateways = {}

    def login(base_url, username=None, password=None, access_token=None):
        return gateways.setdefault(username, FakeGateway(username))

    app = create_app(data_dir=str(tmp_path), login=login, start_poller=False, build_python=build_python())
    client = TestClient(app, base_url="https://testserver")
    assert client.post("/api/login", json={"base_url": "https://fake.tapis.io", "username": "alice",
                                           "password": "x"}).status_code == 200
    return client, app.state.manager, app.state.builds, gateways["alice"]


def euler_time(method, n, cores):
    base = 0.002 * (n / 1000) ** 0.5 if method == "pow" else 0.02 * (n / 1000) ** 1.2
    return base * (40 / cores) ** 0.3


def profile_csv(gw, uuid):
    """What the job runner would write for this job."""
    req = gw.jobs[uuid]["request"]
    spec = json.loads(base64.urlsafe_b64decode(req["parameterSet"]["appArgs"][0]["arg"]))
    lines = ["run_config,run_type,sys_name,sys_processor,sys_tot_cores_count,sys_phy_mem_bytes,run_method,run_n,walltime"]
    for c in spec["combinations"]:
        p = c["parameters"]
        for r in range(spec["repetitions"]):
            w = euler_time(p["method"], p["n"], req["coresPerNode"]) * random.uniform(0.95, 1.05)
            lines.append(f"{spec['job_name']}.run-{c['index']}.iteration-{r},{c['run_type']},Linux,x86_64,40,"
                         f"201326592000,{p['method']},{p['n']},{w:.6f}")
    return ("\n".join(lines) + "\n").encode()


def sweep(manager, gw, name, run_type, ns, cores=(40, 8)):
    spec = euler_spec(name=name, command="python3 calc_e.py {method} {n}", combos_per_job=1, repetitions=2, distribution="replicate",
                      run_sets=[{"run_type": run_type, "parameters": {"method": ["pow", "factorial"], "n": ns}}],
                      targets=[{"system_id": "pitzer", "queue": "serial", "app_id": "harp-sweep-euler",
                                "cores_per_node": c, "memory_mb": c * 4096, "max_concurrent_jobs": 50} for c in cores])
    c = manager.create(spec, gw)
    for _ in range(10):
        manager.tick()
        for u in gw.submitted:
            if gw.jobs[u]["status"] == "PENDING":
                gw.finish(u, profile_csv(gw, u))
        if c["status"] in ("DONE", "DONE_WITH_ERRORS"):
            break
    assert c["status"] == "DONE"
    return c["id"]


def test_standardize_makes_the_pipelines_input_format():
    cols = ["campaign", "system", "hardware", "sys_alloc_cores", "run_config", "run_type", "sys_name",
            "sys_gpu_count", "run_n", "run_flag", "run_added_later", "walltime"]
    rows = [{"campaign": "a", "system": "p", "hardware": "h", "sys_alloc_cores": "8", "run_config": f"j.run-{i}",
             "run_type": t, "sys_name": "Linux", "sys_gpu_count": "0", "run_n": str(10 * i), "run_flag": "True",
             "run_added_later": "x" if i == 0 else "", "walltime": "0.5"}
            for i, t in enumerate(["SD", "SD", "FS", "FS", "FS", "FS", "test_data", "bogus"])]
    rows.append(dict(rows[0], walltime=""))
    header, table, report = standardize(cols, rows)
    assert header == ["run_config", "run_type", "sys_alloc_cores", "sys_name", "sys_gpu_count", "run_n", "run_flag", "walltime"]
    assert "run_added_later" in report["dropped_columns"]
    assert report["dropped_rows"] == {"run type is not SD, FS or test_data": 1, "no walltime": 1}
    assert report["by_run_type"] == {"SD": 2, "FS": 4, "test_data": 1} and report["ready"]
    assert table[0]["run_flag"] == 1 and table[0]["sys_alloc_cores"] == 8   # bools and numbers made numeric
    assert report["varied_features"] == ["run_n"]


def test_build_name_is_lowercase_for_the_pipeline():
    assert build_name("Euler number") == "euler_number" and build_name("YOLO v11!") == "yolo_v11"


def test_training_data_says_what_is_missing(ctx):
    client, manager, _, gw = ctx
    sd = sweep(manager, gw, "sd", "SD", [10, 100])
    r = client.post("/api/apps/harp-sweep-euler/training-data", json={"sweeps": [sd]}).json()
    assert not r["ready"] and r["by_run_type"]["SD"] == 16
    assert any("FS" in p for p in r["problems"]) and any("test_data" in p for p in r["problems"])
    assert "sys_alloc_cores" in r["varied_features"]   # 8 vs 40 cores is a feature the model can learn from
    bad = client.post("/api/apps/harp-sweep-euler/builds", json={"sweeps": [sd], "storage": {"system_id": "storage", "path": "/m"}})
    assert bad.status_code == 400 and "not enough data" in bad.json()["detail"]
    assert client.post("/api/apps/harp-sweep-euler/training-data", json={"sweeps": ["nope"]}).status_code == 404


@pytest.mark.skipif(build_python() is None, reason="needs a Python with TensorFlow (set HARP_BUILD_PYTHON)")
def test_build_runs_the_harp_pipeline_and_saves_models_to_tapis(ctx):
    client, manager, builds, gw = ctx
    random.seed(1)
    ids = [sweep(manager, gw, "sd", "SD", [10, 100, 1000]), sweep(manager, gw, "fs", "FS", [5000, 10000, 20000]),
           sweep(manager, gw, "test", "test_data", [3000, 15000])]
    csv_text = client.get("/api/apps/harp-sweep-euler/training-data.csv", params={"sweeps": ",".join(ids)}).text
    assert csv_text.splitlines()[0].startswith("run_config,run_type,sys_alloc_cores")
    b = client.post("/api/apps/harp-sweep-euler/builds",
                    json={"sweeps": ids, "storage": {"system_id": "storage", "path": "/scratch/harp_models"}}).json()
    assert b["status"] in ("QUEUED", "RUNNING") and b["name"] == "euler_number"
    done = wait_for(builds, b["id"], timeout=900)
    assert done["status"] == "DONE", (done["error"], done["log"][-15:])
    assert len(done["metrics"]) == 24   # LR/NN/DTR x SD, SD+25FS, SD+50FS, SD+75FS x adjusted or not
    assert done["best"]["MAPE"] is not None
    uploaded = {p for (s, p) in gw.files if s == "storage" and p.startswith(done["dest"])}
    assert f"{done['dest']}/model_commons.csv" in uploaded and f"{done['dest']}/{done['dataset_file']}" in uploaded
    assert any(p.endswith(".pkl") for p in uploaded) and any(p.endswith(".h5") for p in uploaded)
    listed = client.get("/api/builds", params={"app_id": "harp-sweep-euler"}).json()
    assert listed[0]["id"] == b["id"] and listed[0]["status"] == "DONE"
