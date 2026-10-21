# ComfyUI Serverless — RunPod Template

Run any ComfyUI workflow on RunPod serverless. Send a workflow JSON, get back output URLs. Workers auto-scale, auto-install missing custom nodes, and shut down when idle.

## What's Included

- **Shared father image** — the exact `hearmeman/comfyui-base` release in `pins.json`
- **Inherited GPU stack** — CUDA 13.0, Python 3.12, Torch 2.11, ComfyUI 0.32, SageAttention, and ComfyUI-Manager
- **Serverless custom nodes baked in** — WanVideoWrapper, KJNodes, VideoHelperSuite, essentials, controlnet_aux, RES4LYF, and the rest of `nodes.lock`
- **Auto custom node installer** — missing nodes are detected, installed, and loaded automatically
- **Metadata stripping** — output files have embedded workflow data removed

## Father Image

This image does not maintain its own CUDA, Python, Torch, ComfyUI, or
SageAttention installation. Those are owned by
`Hearmeman24/comfyui-runtime/base/Dockerfile`, exactly like the pod templates.
The serverless repository owns only its custom nodes and RunPod startup layer.

To upgrade the whole ComfyUI family:

1. Change and validate the father Dockerfile and its Torch/Sage pins.
2. Publish the new immutable `hearmeman/comfyui-base` tag.
3. Advance each child's `base_image` pointer and rebuild it.

The shared versions change only in the father. Child Dockerfiles never repeat
or independently select those versions.

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `AWS_ACCESS_KEY_ID` | Yes | S3 access key |
| `AWS_SECRET_ACCESS_KEY` | Yes | S3 secret key |
| `S3_BUCKET` | Yes | S3 bucket name |
| `S3_REGION` | No | S3 region (default: `eu-west-2`) |
| `LOG_RECEIVER_URL` | No | URL for real-time log streaming |
| `LOG_RECEIVER_TOKEN` | No | Auth token for log receiver |

## Network Volume

Mount at `/runpod-volume` with your models:

```
/runpod-volume/ComfyUI/models/
├── checkpoints/    # SD, SDXL, Wan, Flux, etc.
├── loras/
├── vae/
├── clip/
├── diffusion_models/
├── text_encoders/
└── ...
```

## Example Input

```json
{
  "input": {
    "workflow": {
      "7": {
        "inputs": {"seed": 42, "steps": 20, "model": ["10", 0]},
        "class_type": "KSampler"
      },
      "10": {
        "inputs": {"ckpt_name": "model.safetensors"},
        "class_type": "CheckpointLoaderSimple"
      }
    },
    "file_inputs": {
      "15": {
        "field": "image",
        "url": "https://bucket.s3.amazonaws.com/photo.png",
        "filename": "photo.png"
      }
    },
    "overrides": {
      "7": {"seed": 12345, "denoise": 0.8}
    }
  }
}
```

- `workflow` — ComfyUI API-format JSON (export via Save API Format)
- `file_inputs` — files for the worker to download before execution
- `overrides` — parameter overrides merged into node inputs

## Example Output

```json
{
  "ok": true,
  "output": {
    "url": "https://bucket.s3.amazonaws.com/comfy-gen/outputs/abc123.png",
    "seed": 42,
    "resolution": {"width": 1024, "height": 1024},
    "model_hashes": {
      "model.safetensors": {"sha256": "a1b2c3...", "type": "checkpoints"}
    }
  }
}
```

## Progress Updates

Workers send structured progress via the RunPod status API:

```json
{"stage": "inference", "percent": 55, "message": "KSampler Step 4/8", "completed_nodes": 5, "total_nodes": 12}
```

Stages: `init` → `download_inputs` → `node_check` → `queue` → `inference` → `collecting` → `upload`

## ComfyGen CLI

[ComfyGen](https://github.com/Hearmeman24/ComfyGen.git) is the recommended client. It handles S3 uploads, polling, and progress display.

```bash
pip install boto3
comfy-gen config --set runpod_api_key=rpa_...
comfy-gen config --set endpoint_id=<your-endpoint-id>
comfy-gen config --set s3_bucket=my-bucket
comfy-gen submit workflow.json
```

## Cold Start

~12s with baked nodes. FlashBoot-enabled endpoints start near-instantly.

## License

MIT
