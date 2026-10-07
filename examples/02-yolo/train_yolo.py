#!/usr/bin/env python3
"""
Train an Ultralytics YOLO model once - the target application HARP profiles.

Everything it needs is baked into the container, so it runs on compute
nodes without internet:
  weights:  /app/02-yolo/weights/yolo11{n,s,m}.pt
  dataset:  /app/02-yolo/datasets/coco8   (described by coco8.yaml)

Example:
  python3 train_yolo.py --model yolo11n --epochs 3 --imgsz 640 --batch 8 --device auto
"""

import argparse
import json
import os
import time

APP_DIR = os.path.dirname(os.path.abspath(__file__))
WEIGHTS_DIR = os.environ.get("YOLO_WEIGHTS_DIR", os.path.join(APP_DIR, "weights"))
DEFAULT_DATA = os.environ.get("YOLO_DATA", os.path.join(APP_DIR, "coco8.yaml"))


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--model", default="yolo11n", help="yolo11n | yolo11s | yolo11m (or a path to a .pt file)")
    p.add_argument("--epochs", type=int, default=1)
    p.add_argument("--imgsz", type=int, default=640)
    p.add_argument("--batch", type=int, default=8)
    p.add_argument("--device", default="auto", help="auto (GPU if available, else CPU) | cpu | 0 | 0,1")
    p.add_argument("--workers", type=int, default=2)
    p.add_argument("--data", default=DEFAULT_DATA, help="dataset yaml")
    return p.parse_args()


def resolve_device(device):
    import torch
    if device == "auto":
        return "0" if torch.cuda.is_available() else "cpu"
    if device != "cpu" and not torch.cuda.is_available():
        raise SystemExit(f"device={device} requested but no GPU is visible (Singularity needs --nv)")
    return device


def main():
    args = parse_args()
    from ultralytics import YOLO
    import torch

    weights = args.model if args.model.endswith(".pt") else os.path.join(WEIGHTS_DIR, f"{args.model}.pt")
    if not os.path.isfile(weights):
        raise SystemExit(f"weights not found: {weights}")
    device = resolve_device(args.device)
    gpu = torch.cuda.get_device_name(0) if device != "cpu" else "none"
    print(f"[YOLO] model={args.model} epochs={args.epochs} imgsz={args.imgsz} batch={args.batch} "
          f"device={device} gpu={gpu} torch={torch.__version__}", flush=True)

    start = time.perf_counter()
    model = YOLO(weights)
    results = model.train(
        data=args.data, epochs=args.epochs, imgsz=args.imgsz, batch=args.batch, device=device,
        workers=args.workers, project=os.path.join(os.getcwd(), "runs"), name="train", exist_ok=True,
        plots=False,          # no font downloads, no plotting overhead in the timing
        amp=device != "cpu",  # the AMP check uses the baked-in yolo11n.pt (see Dockerfile)
        verbose=False,
    )
    elapsed = time.perf_counter() - start

    metrics = {k: round(float(v), 5) for k, v in (getattr(results, "results_dict", None) or {}).items()}
    print("[YOLO] train_seconds=%.3f metrics=%s" % (elapsed, json.dumps(metrics)), flush=True)


if __name__ == "__main__":
    main()
