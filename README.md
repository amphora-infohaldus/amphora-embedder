<!-- audience: internal -->
# amphora-embedder

Text-embedding service — wraps a sentence-transformers model (default
`BAAI/bge-m3`) in a small FastAPI endpoint. Run on CPU as a K8s
Deployment, or with `EMBEDDER_DEVICE=cuda` on a GPU box.

This repo is **the shared service** consumed by helpbot, semantic
search, and the customer-journey synthetic probes. Don't fork the
service code into other repos — submit a PR here.

## API

```
GET  /health                       → 200 if model is loaded
POST /embed   { "texts": [...] }   → { "embeddings": [[...], ...] }
GET  /docs                         → OpenAPI auto-docs (FastAPI default)
```

Single endpoint, vectors are float32 list-of-lists, length equal to the
model's dimension (1024 for bge-m3). Send batches of texts in one
request — the service does internal batching of `EMBEDDER_INTERNAL_BATCH`
(default 32 on CPU, 64–128 reasonable on GPU).

## Live URLs (call from inside the cluster)

| Cluster | URL | Notes |
|---|---|---|
| **prod** | `http://embedder.embedder.svc.cluster.local:8000` | 3 CPU replicas, fluid CPU sizing (low request + no limit, priorityClass `low-priority-burstable`), yields to tenant pods under contention. |
| **dev** | `http://embedder.helpbot.svc.cluster.local:8000` | Still in `helpbot` namespace pending consolidation. Same image. |
| **MSI GPU box** *(dev only today)* | `http://gpu-embedder.helpbot.svc.cluster.local:8001` | Reached via the `tailscale-egress` pod in `helpbot` ns. Faster path when MSI is on. |

K8s manifests live in
[`AmphoraKubernetes/workloads/embedder-prod/`](https://github.com/amphora-infohaldus/AmphoraKubernetes/tree/main/workloads/embedder-prod)
(prod) and
[`AmphoraKubernetes/workloads/helpbot/embedder/`](https://github.com/amphora-infohaldus/AmphoraKubernetes/tree/main/workloads/helpbot/embedder)
(dev).

## How to call (examples)

From a pod in any namespace that can reach the cluster DNS:

```bash
curl -sS -X POST http://embedder.embedder.svc.cluster.local:8000/embed \
  -H "Content-Type: application/json" \
  -d '{"texts": ["hello world", "tere maailm"]}'
```

Python:

```python
import requests
r = requests.post(
    "http://embedder.embedder.svc.cluster.local:8000/embed",
    json={"texts": ["hello world", "tere maailm"]},
)
embeddings = r.json()["embeddings"]  # list[list[float]]
```

If `EMBEDDER_API_KEY` is set on the server, include
`Authorization: Bearer <token>` in requests.

## Image

CI in this repo (TBD — currently the K8s build pipeline at
`AmphoraKubernetes/.github/workflows/build-images.yml` builds the CPU
image as `ghcr.io/amphora-infohaldus/helpbot-embedder:latest`. Will
migrate to per-repo CI publishing `ghcr.io/amphora-infohaldus/amphora-embedder:{cpu,cuda}` once this repo settles).

Build locally:

```
docker build -t amphora-embedder .                       # CPU
docker build -t amphora-embedder-cuda -f Dockerfile.cuda .   # CUDA 12.x
```

## Topology (current → planned)

Today:
- 3 CPU pods in prod K (fluid sizing).
- 3 CPU pods in dev K (`helpbot` ns; will move to its own `embedder` ns to match prod).
- 1 GPU pod on the MSI workstation reached over Tailscale (dev-only fallback / fast-path).

Planned:
- HA across prod + DR clusters (DR currently has none).
- Routing layer that picks CPU vs GPU vs cloud-burst based on availability and pending queue length.
- Cloud-burst when MSI is offline AND load is high (Runpod / Modal / similar).

## Tunables (env)

| Variable | Default | Notes |
|---|---|---|
| `MODEL_NAME` | `BAAI/bge-m3` | Any sentence-transformers compatible model. Multilingual, 1024-dim. |
| `EMBEDDER_DEVICE` | `cpu` | Set `cuda` on GPU image. |
| `EMBEDDER_DTYPE` | `fp32` | Use `bf16` on GPU when pool also has FP32 endpoints (smallest drift). |
| `EMBEDDER_INTERNAL_BATCH` | `32` | Increase on GPU (64–128 typical max). |
| `HF_HOME` | unset | Mount a volume here to persist the model download across pod restarts. |
| `HF_TOKEN` | unset | Required only for gated models. |
| `EMBEDDER_API_KEY` | unset | If set, requests must include `Authorization: Bearer <token>`. |

## See also

- Source consumers: `amphora-help-bot/services/{gmail_watcher,webapp,orchestrator}`
- K8s sizing rules: `AmphoraKubernetes/docs/resource-planning.md` § "JVM-on-K8s sizing recipe" (the embedder is not a JVM workload, but the resource-planning rules apply).
