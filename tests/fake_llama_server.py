"""Test double for llama.cpp's llama-server: same flags, same endpoints, fake weights."""
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mycel.models import CATALOG  # noqa: E402

if "--list-devices" in sys.argv:  # what real llama.cpp prints (device list goes to stdout)
    print(os.environ.get("FAKE_LIST_DEVICES", "Available devices:\n  CUDA0: NVIDIA GeForce RTX 3090 (24135 MiB, 23700 MiB free)"))
    sys.exit(0)
ap = argparse.ArgumentParser()
ap.add_argument("-hf"); ap.add_argument("--host"); ap.add_argument("--port", type=int); ap.add_argument("--alias")
ap.add_argument("--jinja", action="store_true"); ap.add_argument("-ngl"); ap.add_argument("-c"); ap.add_argument("--reasoning-budget")
args = ap.parse_args()
assert args.host == "127.0.0.1", "model server must bind loopback"
model = next(m for m in CATALOG if m.hf == args.hf)
cache = Path(os.environ["LLAMA_CACHE"])
repo_dir = cache / ("models--" + model.hf.split(":")[0].replace("/", "--"))
target = repo_dir / "snapshots" / "abc123" / model.file   # Hugging Face cache layout, like real llama.cpp
ready = False


async def download_then_load(app):
    global ready
    if not target.exists():
        (repo_dir / "blobs").mkdir(parents=True, exist_ok=True)
        part = repo_dir / "blobs" / "deadbeef.downloadInProgress"
        part.write_bytes(b"x" * 1000)
        # Real llama.cpp redraws a progress bar with \r and no newline — a 200 KB "line".
        for i in range(2000):
            sys.stderr.write(f"\r[{'=' * (i % 50):50}] {i / 20:.1f}%  downloading {model.file}" + " " * 40)
        sys.stderr.flush()
        await asyncio.sleep(0.6)
        target.parent.mkdir(parents=True, exist_ok=True)
        part.rename(target)
    await asyncio.sleep(0.3)
    ready = True


async def health(_):
    return web.json_response({"status": "ok"} if ready else {"status": "loading"}, status=200 if ready else 503)


async def models(_):
    return web.json_response({"data": [{"id": args.alias}]})


async def chat(request):
    body = await request.json()
    resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
    await resp.prepare(request)
    thinking = "Okay, the user wants a greeting. Keep it short.".split()      # Qwen3-style reasoning first
    words = f"Hello from {args.alias} running on this device.".split()
    stream = [("reasoning_content", w) for w in thinking] + [("content", w) for w in words * 6]
    for i, (field, w) in enumerate(stream[: body.get("max_tokens", 64)]):
        chunk = {"choices": [{"delta": {field: ("" if i == 0 else " ") + w}}]}
        await resp.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
        await asyncio.sleep(0.005)
    await resp.write(b"data: [DONE]\n\n")
    return resp


app = web.Application()
async def on_startup(a):
    asyncio.get_running_loop().create_task(download_then_load(a))


app.on_startup.append(on_startup)
app.router.add_get("/health", health)
app.router.add_get("/v1/models", models)
app.router.add_post("/v1/chat/completions", chat)
web.run_app(app, host=args.host, port=args.port, print=None)
