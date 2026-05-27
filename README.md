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
POST /embed   { "texts": [...] }   → { "model": "...", "dim": 1024, "vectors": [[...], ...] }
GET  /docs                         → OpenAPI auto-docs (FastAPI default)
```

The `/embed` response is `{ "model": str, "dim": int, "vectors": list[list[float]] }`.
The request also accepts an optional `"normalize"` field (default `true`). Vectors are
float32 list-of-lists, length equal to the model's dimension (1024 for bge-m3, reported
in `dim`). Send batches of texts in one
request — the service does internal batching of `EMBEDDER_INTERNAL_BATCH`
(default 32 on CPU, 64–128 reasonable on GPU).

## Live URLs

### In-cluster (no auth needed beyond network reach)

| Cluster | URL | Notes |
|---|---|---|
| **prod** | `http://embedder.embedder.svc.cluster.local:8000` | 3 CPU replicas, fluid CPU sizing (low request + no limit, priorityClass `low-priority-burstable`), yields to tenant pods under contention. |
| **dev** | `http://embedder.helpbot.svc.cluster.local:8000` | Still in `helpbot` namespace pending consolidation. Same image. |
| **MSI GPU box** *(dev only today)* | `http://gpu-embedder.helpbot.svc.cluster.local:8001` | Reached via the `tailscale-egress` pod in `helpbot` ns. Faster path when MSI is on. |

### External (edge nginx, Amphora networks only — not the public internet)

| Cluster | URL | Notes |
|---|---|---|
| **prod** | `https://embedder.svc.amphora.ee` | Edge nginx → NodePort 30180. |
| **dev** | `https://embedder.dev.amphora.ee` | Edge nginx → NodePort 30181. |

These hostnames are public **DNS**, but the edge nginx IP-allowlists them to
Amphora's own networks only (`10.0.0.0/8` + office block `212.47.211.64/26`) —
**customer/internet IPs are denied**. Both also require the bearer token
(`EMBEDDER_PUBLIC=1` is set, so `EMBEDDER_API_KEY` is mandatory and `/docs` is
hidden). Edge limits: 50 MB body, 120 s timeouts. Prefer the in-cluster
`*.svc.cluster.local` URLs for internal consumers; use the external ones only
from off-cluster Amphora hosts.

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
vectors = r.json()["vectors"]  # list[list[float]]
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

Tag conventions:
- `:latest` — what every K8s deployment pulls today. Combined with
  `imagePullPolicy: Always` so a `kubectl rollout restart` picks up the
  newest CI build without needing a tag bump.
- Per-commit `:sha-<short>` tags are pushed too. Pin one in the manifest
  if you need a frozen version for a probe / experiment.

## Kubernetes deployment

The service is deployed by the cluster repo
[`AmphoraKubernetes`](https://github.com/amphora-infohaldus/AmphoraKubernetes)
under Flux GitOps. Two overlays exist today, sized differently because they
serve different purposes.

### Prod cluster (`workloads/embedder-prod/embedder.yaml`)

| Property | Value | Why |
|---|---|---|
| Namespace | `embedder` (dedicated) | Will be the model for every other cluster once we consolidate. |
| Replicas | 3, `RollingUpdate` `maxUnavailable=1 maxSurge=1` | Two pods stay reachable during a rollout; antiAffinity spreads them across nodes. |
| PriorityClass | `low-priority-burstable` (value `-100`) | **Fluid CPU model.** Tenant-facing pods (siga, xroad, dms) preempt the embedder when the cluster is hot. The embedder grabs whatever idle CPU is available the rest of the time. |
| CPU request | `100m` | Tiny so cfs_shares minimal — does not crowd out anything. |
| CPU limit | (none) | Lets a single embed batch burst onto whole cores when the cluster is idle. |
| Memory request | `3Gi` | Model is ~3 GB resident; can't compress, must reserve. |
| Memory limit | `5Gi` | Burst headroom for batch tensors. OOMKill if exceeded — there is no "fluid memory." |
| Anti-affinity | `preferredDuringSchedulingIgnoredDuringExecution` by hostname | Three pods land on three nodes when possible. |
| Image pull | `imagePullPolicy: Always` + `imagePullSecrets: [ghcr-pull]` | Always re-checks `:latest` digest on pod start. `ghcr-pull` is a cluster-wide GHCR PAT secret. |
| Model cache | `emptyDir{sizeLimit:5Gi}` mounted at `/models` | First start downloads ~2 GB from HuggingFace; subsequent restarts of the same pod reuse it. New pods (after eviction or scale-up) re-download — acceptable tradeoff for the simplicity of not needing an RWX PVC. |
| Health | `/health` startup + readiness + liveness probes | Model load takes 30–60 s on cold start; startup probe gives up to 5 min before liveness kicks in. |
| Service | `embedder.embedder.svc.cluster.local:8000`, NodePort 30180 | In-cluster DNS for internal consumers; NodePort is the edge-nginx target for `embedder.svc.amphora.ee` (IP-allowlisted, see Live URLs). |
| Flux Kustomization | `prod-embedder` in `flux/prod/workloads.yaml` (5-min reconcile, `prune: false`) | Edit YAML in git → Flux applies. |

### Dev cluster (`workloads/helpbot/embedder/`)

Same image, same probes, same antiAffinity. Differences:

| Property | Value | Why |
|---|---|---|
| Namespace | `helpbot` (legacy) | Will move to its own `embedder` namespace to match prod. |
| Replicas | 3 | Always-on CPU fallback for the helpbot drafting loop when the MSI GPU is offline. |
| PriorityClass | none | **No fluid sizing in dev** — unlike prod, the dev embedder runs at default priority with a hard CPU limit (below), so it is not preemptible by tenant pods. |
| CPU | `500m` req, `2` limit | Bounded sizing — dev cluster has less headroom and we don't want the embedder to crowd dev tenants. Contrast prod's `100m` req / no limit. |
| Memory | `1Gi` req, `4Gi` limit | Smaller than prod (`3Gi`/`5Gi`). Tighter request fits the dev cluster's headroom. |
| `HF_TOKEN` | Optional, from `helpbot-secrets` secret key `HF_TOKEN` | Needed only if you switch to a gated model. |

### Image build & roll

CI lives in `AmphoraKubernetes/.github/workflows/build-images.yml` today
(will move to this repo). It rebuilds `ghcr.io/amphora-infohaldus/helpbot-embedder:latest`
on every push that touches embedder paths. To pull a new build into the
cluster manually:

```bash
# prod
KUBECONFIG=clusters/prod/kubeconfig.yaml \
  kubectl -n embedder rollout restart deploy/embedder
KUBECONFIG=clusters/prod/kubeconfig.yaml \
  kubectl -n embedder rollout status  deploy/embedder --timeout=10m

# dev
KUBECONFIG=clusters/dev/kubeconfig.yaml \
  kubectl -n helpbot rollout restart deploy/embedder
```

The 10 min timeout is intentional — cold pods need ≥1 min for the model
download + load before they pass startup.

### Deploying to a new cluster

For DR or sandbox K:
1. Copy `workloads/embedder-prod/` (namespace, PriorityClass, Deployment, Service).
2. Create the `ghcr-pull` imagePullSecret in the target namespace (clone from another namespace, or recreate from `~/.docker/config.json`).
3. Add a Flux Kustomization in `flux/<cluster>/workloads.yaml` pointing at the new path with `prune: false` and `interval: 5m`.
4. Push the change; Flux reconciles.

There is **no embedder on the DR cluster today** — gate that on whether the helpbot/customer-journey probes actually need synthesis during a DR event.

## GPU boxes (manual deploy, reached over Tailscale)

A GPU endpoint is a manual deployment of this same service on a workstation
— **not** in K8s. There are two today: Ingmar's MSI box and Janis's RTX 4070
box; each is one endpoint in the dev-box embedder pool. Consumers reach them
in-cluster via the `tailscale-egress` pod (e.g.
`gpu-embedder.helpbot.svc.cluster.local:8001`). Expected speedup over CPU
bge-m3: 50×–500×.

**Full cold-start (prereqs → clone → venv → launch → autostart → report) for
any GPU box, including onboarding an additional box like Janis's, is in
[`docs/gpu-box-runbook.md`](docs/gpu-box-runbook.md).** The summary below is
the quick run+autostart reference once the box is set up.

Run it (PowerShell, in a clone of this repo, inside the venv):

```powershell
$env:EMBEDDER_DEVICE = "cuda"
$env:EMBEDDER_DTYPE = "bf16"           # smallest drift vs CPU fp32 pool members
$env:EMBEDDER_INTERNAL_BATCH = "128"   # GPU saturates well above the CPU default of 32
$env:EMBEDDER_API_KEY = "<token>"      # required; consumers send Authorization: Bearer <token>
$env:MODEL_NAME = "BAAI/bge-m3"
$env:HF_HOME = "$HOME\hf-models"
uvicorn app:app --host 0.0.0.0 --port 8001
```

CUDA torch wheel install and prereqs: see `Dockerfile.cuda` (cu128 index,
`torch==2.11.0`).

**Autostart (required — this is the fix for the silent outages).** A
hand-started `uvicorn` is a foreground process: a reboot, sleep, or crash
kills it and consumers silently fall back to the slow CPU pool until
someone notices. Wrap it so it survives:

- Install as a Windows service with [NSSM](https://nssm.cc/): point it at
  the venv's `uvicorn` with the env vars above set as service environment,
  set startup to **Automatic**, and enable restart-on-failure.
- Or register a Scheduled Task triggered **At log on** / **At startup**
  running the same command. NSSM is preferred — it restarts on crash, a
  Task does not.

**Firewall.** Tailscale traffic doesn't bypass Windows Firewall. Allow
inbound TCP 8001 on the Tailscale (Public) profile:

```powershell
New-NetFirewallRule -DisplayName "Embedder 8001 (Tailscale)" `
  -Direction Inbound -LocalPort 8001 -Protocol TCP -Action Allow -Profile Public
```

**Do not** expose 8001 on any public interface — Tailscale is the only
intended path, and `EMBEDDER_API_KEY` is the second gate.

## Topology (current → planned)

Today:
- 3 CPU pods in prod K (`embedder` ns, fluid sizing).
- 3 CPU pods in dev K (`helpbot` ns; will move to its own `embedder` ns to match prod).
- 1 GPU pod on the MSI workstation reached over Tailscale (dev-only fallback / fast-path).

Planned:
- HA across prod + DR clusters (DR currently has none).
- Routing layer that picks CPU vs GPU vs cloud-burst based on availability and pending queue length.
- Cloud-burst when MSI is offline AND load is high (Runpod / Modal / similar).

## Operations

| Need | How |
|---|---|
| Check rollout status | `kubectl -n embedder rollout status deploy/embedder` (prod), `kubectl -n helpbot rollout status deploy/embedder` (dev). |
| Tail logs from all replicas | `kubectl -n embedder logs -l app=embedder -f --max-log-requests 5`. |
| Confirm a pod loaded the right model | `kubectl -n embedder exec deploy/embedder -- curl -s localhost:8000/health` → check `model`, `device`, `dtype`. |
| Scale replicas | `kubectl -n embedder scale deploy/embedder --replicas=N`. There is no HPA — request volume is too low and bursty to be worth the tuning. |
| Restart one pod (e.g., to reload model cache) | `kubectl -n embedder delete pod <name>`. Anti-affinity + RollingUpdate keep the service available. |
| Force a fresh image pull | `kubectl -n embedder rollout restart deploy/embedder` — `imagePullPolicy: Always` resolves `:latest` on each new pod. |
| Watch a benchmark | `kubectl -n embedder run bench --rm -it --image=curlimages/curl -- sh -c 'time curl -sS -XPOST http://embedder.embedder.svc.cluster.local:8000/embed -H "Content-Type: application/json" -d "{\"texts\": [\"hello\"]}"'`. CPU pod baseline: ~80–150 ms per single-text request after warmup. |

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
| `EMBEDDER_PUBLIC` | unset | Set `1` when behind a public ingress. Refuses to start without `EMBEDDER_API_KEY` and hides `/docs`, `/redoc`, and the OpenAPI schema. |

## See also

- Source consumers: `amphora-help-bot/services/{gmail_watcher,webapp,orchestrator}`
- K8s sizing rules: `AmphoraKubernetes/docs/resource-planning.md` § "JVM-on-K8s sizing recipe" (the embedder is not a JVM workload, but the resource-planning rules apply).
