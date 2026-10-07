"""
The profiling page's plan -> campaign specs.

The page sends what the user picked: for each sweep the app, the parameter
values, repetitions, timeout and the queues with their hardware
configurations, plus allocations per system and a results folder. Everything
else is decided here, from TAPIS, so the browser is never trusted with it:

- the command, work folder and parameter types come from the app's TAPIS notes;
- cores, memory and time limit default from the queue (TAPIS LogicalQueue) and
  are refused when they exceed its limits;
- every job uses one node; GPU queues get --nv; configurations on one queue
  share its per-user job limit.
"""

import re
from datetime import datetime, timezone

from .sweep import RUN_TYPES, SpecError

MB_PER_CORE = 4096          # default memory: 4 GB per core
MIN_MEMORY_MB = 4096
STARTUP_MINUTES = 10        # added to runs x timeout for container start-up
DEFAULT_CONCURRENT = 4      # jobs at once on a queue that sets no per-user limit
MAX_CONCURRENT = 10
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,30}$")


def harp_app(a):
    """A TAPIS app as the page shows it. Its sweep parameters come from the
    `harp` block in the app's notes (the app notebook writes it); without a
    params list they are read from the {placeholders} in the command."""
    h = (a.get("notes") or {}).get("harp") or {}
    command = h.get("command") or ""
    params = h.get("params") if isinstance(h.get("params"), list) else []
    if not params and command:
        params = [{"name": n, "arg": "{%s}" % n} for n in re.findall(r"\{(\w+)\}", command)]
    return {"id": a.get("id"), "version": a.get("version"), "label": h.get("label") or a.get("id"),
            "image": a.get("image") or "", "runtime": a.get("runtime") or "SINGULARITY",
            "description": a.get("description") or "", "command": command, "workdir": h.get("workdir") or "",
            "params": [{"name": p["name"], "arg": p.get("arg") or "", "kind": p.get("kind") or "string",
                        "default": "" if p.get("default") is None else str(p["default"])}
                       for p in params if isinstance(p, dict) and p.get("name")]}


def is_gpu(q):
    return "gpu" in " ".join(str(q.get(k) or "") for k in ("name", "description", "hpc_queue")).lower()


def _limit(q, key):
    v = q.get(key)
    return v if isinstance(v, (int, float)) and v > 0 else None


def _cap(v, mx):
    return min(v, mx) if mx else v


def _positive_int(value, what):
    if value in (None, ""):
        return None
    try:
        v = int(value)
    except (TypeError, ValueError):
        raise SpecError(f"{what} must be a whole number")
    if v < 1:
        raise SpecError(f"{what} must be at least 1")
    return v


def resolve_target(system, queue_name, cfg, repetitions, timeout_min, configs_on_queue, allocation):
    """One hardware configuration on one queue -> a campaign target."""
    where = f"{system['id']}/{queue_name or 'default queue'}"
    if not system.get("can_exec") or system.get("enabled") is False:
        raise SpecError(f"{system['id']} cannot run jobs")
    queues = system.get("queues") or []
    q = next((x for x in queues if x.get("name") == queue_name), None)
    if q is None and (queues or queue_name):
        raise SpecError(f"{system['id']} has no queue {queue_name!r}")
    q = q or {}
    if (_limit(q, "min_nodes") or 1) > 1:
        raise SpecError(f"{where} needs at least {q['min_nodes']} nodes; profiling jobs use 1 node")

    max_cores, max_mem, max_min = _limit(q, "max_cores"), _limit(q, "max_memory_mb"), _limit(q, "max_minutes")
    cores = _positive_int(cfg.get("cores"), f"{where}: cores") or max_cores or 1
    if max_cores and cores > max_cores:
        raise SpecError(f"{where} allows at most {max_cores} cores per node; asked for {cores}")
    if (_limit(q, "min_cores") or 1) > cores:
        raise SpecError(f"{where} needs at least {q['min_cores']} cores; asked for {cores}")
    memory = _positive_int(cfg.get("memory_mb"), f"{where}: memory") or _cap(max(MIN_MEMORY_MB, cores * MB_PER_CORE), max_mem)
    if max_mem and memory > max_mem:
        raise SpecError(f"{where} allows at most {max_mem} MB of memory; asked for {memory}")
    minutes = _positive_int(cfg.get("max_minutes"), f"{where}: max minutes") or \
        _cap(repetitions * timeout_min + STARTUP_MINUTES, max_min)
    if max_min and minutes > max_min:
        raise SpecError(f"{where} allows at most {max_min} minutes; asked for {minutes}")

    per_user = _limit(q, "max_jobs_per_user")
    concurrent = min(per_user, MAX_CONCURRENT) if per_user else DEFAULT_CONCURRENT
    return {"system_id": system["id"], "queue": queue_name or None, "node_count": 1,
            "cores_per_node": cores, "memory_mb": memory, "max_minutes": minutes,
            "container_args": "--nv" if is_gpu(q) else None,
            "scheduler_options": (allocation or "").strip() or None,
            # configurations on one queue share its per-user job limit
            "max_concurrent_jobs": max(1, concurrent // max(1, configs_on_queue))}


def _values(param, raw):
    vals = raw if isinstance(raw, list) else [raw]
    vals = [v.strip() if isinstance(v, str) else v for v in vals]
    vals = [v for v in vals if v not in (None, "")]
    if not vals:
        raise SpecError(f"give {param['name']!r} a value")
    out = []
    for v in vals:
        if param["kind"] in ("int", "float"):
            try:
                num = float(v)
            except (TypeError, ValueError):
                raise SpecError(f"{param['name']!r} needs numbers; got {v!r}")
            if param["kind"] == "int":
                if num != int(num):
                    raise SpecError(f"{param['name']!r} needs whole numbers; got {v!r}")
                num = int(num)
            out.append(num)
        else:
            out.append(str(v))
    return list(dict.fromkeys(out))   # drop repeats, keep order


def _slug(text, n=20):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:n] or "app"


def build_sweep(sweep, apps, systems, allocations, storage, stamp):
    """One sweep of the plan -> (campaign spec, resolved targets). Raises SpecError."""
    app = apps.get(sweep.get("app_id"))
    if app is None:
        raise SpecError(f"app {sweep.get('app_id')!r} is not one of your TAPIS apps")
    if not app["command"]:
        raise SpecError(f"{app['label']} does not declare its sweep command in its TAPIS notes")
    name = (sweep.get("name") or "").strip()
    if not NAME_RE.match(name):
        raise SpecError("sweep name: letters, digits, - _ . (max 31)")
    run_type = sweep.get("run_type") or "SD"
    if run_type not in RUN_TYPES:
        raise SpecError(f"run type must be one of {', '.join(RUN_TYPES)}")
    reps = _positive_int(sweep.get("repetitions"), "repetitions") or 1
    timeout = _positive_int(sweep.get("timeout_min"), "timeout per run") or 20

    given = sweep.get("params") or {}
    unknown = set(given) - {p["name"] for p in app["params"]}
    if unknown:
        raise SpecError(f"{app['label']} has no parameter {sorted(unknown)[0]!r}")
    params = {p["name"]: _values(p, given.get(p["name"], p["default"])) for p in app["params"]}

    picked = sweep.get("targets") or []
    if not picked:
        raise SpecError("tick at least one queue")
    per_queue = {}
    for t in picked:
        key = (t.get("system_id"), t.get("queue") or None)
        per_queue[key] = per_queue.get(key, 0) + 1
    targets, seen = [], set()
    for t in picked:
        sid = t.get("system_id")
        if sid not in systems:
            raise SpecError(f"{sid!r} is not one of your TAPIS execution systems")
        if isinstance(systems[sid], Exception):
            raise SpecError(f"could not read {sid} from TAPIS: {systems[sid]}")
        queue = t.get("queue") or None
        r = resolve_target(systems[sid], queue, t, reps, timeout, per_queue[(sid, queue)], allocations.get(sid))
        sig = (sid, queue, r["cores_per_node"], r["memory_mb"], r["max_minutes"])
        if sig in seen:
            raise SpecError(f"{sid}/{queue or 'default queue'} has two identical configurations")
        seen.add(sig)
        targets.append({**r, "app_id": app["id"], "app_version": app["version"]})

    slug = _slug(app["label"])
    spec = {"name": f"{slug}-{name}-{stamp}"[:64], "application": slug, "command": app["command"],
            "workdir": app["workdir"], "repetitions": reps, "run_timeout_sec": timeout * 60,
            "combos_per_job": 1, "distribution": "replicate",
            "run_sets": [{"run_type": run_type, "parameters": params}],
            "targets": targets, "storage": storage}
    return spec, targets


def build_plan(plan, gw):
    """Every sweep of the plan -> [{"name", "spec" | "error", "targets"}], plus a storage error.
    Systems and apps are read from TAPIS once per call."""
    storage = plan.get("storage") or {}
    storage = {"system_id": (storage.get("system_id") or "").strip(), "path": (storage.get("path") or "").strip()}
    storage_error = None
    if not storage["system_id"]:
        storage_error = "pick a TAPIS system for the results"
    elif not storage["path"].startswith("/"):
        storage_error = "the results folder must be an absolute path, e.g. /fs/scratch/PAS0000/harp_runs"

    sweeps = plan.get("sweeps") or []
    apps = {a["id"]: harp_app(a) for a in gw.list_apps()}
    systems = {}
    for s in sweeps:
        for t in s.get("targets") or []:
            sid = t.get("system_id")
            if sid and sid not in systems:
                try:
                    systems[sid] = gw.get_system(sid)
                except Exception as e:   # reported against the sweeps that use it
                    systems[sid] = e
    allocations = {k: v for k, v in (plan.get("allocations") or {}).items() if isinstance(v, str)}
    stamp = datetime.now(timezone.utc).strftime("%y%m%d%H%M")

    out, names = [], set()
    for s in sweeps:
        label = f"{s.get('app_id')} / {s.get('name')}"
        try:
            if (s.get("app_id"), s.get("name")) in names:
                raise SpecError(f"the plan has two sweeps called {s.get('name')!r} for this app")
            names.add((s.get("app_id"), s.get("name")))
            spec, targets = build_sweep(s, apps, systems, allocations, storage, stamp)
            out.append({"name": label, "spec": spec, "targets": targets})
        except SpecError as e:
            out.append({"name": label, "error": str(e), "targets": []})
    return out, storage_error
