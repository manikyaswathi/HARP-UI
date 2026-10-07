# 02-yolo — Ultralytics YOLO training, profiled by HARP

Target application: train an [Ultralytics YOLO](https://github.com/ultralytics/ultralytics) model once.
HARP sweeps the training hyperparameters across CPU and GPU systems and learns how long training takes.

| File | Purpose |
|---|---|
| `train_yolo.py` | Trains one model: `--model --epochs --imgsz --batch --device` |
| `coco8.yaml` | The dataset: [coco8](https://docs.ultralytics.com/datasets/detect/coco8/) (4 train + 4 val images, 80 COCO classes) |
| `../../DockerFiles/Dockerfile_App_YOLO_Sweep` | The container: Ultralytics `v8.4.174` from GitHub, PyTorch 2.7.1 (CUDA 12.6), coco8, and yolo11n/s/m weights |

Everything the job needs is inside the image, so it runs on compute nodes **without internet**.

## Sweep parameters (HARP UI command template)

```
python3 train_yolo.py --model {model} --epochs {epochs} --imgsz {imgsz} --batch {batch} --device {device}
```
Work folder in container: `/app/02-yolo`

| Parameter | Values | Notes |
|---|---|---|
| `model` | `yolo11n`, `yolo11s`, `yolo11m` | Baked-in weights (nano / small / medium) |
| `epochs` | e.g. `1, 2, 5` | Largest driver of training time |
| `imgsz` | e.g. `320, 480, 640` | Training image size |
| `batch` | e.g. `4, 8, 16` | Batch size |
| `device` | `auto`, `cpu`, `0` | `auto` uses the GPU when the job has one, else CPU |

The **Load YOLO example** button in the HARP UI fills all of this in.

## CPU and GPU

* **CPU queues** (e.g. Pitzer `serial`): nothing special. Give the job a few cores (`cores per node` 4+).
* **GPU queues** (e.g. Pitzer `gpuserial-40core`): set **Container args** to `--nv` for that system in the
  HARP UI. Singularity then exposes the node's GPUs and `device=auto` trains on the GPU.
  Without `--nv`, `auto` silently falls back to CPU and `device=0` fails with a clear message.
* The profiling CSV records the hardware for every run: CPU cores, memory, and `sys_gpu_count`,
  `sys_gpu_name`, `sys_gpu_mem_mb`.

## Build

The GitHub workflow `.github/workflows/build-sweep-image.yml` builds, tests (one training run with no network)
and pushes `ghcr.io/<owner>/harp-sweep-yolo:1.0.0`. To build it yourself (on an Apple-silicon Mac add
`--platform linux/amd64`):
```bash
docker build -f DockerFiles/Dockerfile_App_YOLO_Sweep -t <registry>/harp-sweep-yolo:1.0.0 .
```

Register the TAPIS app with `Notebooks/Create_HARP_Sweep_App_TAPIS.ipynb` using `APP = "yolo"`.

## Run it locally

```bash
docker run --rm --entrypoint python3 ghcr.io/<owner>/harp-sweep-yolo:1.0.0 \
  /app/02-yolo/train_yolo.py --model yolo11n --epochs 1 --imgsz 320 --batch 4 --device cpu
```

Ultralytics is licensed under AGPL-3.0; it is installed into the image from its repository, not copied into this one.
