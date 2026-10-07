"""The profiling page's plan: built, defaulted and checked on the server from TAPIS."""
from test_api import _login, ctx  # noqa: F401  (fixture)


def sweep(**over):
    s = {"app_id": "harp-sweep-euler", "name": "sd", "run_type": "SD", "repetitions": 2, "timeout_min": 20,
         "params": {"method": ["pow", "factorial"], "n": ["10", "100"]},
         "targets": [{"system_id": "pitzer", "queue": "serial"}]}
    s.update(over)
    return s


def plan(*sweeps, **over):
    p = {"storage": {"system_id": "storage", "path": "/scratch/harp_runs"},
         "allocations": {"pitzer": "-A PAS2271", "stampede": "-A TG-X"}, "sweeps": list(sweeps) or [sweep()]}
    p.update(over)
    return p


def preview(client, p):
    r = client.post("/api/plan/preview", json=p)
    assert r.status_code == 200, r.text
    return r.json()


def test_apps_come_with_their_sweep_parameters(ctx):
    client, _, _ = ctx
    _login(client)
    [app] = client.get("/api/tapis/apps").json()
    assert app["label"] == "Euler number" and app["command"] == "python3 calc_e.py {method} {n}"
    assert [(p["name"], p["kind"], p["default"]) for p in app["params"]] == [("method", "string", "pow"), ("n", "int", "1000")]


def test_queues_say_whether_they_have_gpus(ctx):
    client, _, _ = ctx
    _login(client)
    pitzer = client.get("/api/tapis/exec-systems").json()[0]
    assert {q["name"]: q["gpu"] for q in pitzer["queues"]} == {"serial": False, "parallel": False, "gpuserial": True}


def test_defaults_come_from_the_queue(ctx):
    client, _, _ = ctx
    _login(client)
    r = preview(client, plan(sweep(targets=[{"system_id": "pitzer", "queue": "serial"},
                                            {"system_id": "pitzer", "queue": "gpuserial", "cores": 8},
                                            {"system_id": "stampede", "queue": "skx"}])))
    assert r["ok"], r
    [s] = r["sweeps"]
    serial, gpu, skx = s["targets"]
    # cores = queue max; memory = 4 GB per core capped at the queue; minutes = reps x timeout + 10 capped at the queue
    assert (serial["cores_per_node"], serial["memory_mb"], serial["max_minutes"]) == (40, 163840, 50)
    assert serial["node_count"] == 1 and serial["container_args"] is None and serial["scheduler_options"] == "-A PAS2271"
    assert (gpu["cores_per_node"], gpu["memory_mb"], gpu["container_args"]) == (8, 32768, "--nv")
    assert (skx["cores_per_node"], skx["scheduler_options"], skx["max_concurrent_jobs"]) == (48, "-A TG-X", 10)
    assert s["summary"]["jobs"] == 12 and s["summary"]["total_runs"] == 24


def test_time_limit_is_capped_at_the_queue(ctx):
    client, _, _ = ctx
    _login(client)
    r = preview(client, plan(sweep(repetitions=5, timeout_min=30)))
    assert r["sweeps"][0]["targets"][0]["max_minutes"] == 60


def test_configurations_on_one_queue_share_its_job_limit(ctx):
    client, _, _ = ctx
    _login(client)
    r = preview(client, plan(sweep(targets=[{"system_id": "pitzer", "queue": "serial", "cores": 40},
                                            {"system_id": "pitzer", "queue": "serial", "cores": 8}])))
    assert [t["max_concurrent_jobs"] for t in r["sweeps"][0]["targets"]] == [1, 1]


def test_limits_and_mistakes_are_refused(ctx):
    client, _, _ = ctx
    _login(client)
    cases = [
        (sweep(targets=[{"system_id": "pitzer", "queue": "serial", "cores": 48}]), "at most 40 cores"),
        (sweep(targets=[{"system_id": "pitzer", "queue": "serial", "memory_mb": 999999}]), "at most 163840 MB"),
        (sweep(targets=[{"system_id": "pitzer", "queue": "serial", "max_minutes": 61}]), "at most 60 minutes"),
        (sweep(targets=[{"system_id": "pitzer", "queue": "parallel"}]), "at least 2 nodes"),
        (sweep(targets=[{"system_id": "pitzer", "queue": "nope"}]), "no queue 'nope'"),
        (sweep(targets=[{"system_id": "storage", "queue": None}]), "cannot run jobs"),
        (sweep(targets=[{"system_id": "elsewhere", "queue": "q"}]), "could not read elsewhere"),
        (sweep(targets=[]), "tick at least one queue"),
        (sweep(targets=[{"system_id": "pitzer", "queue": "serial"}, {"system_id": "pitzer", "queue": "serial"}]),
         "two identical configurations"),
        (sweep(params={"method": ["pow"], "n": ["ten"]}), "'n' needs numbers"),
        (sweep(params={"method": ["pow"], "n": ["1"], "rm": ["-rf"]}), "no parameter 'rm'"),
        (sweep(app_id="someone-elses-app"), "not one of your TAPIS apps"),
        (sweep(name="bad name!"), "sweep name"),
    ]
    for i, (s, _) in enumerate(cases):
        if s["name"] == "sd":
            s["name"] = f"case{i}"
    r = preview(client, plan(*[s for s, _ in cases]))
    assert not r["ok"]
    for (_, expected), got in zip(cases, r["sweeps"]):
        assert not got["ok"] and expected in got["error"], (expected, got)


def test_storage_must_be_a_system_and_an_absolute_folder(ctx):
    client, _, _ = ctx
    _login(client)
    r = preview(client, plan(storage={"system_id": "storage", "path": "harp_runs"}))
    assert not r["ok"] and "absolute" in r["storage_error"] and r["sweeps"][0]["ok"]
    r = preview(client, plan(storage={}))   # before a folder is chosen the sweeps still get checked
    assert not r["ok"] and r["storage_error"] and r["sweeps"][0]["ok"] and r["sweeps"][0]["summary"]["jobs"] == 4


def test_the_command_comes_from_tapis_not_the_browser(ctx):
    client, manager, gateways = ctx
    _login(client)
    s = sweep(params={"method": ["pow"], "n": ["10"]})
    s["command"] = "rm -rf /"   # ignored
    created = client.post("/api/plan/submit", json=plan(s)).json()
    spec = manager.campaigns[created[0]["id"]]["spec"]
    assert spec["command"] == "python3 calc_e.py {method} {n}" and spec["workdir"] == "/app/01-eulers_number"
    assert spec["run_sets"][0]["parameters"] == {"method": ["pow"], "n": [10]}   # typed from the app's notes


def test_submit_is_all_or_nothing_and_jobs_ask_for_the_resolved_hardware(ctx):
    client, manager, gateways = ctx
    _login(client)
    bad = client.post("/api/plan/submit", json=plan(sweep(name="a"), sweep(name="b", targets=[])))
    assert bad.status_code == 400 and "sd" not in bad.json()["detail"] and "/ b" in bad.json()["detail"]
    assert manager.campaigns == {}
    ok = client.post("/api/plan/submit", json=plan(sweep(name="a"), sweep(name="b", repetitions=1))).json()
    assert [c["name"].split("-")[2] for c in ok] == ["a", "b"] and len(manager.campaigns) == 2
    manager.tick()
    req = gateways["alice"].jobs[gateways["alice"].submitted[0]]["request"]
    assert (req["execSystemLogicalQueue"], req["coresPerNode"], req["memoryMB"], req["nodeCount"]) == ("serial", 40, 163840, 1)
    assert req["parameterSet"]["schedulerOptions"][0]["arg"] == "-A PAS2271"


def test_empty_plan_is_refused(ctx):
    client, _, _ = ctx
    _login(client)
    assert client.post("/api/plan/preview", json={"sweeps": []}).status_code == 400


def test_removed_routes_are_gone(ctx):
    client, _, _ = ctx
    _login(client)
    for method, path in (("post", "/api/campaigns"), ("post", "/api/campaigns/batch"), ("post", "/api/tapis/check"),
                         ("get", "/api/tapis/files"), ("get", "/static/app.js"), ("post", "/api/campaigns/x/resubmit")):
        assert getattr(client, method)(path).status_code in (404, 405), path


def test_results_can_be_copied_elsewhere_when_the_sweep_ends(ctx):
    client, manager, gateways = ctx
    _login(client)
    p = plan(sweep(params={"method": ["pow"], "n": ["10"]}), move_to={"system_id": "pitzer", "path": "/fs/project/keep"})
    assert preview(client, p)["ok"]
    [c] = client.post("/api/plan/submit", json=p).json()
    gw = gateways["alice"]
    camp = manager.campaigns[c["id"]]
    assert camp["spec"]["move_to"] == {"system_id": "pitzer", "path": "/fs/project/keep"}
    manager.tick()
    for u in gw.submitted:
        gw.finish(u, b"run_type,walltime\nSD,1.0\n")
    manager.tick()                     # finalize: merged CSV written, copy queued
    move = camp["result"]["move"]
    assert camp["status"] == "DONE" and move["status"] == "PENDING"
    manager.tick()                     # transfer task created (no job, no allocation)
    assert move["status"] == "COPYING" and gw.transfers[0][:3] == ("storage", camp["campaign_dir"], "pitzer")
    manager.tick()
    assert move["status"] == "DONE"
    folder = camp["campaign_dir"].rsplit("/", 1)[-1]
    assert ("pitzer", f"/fs/project/keep/{folder}/{camp['result']['csv_path'].rsplit('/', 1)[-1]}") in gw.files
    assert client.get("/api/campaigns").json()[0]["result"]["move"]["status"] == "DONE"


def test_failed_copy_is_reported_and_bad_locations_refused(ctx):
    client, manager, gateways = ctx
    _login(client)
    r = preview(client, plan(move_to={"system_id": "pitzer", "path": "relative"}))
    assert not r["ok"] and "absolute" in r["storage_error"]
    r = preview(client, plan(move_to={"system_id": "storage", "path": "/scratch/harp_runs/"}))
    assert not r["ok"] and "same as the results folder" in r["storage_error"]
    [c] = client.post("/api/plan/submit", json=plan(sweep(params={"method": ["pow"], "n": ["10"]}),
                                                     move_to={"system_id": "pitzer", "path": "/x"})).json()
    gw = gateways["alice"]
    gw.transfer_result = "FAILED"
    manager.tick()
    for u in gw.submitted:
        gw.finish(u, b"run_type,walltime\nSD,1.0\n")
    for _ in range(3):
        manager.tick()
    move = manager.campaigns[c["id"]]["result"]["move"]
    assert move["status"] == "FAILED" and "FAILED" in move["error"]


def test_apps_say_where_their_parameters_and_defaults_come_from(ctx):
    client, _, gateways = ctx
    _login(client)
    [app] = client.get("/api/tapis/apps").json()
    assert app["params_from"] == "notes" and app["defaults"] == {}
