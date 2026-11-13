"""Record the Mycel launch video by driving the real app in headless Chrome.

Usage: python3 -I tools/record.py <out.mp4>
Requires: a running `mycel simulate --fresh --control-port 8000 --mesh-port 7900` (never your real
node), Chrome, ffmpeg. Set MYCEL_URL to point elsewhere.
Footage of the app is labeled "Demo network" on screen because the members are simulated.
"""
import asyncio
import base64
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import aiohttp
import cv2
import numpy as np

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
import os
APP = os.environ.get("MYCEL_URL", "http://127.0.0.1:8000")
CARDS = (Path(__file__).resolve().parent.parent / "launch/video/cards.html").as_uri()
W, H, FPS, FADE = 1920, 1080, 30, 12

CAPTION_JS = """(text => {
  let el = document.getElementById('__cap');
  if (!el) {
    el = document.createElement('div'); el.id = '__cap';
    el.style.cssText = 'position:fixed;left:50%;bottom:54px;transform:translateX(-50%);z-index:9999;padding:16px 28px;border-radius:18px;' +
      'background:rgba(10,10,14,.82);backdrop-filter:blur(12px);color:#fff;font:600 30px/1.2 -apple-system,system-ui,sans-serif;' +
      'letter-spacing:-.01em;box-shadow:0 12px 40px rgba(0,0,0,.45);border:1px solid rgba(255,255,255,.12);transition:opacity .4s;white-space:nowrap';
    document.body.appendChild(el);
    const tag = document.createElement('div');
    tag.style.cssText = 'position:fixed;top:14px;right:18px;z-index:9999;padding:5px 11px;border-radius:999px;background:rgba(10,10,14,.7);' +
      'color:#b9b8b2;font:600 13px -apple-system,system-ui,sans-serif;border:1px solid rgba(255,255,255,.12)';
    tag.textContent = 'Demo network · simulated members'; document.body.appendChild(tag);
  }
  el.style.opacity = text ? '1' : '0'; el.textContent = text;
})"""


class Browser:
    def __init__(self, ws):
        self.ws, self.n, self.pending, self.frames, self.recording = ws, 0, {}, [], False

    async def reader(self):
        async for msg in self.ws:
            data = json.loads(msg.data)
            if "id" in data and data["id"] in self.pending:
                self.pending.pop(data["id"]).set_result(data)
            elif data.get("method") == "Page.screencastFrame":
                p = data["params"]
                if self.recording:
                    self.frames.append((time.monotonic(), p["data"]))
                asyncio.create_task(self.cmd("Page.screencastFrameAck", sessionId=p["sessionId"]))

    async def cmd(self, method, **params):
        self.n += 1
        fut = asyncio.get_running_loop().create_future()
        self.pending[self.n] = fut
        await self.ws.send_json({"id": self.n, "method": method, "params": params})
        res = await fut
        if "error" in res:
            raise RuntimeError(f"{method}: {res['error']}")
        return res.get("result", {})

    async def js(self, expr):
        r = await self.cmd("Runtime.evaluate", expression=expr, awaitPromise=True, returnByValue=True)
        if r.get("exceptionDetails"):
            raise RuntimeError(r["exceptionDetails"].get("exception", {}).get("description", r["exceptionDetails"]))
        return r.get("result", {}).get("value")

    async def go(self, url, settle=1.5):
        # Title cards are authored at 1920x1080 CSS px; the app is shown at 1.35x so it reads on video.
        scale = 1.0 if url.startswith("file:") else 1.35
        await self.cmd("Emulation.setDeviceMetricsOverride", width=round(W / scale), height=round(H / scale),
                       deviceScaleFactor=scale, mobile=False)
        await self.cmd("Page.navigate", url=url)
        await asyncio.sleep(settle)

    async def caption(self, text):
        await self.js(f"{CAPTION_JS}({json.dumps(text)})")

    async def click(self, selector):
        await self.js(f"document.querySelector({json.dumps(selector)}).click()")

    async def type_send(self, text):
        await self.js(f"""(() => {{ const t = document.querySelector('.composer textarea'); t.value = {json.dumps(text)};
          t.dispatchEvent(new Event('input')); t.dispatchEvent(new KeyboardEvent('keydown', {{key: 'Enter', bubbles: true}})); }})()""")


async def scenes(b: Browser, segments: list):
    async def seg(seconds, *, actions=()):
        """Record `seconds` of video while running timed actions [(t, coro_fn), ...]."""
        start = time.monotonic()
        b.recording = True
        for t, fn in actions:
            await asyncio.sleep(max(0, start + t - time.monotonic()))
            await fn()
        await asyncio.sleep(max(0, start + seconds - time.monotonic()))
        b.recording = False
        segments.append((start, start + seconds))

    async def nudge():  # force a repaint so a static page still yields frames
        await b.js("document.body.style.outline = document.body.style.outline ? '' : '0px solid transparent'")

    # 1-3: title cards
    for scene, dur in (("title", 5.5), ("problem", 5.0), ("idea", 6.0)):
        await b.go(f"{CARDS}#{scene}", settle=0.05)
        await seg(dur, actions=[(i * 0.5, nudge) for i in range(int(dur * 2))])

    # 4: onboarding — join the global mesh in one tap
    await b.go(APP + "/", settle=2.0)
    await b.js("localStorage.clear()")
    await b.go(APP + "/?fresh=1", settle=2.0)
    await seg(8.0, actions=[(0.0, nudge), (1.4, lambda: b.click(".onboard .btn.glow")),
                            (1.8, lambda: b.caption("Join the global mesh in one tap")),
                            (3.0, lambda: b.click(".onboard .btn.glow")),
                            (5.8, lambda: b.caption("No account. No owner. No open ports."))])
    await b.js("localStorage.setItem('mycel.prefs', JSON.stringify({onboarded: true, model: 'auto', preferLocal: false}))")
    # 5: a small model, one member's GPU
    await b.go(APP + "/?app=1#/chat", settle=2.0)
    await seg(7.0, actions=[(0.0, lambda: b.caption("Small models run on one member's GPU")),
                            (0.8, lambda: b.type_send("Explain how mesh networks survive internet blackouts"))])
    # 6: the swarm section of the model picker
    await b.go(APP + "/?app=2#/chat", settle=1.5)
    await seg(5.5, actions=[(0.2, lambda: b.click(".composer .cchip")),
                            (0.6, lambda: b.caption("Giant models run as a swarm — across many GPUs")),
                            (1.5, lambda: b.js("document.querySelector('.pop .swarm-label')?.scrollIntoView({block: 'center', behavior: 'smooth'})"))])
    await b.js("[...document.querySelectorAll('.pop .opt')].find(o => o.querySelector('.badge.swarm') && !o.classList.contains('disabled'))?.click()")
    # 7: a swarm reply assembling and streaming
    await seg(10.0, actions=[(0.3, lambda: b.type_send("Write a short poem about a forest that thinks together")),
                             (0.5, lambda: b.caption("Your prompt crosses six GPUs — as numbers, never words")),
                             (6.0, lambda: b.caption("Llama 3.3 70B. No single machine here could run it."))])
    for _ in range(30):
        if await b.js("!!document.querySelector('.prov.swarm')"):
            break
        await asyncio.sleep(0.5)
    # 8: the path
    await seg(9.0, actions=[(0.0, lambda: b.caption("Every reply shows the exact path it took")),
                            (1.0, lambda: b.click(".prov.swarm")),
                            (5.0, lambda: b.js("document.querySelector('.scrim')?.scrollTo?.(0, 0); document.querySelector('.modal')?.scrollTo({top: 420, behavior: 'smooth'})")),
                            (5.2, lambda: b.caption("Failovers, cross-checks, and who saw what"))])
    await b.js("document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape'}))")
    # 9: the swarm assembles itself
    await b.go(APP + "/?n=1#/network", settle=2.0)
    await seg(6.5, actions=[(0.0, lambda: b.caption("Nobody assigns layers. The swarm assembles itself.")), (3.0, nudge)])
    # 10: host a slice
    await b.js("fetch('/api/library/stop', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'})")
    await b.go(APP + "/?m=1#/models", settle=2.5)
    await seg(6.0, actions=[(0.0, lambda: b.caption("An ordinary GPU can hold a slice of a giant")), (3.0, nudge)])
    # 11: private by default
    await b.go(APP + "/?s=1#/settings/visibility", settle=2.0)
    await seg(6.5, actions=[(0.0, lambda: b.caption("Private by default — even for the people who serve")),
                            (3.2, lambda: b.js("document.querySelector('.view').scrollTo({top: 900, behavior: 'smooth'})"))])
    # 12: end card
    await b.go(f"{CARDS}#end", settle=0.05)
    await seg(6.5, actions=[(i * 0.5, nudge) for i in range(13)])


def render(frames, segments, out: Path):
    decoded = []
    for ts, data in frames:
        img = cv2.imdecode(np.frombuffer(base64.b64decode(data), np.uint8), cv2.IMREAD_COLOR)
        if img.shape[1] != W or img.shape[0] != H:
            img = cv2.resize(img, (W, H), interpolation=cv2.INTER_AREA)
        decoded.append((ts, img))
    clips = []
    for start, end in segments:
        seg = [(t, im) for t, im in decoded if start - 0.05 <= t <= end]
        if not seg:
            continue
        clip, k = [], 0
        for i in range(int((end - start) * FPS)):
            t = start + i / FPS
            while k + 1 < len(seg) and seg[k + 1][0] <= t:
                k += 1
            clip.append(seg[k][1])
        clips.append(clip)
    tmp = out.with_suffix(".raw.mp4")
    vw = cv2.VideoWriter(str(tmp), cv2.VideoWriter_fourcc(*"mp4v"), FPS, (W, H))
    for ci, clip in enumerate(clips):
        body = clip if ci == 0 else clip[FADE:]
        if ci > 0:  # crossfade from the previous clip's tail
            prev = clips[ci - 1][-1]
            for j in range(FADE):
                a = (j + 1) / (FADE + 1)
                vw.write(cv2.addWeighted(prev, 1 - a, clip[j], a, 0))
        for im in body:
            vw.write(im)
    vw.release()
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(tmp), "-c:v", "libx264", "-preset", "slow", "-crf", "18",
                    "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)], check=True)
    tmp.unlink()


async def main(out: Path):
    prof = tempfile.mkdtemp()
    proc = subprocess.Popen([CHROME, "--headless=new", "--no-first-run", f"--user-data-dir={prof}", "--hide-scrollbars",
                             "--remote-debugging-port=9334", "--remote-debugging-address=127.0.0.1", f"--window-size={W},{H}",
                             "--force-color-profile=srgb", "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        async with aiohttp.ClientSession() as s:
            for _ in range(50):
                try:
                    async with s.get("http://127.0.0.1:9334/json") as r:
                        tabs = await r.json()
                        break
                except aiohttp.ClientError:
                    await asyncio.sleep(0.2)
            url = next(t["webSocketDebuggerUrl"] for t in tabs if t["type"] == "page")
            async with s.ws_connect(url, max_msg_size=256 << 20) as ws:
                b = Browser(ws)
                reader = asyncio.create_task(b.reader())
                await b.cmd("Page.enable")
                await b.cmd("Emulation.setDeviceMetricsOverride", width=W, height=H, deviceScaleFactor=1, mobile=False)
                await b.cmd("Emulation.setEmulatedMedia", features=[{"name": "prefers-color-scheme", "value": "dark"}])
                await b.cmd("Page.startScreencast", format="jpeg", quality=92, maxWidth=W, maxHeight=H, everyNthFrame=1)
                segments = []
                await scenes(b, segments)
                await b.cmd("Page.stopScreencast")
                reader.cancel()
        print(f"captured {len(b.frames)} frames in {len(segments)} scenes; encoding…", flush=True)
        render(b.frames, segments, out)
        print("wrote", out)
    finally:
        proc.terminate()
        shutil.rmtree(prof, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1]).resolve()))
