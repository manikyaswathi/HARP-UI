import pytest

from harp_server.sweep import SpecError, expand_combinations, plan_jobs, summarize, validate_spec
from specs import euler_spec


def test_expand_counts_every_combination():
    spec = validate_spec(euler_spec())
    combos = expand_combinations(spec)
    assert len(combos) == 4 + 2 + 1
    assert combos[0] == {"index": 0, "run_type": "SD", "parameters": {"method": "pow", "n": 10, "precision": 64}}
    assert [c["index"] for c in combos] == list(range(7))


def test_split_chunks_by_run_type_and_round_robins_targets():
    spec = validate_spec(euler_spec())
    jobs = plan_jobs(spec)
    # SD: 4 combos / 2 -> 2 jobs, FS: 2 -> 1 job, test: 1 -> 1 job
    assert [j["run_type"] for j in jobs] == ["SD", "SD", "FS", "test_data"]
    assert [j["system_id"] for j in jobs] == ["pitzer", "stampede", "pitzer", "stampede"]
    assert all(len({c["run_type"] for c in j["combinations"]}) == 1 for j in jobs)
    assert len({j["name"] for j in jobs}) == len(jobs)


def test_weights_bias_assignment():
    s = euler_spec(combos_per_job=1)
    s["targets"][0]["weight"] = 3
    jobs = plan_jobs(validate_spec(s))
    systems = [j["system_id"] for j in jobs]
    assert systems[:4] == ["pitzer", "pitzer", "pitzer", "stampede"]


def test_replicate_runs_every_chunk_on_every_target():
    spec = validate_spec(euler_spec(distribution="replicate"))
    jobs = plan_jobs(spec)
    assert len(jobs) == 4 * 2
    summary = summarize(spec, jobs)
    assert summary["per_system"]["pitzer"]["runs"] == 7 * 2
    assert summary["per_system"]["stampede"]["runs"] == 7 * 2
    assert summary["total_runs"] == 28


@pytest.mark.parametrize("change, message", [
    ({"name": "bad name!"}, "name"),
    ({"command": ""}, "command"),
    ({"run_sets": []}, "run set"),
    ({"targets": []}, "target"),
    ({"storage": {"system_id": "s"}}, "storage"),
    ({"distribution": "everywhere"}, "distribution"),
    ({"command": "python3 calc_e.py {method} {missing}"}, "missing"),
])
def test_invalid_specs_are_rejected(change, message):
    with pytest.raises(SpecError, match=message):
        validate_spec(euler_spec(**change))


def test_storage_path_is_normalized():
    assert validate_spec(euler_spec())["storage"]["path"] == "/scratch/harp_runs"
