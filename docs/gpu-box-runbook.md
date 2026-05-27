<!-- audience: internal -->
# GPU box runbook — cold start for a Claude session on the box

> **Read me first.** This is the canonical, self-contained runbook for standing
> up the bge-m3 embedder on a **GPU workstation** (Ingmar's MSI box, Janis's RTX
> 4070 box, or any future GPU host) so the dev-box ingestion pool can fan
> embedding work out to it over Tailscale. The embedder service was extracted
> from `amphora-help-bot` into this repo (`amphora-embedder`); the app is
> identical, only the repo and paths changed.
>
> Run this on the GPU box itself (a fresh Claude Code session there is the
> intended operator). When done, report the **Tailscale IP + bearer token** so
> Ingmar can wire the endpoint into the dev-box pool.

## How the box fits the pool

- Each GPU box is **one embedder endpoint** added to the dev-box `EmbedderPool`
  (lives in `amphora-help-bot/services/ingestion/_lib.py`). The pool round-robins
  batches across every URL in `EMBEDDER_URLS`, with a circuit breaker that marks
  unreachable endpoints unhealthy for a cooldown window.
- **GPU boxes are expected to be offline at random** (sleep, reboot, owner takes
  the laptop home). Nothing breaks when a box is down — the K cluster's always-on
  3× CPU embedders are the fallback; throughput just drops until the box wakes.
  No SLA, no alerts, no panic.
- On boot the pool probes every endpoint's `/health` and **refuses the whole run
  if the `model` field disagrees**. So every box MUST serve the same model
  (`BAAI/bge-m3`). Do not change the model.
- Expected speedup over CPU bge-m3: **50×–500×** depending on the card.

## Step 0 — Prerequisites (verify, don't assume)

In PowerShell, run each; fix anything that fails before continuing:

```powershell
nvidia-smi          # NVIDIA driver present? Shows GPU + CUDA version. Need driver >= 535.
py -0               # Python launcher + versions. Need 3.12.
git --version       # Need git.
tailscale status    # If "not found" -> install from https://tailscale.com/download/windows
```

Fix order: NVIDIA driver (reboot may be required) → Python 3.12
(`winget install Python.Python.3.12`) → Git (`winget install Git.Git`) →
Tailscale (MSI installer; sign in with **Ingmar's tailnet**, the same one the dev
box uses).

## Step 1 — Clone this repo

```powershell
mkdir C:\Sources -Force
cd C:\Sources
git clone https://github.com/amphora-infohaldus/amphora-embedder.git
cd amphora-embedder
git log --oneline -3    # sanity check
```

`app.py` and `requirements.txt` are at the **repo root** (not `services/embedder/`
as in the old help-bot layout).

## Step 2 — Python venv + CUDA torch

```powershell
cd C:\Sources\amphora-embedder
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1

# CUDA torch wheel. Keep torch==2.11.0 (CPU/GPU lockstep) from the cu128 index —
# 2.11.0 is no longer on cu124. Verified 2026-04 on driver 576 / CUDA 12.9, RTX
# 4070 Laptop. Fallback if cu128 errors: cu128 -> cu126 -> cu124 -> cu121.
pip install --index-url https://download.pytorch.org/whl/cu128 torch==2.11.0
pip install -r requirements.txt
```

Verify the GPU is visible:

```powershell
python -c "import torch; print('cuda:', torch.cuda.is_available(), torch.cuda.get_device_name(0) if torch.cuda.is_available() else '')"
```

Expect `cuda: True <card>`. If `False`, the wheel didn't match the driver — fall
back one CUDA major at a time.

## Step 3 — Bearer token

- **First GPU box (MSI):** generate one.
  ```powershell
  python -c "import secrets; print(secrets.token_urlsafe(32))"
  ```
- **Additional box (Janis / future):** **do not generate a new one.** The dev-box
  pool uses a single shared `EMBEDDER_API_KEY` across all endpoints. Ask Ingmar
  for the existing value and reuse it verbatim.

## Step 4 — Launch the embedder

```powershell
$env:EMBEDDER_DEVICE = "cuda"
$env:EMBEDDER_DTYPE = "bf16"           # same exponent range as fp32 -> smallest drift vs the CPU pool members
$env:EMBEDDER_INTERNAL_BATCH = "128"   # GPU saturates well above the CPU default of 32
$env:EMBEDDER_API_KEY = "<token from step 3>"
$env:MODEL_NAME = "BAAI/bge-m3"
$env:HF_HOME = "$HOME\hf-models"
$env:HF_HUB_DISABLE_SYMLINKS_WARNING = "1"   # silence the Windows symlink warning on each download

python -m uvicorn app:app --host 0.0.0.0 --port 8001
```

First run downloads bge-m3 (~2.3 GB) into `$HOME\hf-models`. Wait for
`Uvicorn running on http://0.0.0.0:8001`.

## Step 5 — Local verification

In a second PowerShell (leave uvicorn running):

```powershell
curl.exe http://localhost:8001/health
```

Expect `"ok": true`, `"device": "cuda"`, `"cuda_available": true`, the card name,
and `"model": "BAAI/bge-m3"`. Then a real embed (replace TOKEN):

```powershell
curl.exe -H "Authorization: Bearer TOKEN" -H "Content-Type: application/json" `
  -d '{\"texts\":[\"tere maailm\"],\"normalize\":true}' http://localhost:8001/embed
```

Expect `{"model":"BAAI/bge-m3","dim":1024,"vectors":[[...]]}` in well under a
second. 401 → token mismatch.

## Step 6 — Firewall

Tailscale traffic doesn't bypass Windows Firewall. In an **elevated** PowerShell,
allow inbound TCP 8001 (one line — line breaks corrupt the parser on paste):

```powershell
New-NetFirewallRule -DisplayName "Embedder 8001 (Tailscale)" -Direction Inbound -LocalPort 8001 -Protocol TCP -Action Allow -Profile Public
```

## Step 7 — Tailscale IP

```powershell
tailscale ip -4
```

Note the `100.x.y.z`. (Additional box: accept Ingmar's Tailscale invite first —
he sends it from https://login.tailscale.com/admin/users/invite.)

## Step 8 — Autostart (required — prevents the silent outages)

A hand-started `uvicorn` is a foreground process: a reboot, sleep, or crash kills
it and the dev-box pool silently falls back to the slow CPU embedders until
someone notices. Wrap it so it survives:

- **Preferred — Windows service via [NSSM](https://nssm.cc/):** point the service
  at the venv's `python.exe` with arguments `-m uvicorn app:app --host 0.0.0.0
  --port 8001`, set the working dir to the repo, set the step-4 env vars as the
  service environment, startup **Automatic**, and enable restart-on-failure.
- **Alternative — Scheduled Task "At log on / At startup"** running the same
  command. NSSM is preferred because it restarts on crash; a Task does not.

## Step 9 — Report back to Ingmar

```
Tailscale IP:   <100.x.y.z>
GPU:            <cuda_device_name from /health>
Bearer token:   <token>   (additional box: "same as MSI — reused EMBEDDER_API_KEY")
Firewall rule:  added (Public profile, 8001)
Autostart:      NSSM service installed / Scheduled Task / not yet
/health:        200 OK locally
```

Ingmar adds the endpoint to `EMBEDDER_URLS` on the dev box (consumer-side config
— see `amphora-help-bot`); the pool picks it up on the next process restart.

## Failure modes

| Symptom | Action |
|---|---|
| `nvidia-smi` not found | Driver not installed. Install latest Game Ready / Studio driver. |
| `torch.cuda.is_available() == False` | Wrong CUDA wheel. Fall back cu128 → cu126 → cu124 → cu121. |
| First embed times out (>30 s) | Model still downloading. Watch uvicorn for weight-load progress. |
| Port 8001 in use | `Get-NetTCPConnection -LocalPort 8001` to find the PID. |
| 401 from `/embed` | Token mismatch. Check `$env:EMBEDDER_API_KEY`. |
| `device: "cpu"` despite a GPU | `$env:EMBEDDER_DEVICE` didn't stick. Re-set and restart. |
| Dev box can't reach `100.x.y.z:8001` | Firewall (step 6) or Tailscale ACL. Check `tailscale status`. |

## What NOT to do

- Don't commit the bearer token — runtime secret.
- Don't change the model — the pool refuses mismatched endpoints on purpose.
- Don't change the `torch==2.11.0` pin — CPU and GPU nodes stay in lockstep.
- Don't expose 8001 on a public interface — Tailscale is the only intended path.

## See also

- `README.md` (this repo) § "MSI GPU box" — the short run+autostart summary.
- `amphora-help-bot/services/ingestion/_lib.py` — the `EmbedderPool` consumer.
- `amphora-help-bot/docs/gpu-capacity-plan.md` — multi-box pool topology.
- The **reranker** GPU service is separate and still lives in `amphora-help-bot`
  (`docs/gpu-reranker-runbook-for-claude.md`); this runbook is embedder-only.
