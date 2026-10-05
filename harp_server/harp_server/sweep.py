"""
Sweep planning: validate a campaign spec, expand every parameter combination
and split the combinations into TAPIS jobs spread across the target hardware.
"""

import itertools
import re
import string

RUN_TYPES = ("SD", "FS", "test_data")
DISTRIBUTIONS = ("split", "replicate")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


class SpecError(ValueError):
    pass


def _require(cond, msg):
    if not cond:
        raise SpecError(msg)


def validate_spec(spec: dict) -> dict:
    """Validate a campaign spec and fill in defaults. Returns a normalized copy."""
    _require(isinstance(spec, dict), "spec must be a JSON object")
    s = dict(spec)
    _require(NAME_RE.match(str(s.get("name", ""))), "name: letters, digits, '_', '-', '.' only (max 64)")
    _require(NAME_RE.match(str(s.get("application", ""))), "application: letters, digits, '_', '-', '.' only")
    _require(str(s.get("command", "")).strip(), "command is required, e.g. 'python3 calc_e.py {method} {n}'")
    s.setdefault("workdir", "")
    s["repetitions"] = int(s.get("repetitions", 1))
    _require(s["repetitions"] >= 1, "repetitions must be >= 1")
    s["combos_per_job"] = int(s.get("combos_per_job", 1))
    _require(s["combos_per_job"] >= 1, "combos_per_job must be >= 1")
    s["run_timeout_sec"] = int(s.get("run_timeout_sec") or 0) or None
    s["distribution"] = s.get("distribution", "split")
    _require(s["distribution"] in DISTRIBUTIONS, f"distribution must be one of {DISTRIBUTIONS}")

    run_sets = s.get("run_sets") or []
    _require(run_sets, "at least one run set (SD / FS / test_data) is required")
    placeholders = {f for _, f, _, _ in string.Formatter().parse(s["command"]) if f}
    for i, rs in enumerate(run_sets):
        _require(rs.get("run_type") in RUN_TYPES, f"run_sets[{i}].run_type must be one of {RUN_TYPES}")
        params = rs.get("parameters") or {}
        _require(params, f"run_sets[{i}] has no parameters")
        for name, values in params.items():
            _require(re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name), f"invalid parameter name '{name}'")
            _require(isinstance(values, list) and values, f"parameter '{name}' needs a non-empty list of values")
        missing = placeholders - set(params)
        _require(not missing, f"run_sets[{i}] has no values for command placeholders {sorted(missing)}")

    targets = s.get("targets") or []
    _require(targets, "at least one target system (hardware) is required")
    norm_targets = []
    for i, t in enumerate(targets):
        _require(t.get("system_id"), f"targets[{i}].system_id is required")
        _require(t.get("app_id"), f"targets[{i}].app_id is required")
        nt = dict(t)
        nt.setdefault("app_version", "1.0.0")
        nt["max_concurrent_jobs"] = int(t.get("max_concurrent_jobs") or 1)
        nt["weight"] = int(t.get("weight") or 1)
        _require(nt["max_concurrent_jobs"] >= 1 and nt["weight"] >= 1,
                 f"targets[{i}]: max_concurrent_jobs and weight must be >= 1")
        for key in ("node_count", "cores_per_node", "memory_mb", "max_minutes"):
            if nt.get(key) in ("", None):
                nt.pop(key, None)
            else:
                nt[key] = int(nt[key])
        nt["key"] = f"t{i}"
        norm_targets.append(nt)
    s["targets"] = norm_targets

    storage = s.get("storage") or {}
    _require(storage.get("system_id") and storage.get("path"),
             "storage.system_id and storage.path (where profiling CSVs are stored) are required")
    s["storage"] = {"system_id": storage["system_id"], "path": "/" + storage["path"].strip("/")}
    return s


def expand_combinations(spec: dict) -> list:
    """Cartesian product of every run set's parameters, tagged with the run type."""
    combos = []
    for rs in spec["run_sets"]:
        names = list(rs["parameters"])
        for values in itertools.product(*(rs["parameters"][n] for n in names)):
            combos.append({"index": len(combos), "run_type": rs["run_type"],
                           "parameters": dict(zip(names, values))})
    return combos


def _weighted_cycle(targets):
    order = [t for t in targets for _ in range(t["weight"])]
    return itertools.cycle(order)


def plan_jobs(spec: dict) -> list:
    """Split the combinations into jobs and assign each job to a target.

    split:     each chunk runs once, round-robin over targets (by weight).
    replicate: each chunk runs on every target, to profile across hardware.
    """
    combos = expand_combinations(spec)
    size = spec["combos_per_job"]
    # Keep run types apart so one job never mixes SD/FS/test runs.
    chunks = []
    for run_type in RUN_TYPES:
        typed = [c for c in combos if c["run_type"] == run_type]
        chunks += [typed[i:i + size] for i in range(0, len(typed), size)]

    jobs = []
    if spec["distribution"] == "replicate":
        assignments = [(chunk, t) for chunk in chunks for t in spec["targets"]]
    else:
        cycle = _weighted_cycle(spec["targets"])
        assignments = [(chunk, next(cycle)) for chunk in chunks]

    for n, (chunk, target) in enumerate(assignments):
        jobs.append({
            "name": f"{spec['name']}-{chunk[0]['run_type']}-{n:04d}",
            "target": target["key"],
            "system_id": target["system_id"],
            "run_type": chunk[0]["run_type"],
            "combinations": chunk,
        })
    return jobs


def summarize(spec: dict, jobs: list) -> dict:
    combos = expand_combinations(spec)
    per_target = {}
    for j in jobs:
        per_target.setdefault(j["system_id"], {"jobs": 0, "runs": 0})
        per_target[j["system_id"]]["jobs"] += 1
        per_target[j["system_id"]]["runs"] += len(j["combinations"]) * spec["repetitions"]
    per_type = {}
    for c in combos:
        per_type[c["run_type"]] = per_type.get(c["run_type"], 0) + 1
    return {"combinations": len(combos), "combinations_per_run_type": per_type,
            "jobs": len(jobs), "total_runs": sum(v["runs"] for v in per_target.values()),
            "per_system": per_target}
