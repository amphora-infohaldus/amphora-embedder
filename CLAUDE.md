<!-- audience: internal -->
# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single-file FastAPI service (`app.py`) that wraps a sentence-transformers model
(default `BAAI/bge-m3`, 1024-dim, multilingual) behind a `/embed` endpoint. It is the
**shared embedding service** for the org — consumed by helpbot, semantic search, and
customer-journey probes. Do not fork the service code into consumer repos; change it here.

## Commands

```bash
# Run locally (downloads the model on first start; ~2GB)
pip install -r requirements.txt
pip install torch==2.11.0 --index-url https://download.pytorch.org/whl/cpu
uvicorn app:app --host 0.0.0.0 --port 8000

# Build images
docker build -t amphora-embedder .                          # CPU
docker build -t amphora-embedder-cuda -f Dockerfile.cuda .  # CUDA 12.x

# Smoke test a running instance
curl -sS http://localhost:8000/health
curl -sS -X POST http://localhost:8000/embed \
  -H "Content-Type: application/json" \
  -d '{"texts": ["hello world", "tere maailm"]}'
```

There is no test suite, linter config, or CI in this repo yet. Images are currently built
by the external `AmphoraKubernetes/.github/workflows/build-images.yml` pipeline as
`ghcr.io/amphora-infohaldus/helpbot-embedder:latest` (per-repo CI is planned).

## Architecture notes

- The model is loaded **once at module import** (`app.py:43`), not per-request. `model.encode`
  does its own internal batching (`EMBEDDER_INTERNAL_BATCH`, default 32); callers should send
  many texts per request rather than many requests.
- **Cross-endpoint numerical consistency is a real constraint.** The same model runs on CPU
  (fp32) and GPU (fp16/bf16) pods serving one logical pool. Vectors are always cast to float32
  before serialization (`app.py:101`) so JSON output is stable regardless of compute dtype.
  Prefer `bf16` over `fp16` on GPU — it has fp32's exponent range, so vectors drift least from
  the CPU fp32 baseline. The two Dockerfiles pin the identical `torch==2.11.0` for the same reason.
- **Auth is intentionally asymmetric** (`app.py:65`): when `EMBEDDER_API_KEY` is set, only
  `/embed` requires the bearer token; `/health` stays open so K8s/load-balancer probes work
  without the key. Tailscale ACLs are the real perimeter for private deploys.

## Conventions

- This repo follows the org docs rules in `C:\Sources\.github\docs-discipline.md` and
  `docs-audience.md`. Every markdown file needs an audience marker; check the pre-commit
  checklist before committing. Live cluster URLs and topology live in `README.md` (marked
  `internal`) — keep deployment facts there, not duplicated here.
