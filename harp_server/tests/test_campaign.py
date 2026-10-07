import base64
import csv
import io
import json

import pytest

from harp_server.campaign import CampaignManager, CampaignStore, build_job_request, merge_csvs
from fake_tapis import FakeGateway
from specs import euler_spec


def _csv(rows):
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=list(rows[0]))
    w.writeheader()
    w.writerows(rows)
    return out.getvalue().encode()


def _row(run_type, n, walltime, **extra):
    return {"run_config": f"r{n}", "run_type": run_type, "sys_name": "Linux", "run_method": "pow",
            "run_n": n, **extra, "walltime": walltime}


@pytest.fixture
def setup(tmp_path):
    gw = FakeGateway()
    manager = CampaignManager(CampaignStore(str(tmp_path)))
    return gw, manager


def test_create_writes_spec_to_storage_through_tapis(setup):
    gw, manager = setup
    c = manager.create(euler_spec(), gw)
    assert c["campaign_dir"].startswith("/scratch/harp_runs/euler-sweep-")
    spec_blob = gw.files[("storage", c["campaign_dir"] + "/campaign_spec.json")]
    assert json.loads(spec_blob)["name"] == "euler-sweep"
    assert all(j["status"] == "NOT_SUBMITTED" for j in c["jobs"])


def test_create_fails_when_storage_is_not_writable(setup):
    gw, manager = setup
    gw.readonly_systems.add("storage")
    with pytest.raises(Exception, match="permission denied"):
        manager.create(euler_spec(), gw)
    assert manager.campaigns == {}


def test_job_request_targets_system_and_archives_to_storage(setup):
    gw, manager = setup
    c = manager.create(euler_spec(), gw)
    job = c["jobs"][0]
    req = build_job_request(c, job)
    assert req["execSystemId"] == "pitzer" and req["execSystemLogicalQueue"] == "serial"
    assert req["archiveSystemId"] == "storage"
    assert req["archiveSystemDir"] == f"{c['campaign_dir']}/jobs/{job['name']}"
    assert req["archiveOnAppError"] is True
    assert req["parameterSet"]["schedulerOptions"] == [{"arg": "-A PAS0000"}]
    payload = json.loads(base64.urlsafe_b64decode(req["parameterSet"]["appArgs"][0]["arg"]))
    assert payload["combinations"] == job["combinations"]
    assert payload["command"] == "python3 calc_e.py {method} {n} {precision}"


def test_full_lifecycle_throttles_tracks_and_merges(setup):
    gw, manager = setup
    c = manager.create(euler_spec(), gw)  # 4 jobs: pitzer x2 (limit 2), stampede x2 (limit 1)

    manager.tick()
    by_system = {}
    for uuid in gw.submitted:
        by_system.setdefault(gw.jobs[uuid]["request"]["execSystemId"], []).append(uuid)
    assert len(by_system["pitzer"]) == 2 and len(by_system["stampede"]) == 1  # concurrency limits respected
    assert c["status"] == "RUNNING"

    # first stampede job finishes -> the second one gets submitted
    first_stampede = by_system["stampede"][0]
    gw.finish(first_stampede, _csv([_row("SD", 100, 0.5)]))
    manager.tick()
    assert len([u for u in gw.submitted if gw.jobs[u]["request"]["execSystemId"] == "stampede"]) == 2

    # finish everything; different jobs carry different columns
    for uuid in gw.submitted:
        if gw.jobs[uuid]["status"] != "FINISHED":
            run_type = json.loads(base64.urlsafe_b64decode(
                gw.jobs[uuid]["request"]["parameterSet"]["appArgs"][0]["arg"]))["combinations"][0]["run_type"]
            gw.finish(uuid, _csv([_row(run_type, 10, 0.1, run_precision=64)]))
    manager.tick()

    assert c["status"] == "DONE"
    result = c["result"]
    assert result["rows"] == 4 and result["jobs_finished"] == 4
    assert result["csv_path"].startswith(c["campaign_dir"] + "/euler_")  # name the build phase picks up
    merged = list(csv.DictReader(io.StringIO(gw.files[("storage", result["csv_path"])].decode())))
    assert {r["run_type"] for r in merged} == {"SD", "FS", "test_data"}
    assert list(merged[0])[-1] == "walltime"
    assert "run_precision" in merged[0]
    manifest = json.loads(gw.files[("storage", result["manifest_path"])])
    assert len(manifest["jobs"]) == 4


def test_failed_jobs_give_done_with_errors_and_can_be_resubmitted(setup):
    gw, manager = setup
    c = manager.create(euler_spec(targets=[euler_spec()["targets"][0] | {"max_concurrent_jobs": 10}]), gw)
    manager.tick()
    uuids = list(gw.submitted)
    gw.finish(uuids[0], _csv([_row("SD", 10, 0.1)]))
    for u in uuids[1:]:
        gw.finish(u, status="FAILED")
    manager.tick()
    assert c["status"] == "DONE_WITH_ERRORS"
    assert c["result"]["rows"] == 1

    assert manager.resubmit_failed(c) == len(uuids) - 1
    manager.tick()
    for u in gw.submitted[len(uuids):]:
        gw.finish(u, _csv([_row("FS", 1000, 1.0)]))
    manager.tick()
    assert c["status"] == "DONE"
    assert c["result"]["rows"] == len(uuids)


def test_submit_failures_end_the_job_and_no_data_means_failed(setup):
    gw, manager = setup
    gw.fail_submit_for = {"pitzer", "stampede"}
    c = manager.create(euler_spec(), gw)
    manager.tick()
    assert all(j["status"] == "SUBMIT_FAILED" for j in c["jobs"])
    assert "queue limit exceeded" in c["jobs"][0]["error"]
    assert c["status"] == "FAILED"


def test_campaign_pauses_without_login_and_survives_restart(tmp_path):
    gw = FakeGateway()
    manager = CampaignManager(CampaignStore(str(tmp_path)))
    c = manager.create(euler_spec(), gw)
    manager.tick()

    restarted = CampaignManager(CampaignStore(str(tmp_path)))
    restarted.tick()
    c2 = restarted.campaigns[c["id"]]
    assert c2["status"] == "WAITING_FOR_LOGIN"

    restarted.register_gateway(gw)
    restarted.tick()
    assert c2["status"] == "RUNNING"
    assert sum(1 for j in c2["jobs"] if j["uuid"]) == 3


def test_cancel_cancels_submitted_and_pending_jobs(setup):
    gw, manager = setup
    c = manager.create(euler_spec(), gw)
    manager.tick()
    manager.cancel(c)
    assert c["status"] == "CANCELLED"
    assert all(gw.jobs[u]["status"] == "CANCELLED" for u in gw.submitted)
    assert all(j["status"] in ("CANCELLED", "PENDING") for j in c["jobs"])
    manager.tick()
    assert c["status"] == "CANCELLED"


def test_merge_handles_differing_columns():
    merged, rows = merge_csvs([b"a,walltime\n1,2\n", b"a,b,walltime\n3,4,5\n"])
    assert rows == 2
    assert merged.decode().splitlines() == ["a,b,walltime", "1,,2", "3,4,5"]


def test_container_args_are_passed_to_tapis(setup):
    gw, manager = setup
    spec = euler_spec()
    spec["targets"][1]["container_args"] = "--nv"
    c = manager.create(spec, gw)
    by_system = {j["system_id"]: build_job_request(c, j) for j in c["jobs"]}
    assert by_system["stampede"]["parameterSet"]["containerArgs"] == [{"arg": "--nv"}]
    assert "containerArgs" not in by_system["pitzer"]["parameterSet"]
