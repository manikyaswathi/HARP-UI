def euler_spec(**over):
    spec = {
        "name": "euler-sweep",
        "application": "euler",
        "command": "python3 calc_e.py {method} {n} {precision}",
        "workdir": "/app/01-eulers_number",
        "repetitions": 2,
        "combos_per_job": 2,
        "run_sets": [
            {"run_type": "SD", "parameters": {"method": ["pow", "factorial"], "n": [10, 100], "precision": [64]}},
            {"run_type": "FS", "parameters": {"method": ["pow"], "n": [1000, 2000], "precision": [64]}},
            {"run_type": "test_data", "parameters": {"method": ["factorial"], "n": [500], "precision": [64]}},
        ],
        "targets": [
            {"system_id": "pitzer", "app_id": "harp-sweep-euler", "queue": "serial",
             "scheduler_options": "-A PAS0000", "max_concurrent_jobs": 2},
            {"system_id": "stampede", "app_id": "harp-sweep-euler", "max_concurrent_jobs": 1},
        ],
        "storage": {"system_id": "storage", "path": "/scratch/harp_runs/"},
    }
    spec.update(over)
    return spec
