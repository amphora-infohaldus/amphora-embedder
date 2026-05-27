"""Embedder service — wraps BAAI/bge-m3 in a FastAPI endpoint.

Runs on CPU by default; on the GPU workstation, set EMBEDDER_DEVICE=cuda
in the environment (see services/embedder/Dockerfile.cuda).

Optional bearer-token auth: set EMBEDDER_API_KEY; requests without a
matching Authorization header are rejected with HTTP 401.

Tuneables:
  EMBEDDER_DTYPE          fp32 (default) | fp16 | bf16
                          Lower precision => ~2x throughput + half VRAM on GPU.
                          BF16 has the same exponent range as FP32 (smallest
                          vector drift vs CPU FP32 baseline). Use BF16 when the
                          pool also includes FP32 endpoints.
  EMBEDDER_INTERNAL_BATCH model.encode batch_size (default 32). On FP16/BF16
                          GPU, 64-128 typically maxes throughput.
"""
from __future__ import annotations

import os
from typing import Sequence

import torch
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sentence_transformers import SentenceTransformer

MODEL_NAME = os.getenv("MODEL_NAME", "BAAI/bge-m3")
DEVICE = os.getenv("EMBEDDER_DEVICE", "cpu")
API_KEY = os.getenv("EMBEDDER_API_KEY", "").strip()
# Set EMBEDDER_PUBLIC=1 when the service is reachable from the public
# internet (ingress on *.amphora.ee). It hardens two things: refuses to
# start without EMBEDDER_API_KEY, and hides the interactive docs + OpenAPI
# schema so the API surface isn't advertised.
PUBLIC = os.getenv("EMBEDDER_PUBLIC", "").strip().lower() in {"1", "true", "yes"}

_DTYPE_MAP = {
    "fp32": torch.float32, "float32": torch.float32,
    "fp16": torch.float16, "float16": torch.float16, "half": torch.float16,
    "bf16": torch.bfloat16, "bfloat16": torch.bfloat16,
}
_DTYPE_NAME = os.getenv("EMBEDDER_DTYPE", "fp32").strip().lower()
DTYPE = _DTYPE_MAP.get(_DTYPE_NAME, torch.float32)
INTERNAL_BATCH = int(os.getenv("EMBEDDER_INTERNAL_BATCH", "32"))

if PUBLIC and not API_KEY:
    raise RuntimeError(
        "EMBEDDER_PUBLIC is set but EMBEDDER_API_KEY is empty — refusing to "
        "start a publicly-exposed embedder without bearer-token auth."
    )

# Hide /docs, /redoc, and the OpenAPI schema when publicly exposed.
_docs_kwargs = (
    dict(docs_url=None, redoc_url=None, openapi_url=None) if PUBLIC else {}
)
app = FastAPI(title="amphora-help-bot embedder", **_docs_kwargs)
model = SentenceTransformer(MODEL_NAME, device=DEVICE)
if DTYPE is torch.float16:
    model = model.half()
elif DTYPE is torch.bfloat16:
    model = model.bfloat16()
_actual_dtype = str(next(model.parameters()).dtype)
_torch_threads = torch.get_num_threads()
_cuda_available = torch.cuda.is_available()
_cuda_device_name = torch.cuda.get_device_name(0) if _cuda_available else None


class EmbedRequest(BaseModel):
    texts: Sequence[str]
    normalize: bool = True


class EmbedResponse(BaseModel):
    model: str
    dim: int
    vectors: list[list[float]]


@app.middleware("http")
async def _auth_mw(request: Request, call_next):
    # When EMBEDDER_API_KEY is set, require it on /embed.
    # /health stays open so the client's pool can probe without knowing
    # the key (Tailscale ACLs are the real front-door for private deploys).
    if API_KEY and request.url.path.startswith("/embed"):
        if request.headers.get("authorization", "") != f"Bearer {API_KEY}":
            return JSONResponse(status_code=401, content={"detail": "invalid bearer token"})
    return await call_next(request)


@app.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "model": MODEL_NAME,
        "device": DEVICE,
        "dtype": _actual_dtype,
        "internal_batch": INTERNAL_BATCH,
        "cuda_available": _cuda_available,
        "cuda_device_name": _cuda_device_name,
        "torch_threads": _torch_threads,
        "auth_required": bool(API_KEY),
        "public": PUBLIC,
    }


@app.post("/embed", response_model=EmbedResponse)
def embed(req: EmbedRequest) -> EmbedResponse:
    vectors = model.encode(
        list(req.texts),
        normalize_embeddings=req.normalize,
        batch_size=INTERNAL_BATCH,
        show_progress_bar=False,
    )
    # In bf16/fp16 mode .encode returns lower-precision tensors; cast to float32
    # before tolist() so JSON gets stable Python floats.
    if vectors.dtype != "float32":
        vectors = vectors.astype("float32")
    return EmbedResponse(
        model=MODEL_NAME,
        dim=int(vectors.shape[1]),
        vectors=vectors.tolist(),
    )
