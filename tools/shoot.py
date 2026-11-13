"""Drive headless Chrome over the DevTools protocol to screenshot app flows.

Usage: python3 -I tools/shoot.py <out_dir> <scenario> [theme]
"""
import asyncio
import base64
import json
import subprocess
import sys
import tempfile
import time

import aiohttp

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
import os
BASE = os.environ.get("MYCEL_URL", "http://127.0.0.1:7800")


class Page:
    def __init__(self, ws):
        self.ws, self.n = ws, 0

    async def cmd(self, method, **params):
        self.n += 1
        await self.ws.send_json({"id": self.n, "method": method, "params": params})
        while True:
            msg = await self.ws.receive_json()
            if msg.get("id") == self.n:
                if "error" in msg:
                    raise RuntimeError(msg["error"])
                return msg.get("result", {})

    async def js(self, expr):
        r = await self.cmd("Runtime.evaluate", expression=expr, awaitPromise=True, returnByValue=True)
        if r.get("exceptionDetails"):
            raise RuntimeError(r["exceptionDetails"])
        return r.get("result", {}).get("value")

    async def go(self, url, wait=1.5):
        await self.cmd("Page.navigate", url=url)
        await asyncio.sleep(wait)

    async def shot(self, path, full=False):
        params = {"format": "png"}
        if full:
            h = await self.js("Math.max(document.querySelector('.view')?.scrollHeight||0, innerHeight)")
            await self.cmd("Emulation.setDeviceMetricsOverride", width=1440, height=int(h) + 60, deviceScaleFactor=1, mobile=False)
            await asyncio.sleep(0.4)
        data = (await self.cmd("Page.captureScreenshot", **params))["data"]
        open(path, "wb").write(base64.b64decode(data))
        if full:
            await self.cmd("Emulation.setDeviceMetricsOverride", width=1440, height=900, deviceScaleFactor=1, mobile=False)
        print("saved", path)

    async def type_and_send(self, text):
        await self.js(f"""(() => {{ const t = document.querySelector('.composer textarea'); t.value = {json.dumps(text)};
            t.dispatchEvent(new Event('input')); t.dispatchEvent(new KeyboardEvent('keydown', {{key: 'Enter', bubbles: true}})); }})()""")


async def main(out, scenario, theme):
    prof = tempfile.mkdtemp()
    proc = subprocess.Popen([CHROME, "--headless=new", "--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--no-first-run", f"--user-data-dir={prof}",
                             "--remote-debugging-port=9333", "--remote-debugging-address=127.0.0.1", "--window-size=1440,900",
                             "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        async with aiohttp.ClientSession() as s:
            for _ in range(50):
                try:
                    async with s.get("http://127.0.0.1:9333/json") as r:
                        tabs = await r.json()
                        break
                except aiohttp.ClientError:
                    await asyncio.sleep(0.2)
            ws_url = next(t["webSocketDebuggerUrl"] for t in tabs if t["type"] == "page")
            async with s.ws_connect(ws_url, max_msg_size=64 << 20) as ws:
                p = Page(ws)
                await p.cmd("Page.enable")
                await p.cmd("Emulation.setDeviceMetricsOverride", width=1440, height=900, deviceScaleFactor=1, mobile=False)
                await p.cmd("Emulation.setEmulatedMedia", features=[{"name": "prefers-color-scheme", "value": theme}])
                await p.go(BASE + "/")
                if scenario == "onboarding":
                    await p.shot(f"{out}/onboard-1.png")
                    await p.js("document.querySelector('.onboard .btn.glow').click()"); await asyncio.sleep(0.5)
                    await p.shot(f"{out}/onboard-2.png")
                    await p.js("document.querySelector('.onboard .btn.glow').click()"); await asyncio.sleep(0.4)
                    await p.shot(f"{out}/onboard-3.png")
                    await asyncio.sleep(4)
                    await p.shot(f"{out}/onboard-4.png")
                    return
                await p.js("localStorage.setItem('mycel.prefs', JSON.stringify({onboarded: true}))")
                await p.go(BASE + "/?r=1#/chat", wait=2)
                if scenario == "chat":
                    await p.shot(f"{out}/chat-empty.png")
                    await p.type_and_send("Explain how mesh networks survive internet blackouts")
                    await asyncio.sleep(0.9)
                    await p.shot(f"{out}/chat-streaming.png")
                    await asyncio.sleep(4)
                    await p.type_and_send("What is 1499 * 12 / 7?")
                    await asyncio.sleep(6)
                    await p.js("document.querySelector('.thread').scrollTop = 1e6"); await asyncio.sleep(0.3)
                    await p.shot(f"{out}/chat-tools.png")
                    await p.type_and_send("Please remember that the demo is on Friday")
                    await asyncio.sleep(3)
                    await p.js("document.querySelector('.thread').scrollTop = 1e6"); await asyncio.sleep(0.3)
                    await p.shot(f"{out}/chat-approval.png")
                    await p.js("document.querySelector('.tool-approve .btn.primary')?.click()"); await asyncio.sleep(4)
                    await p.js("document.querySelector('.thread').scrollTop = 1e6"); await asyncio.sleep(0.3)
                    await p.shot(f"{out}/chat-approved.png")
                    await p.js("document.querySelector('.composer .cchip').click()"); await asyncio.sleep(0.5)
                    await p.shot(f"{out}/chat-models.png")
                    await p.js("document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape'}))"); await asyncio.sleep(0.2)
                    await p.js("document.querySelector('.node-btn').click()"); await asyncio.sleep(0.8)
                    await p.shot(f"{out}/chat-node-panel.png")
                    return
                if scenario == "glcheck":
                    await p.go(f"{BASE}/?g=1#/network", wait=3)
                    print("orb-gl:", await p.js("!!document.querySelector('.orb-gl')"), "aurora:", await p.js("!!document.querySelector('canvas.aurora')"),
                          "globe:", await p.js("!!document.querySelector('canvas.globe')"))
                    await p.shot(f"{out}/glcheck.png")
                    await p.js("document.querySelector('.globe-card').scrollIntoView()"); await asyncio.sleep(1.5)
                    await p.shot(f"{out}/globe.png")
                    return
                if scenario == "cockpit":
                    await p.go(f"{BASE}/?c=1#/network", wait=2.5)
                    await p.js("[...document.querySelectorAll('.mode-seg button')].find(b => b.textContent.includes('Private')).click()")
                    await asyncio.sleep(2.0)
                    await p.shot(f"{out}/cockpit-private.png")
                    await p.js("document.querySelector('.btn.glow-swarm').click()"); await asyncio.sleep(0.6)
                    await p.shot(f"{out}/cockpit-swap-modal.png")
                    await p.js("document.querySelector('.modal .btn.glow').click()")
                    await asyncio.sleep(9)
                    await p.shot(f"{out}/cockpit-swarm.png")
                    await p.go(f"{BASE}/?c=2#/chat", wait=2.0)
                    await p.shot(f"{out}/chat-header.png")
                    await p.js("document.querySelector('.node-btn').click()"); await asyncio.sleep(1.5)
                    await p.shot(f"{out}/after-node-click.png")
                    return
                if scenario == "swarm":
                    await p.js("document.querySelector('.composer .cchip').click()"); await asyncio.sleep(0.6)
                    await p.shot(f"{out}/swarm-picker.png")
                    await p.js("[...document.querySelectorAll('.pop .opt')].find(o => o.querySelector('.badge.swarm') && !o.classList.contains('disabled'))?.click()")
                    await asyncio.sleep(0.5)
                    await p.shot(f"{out}/swarm-selected.png")
                    await p.type_and_send("Write a short poem about a forest that thinks together")
                    await asyncio.sleep(0.35)
                    await p.shot(f"{out}/swarm-assembling.png")
                    await asyncio.sleep(2.5)
                    await p.shot(f"{out}/swarm-streaming.png")
                    for _ in range(40):
                        if await p.js("!!document.querySelector('.prov.swarm')"):
                            break
                        await asyncio.sleep(0.5)
                    await p.js("document.querySelector('.thread').scrollTop = 1e6"); await asyncio.sleep(0.3)
                    await p.shot(f"{out}/swarm-done.png")
                    await p.js("document.querySelector('.prov.swarm').click()"); await asyncio.sleep(1.6)
                    await p.shot(f"{out}/swarm-path.png")
                    await p.js("document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape'}))"); await asyncio.sleep(0.3)
                    await p.js("document.querySelector('.cchip.route').click()"); await asyncio.sleep(0.6)
                    await p.shot(f"{out}/swarm-explain.png")
                    return
                for view in scenario.split(","):
                    await p.go(f"{BASE}/?v={view}#/{view}", wait=2)
                    await p.shot(f"{out}/{view.replace('/', '-')}.png", full=True)
    finally:
        proc.terminate()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "dark"))
