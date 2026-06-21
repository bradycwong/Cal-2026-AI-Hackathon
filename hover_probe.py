"""Reproduce the inventory depleted-row hover cascade in real Edge via CDP.

Loads a harness using the EXACT rules from FrontendTest/inventory.css, forces
:hover on a depleted row, and reads the computed background-color. Red wins =>
no bug. Blue/transparent wins => bug reproduced.
"""
import asyncio, base64, json, subprocess, tempfile, time, urllib.request
import websockets

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PORT = 9337

# Exact rules copied from FrontendTest/inventory.css (lines 6-15).
HARNESS = """<!doctype html><html><head><meta charset=utf-8><style>
.inventory-row { padding:20px; background: transparent; }
.inventory-row:hover { background: rgba(184, 195, 255, 0.04); }
.inventory-row.inv-depleted { background: rgba(255, 107, 107, 0.10); }
.inventory-row.inv-depleted:hover { background: rgba(255, 107, 107, 0.16); }
</style></head><body>
<div id="dep" class="inventory-row grid items-center transition-colors group inv-depleted">Depleted</div>
<div id="norm" class="inventory-row grid items-center transition-colors group">Normal</div>
</body></html>"""


async def call(ws, mid, method, params=None):
    await ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
    while True:
        msg = json.loads(await ws.recv())
        if msg.get("id") == mid:
            return msg.get("result", {})


async def main():
    udir = tempfile.mkdtemp()
    proc = subprocess.Popen([EDGE, "--headless=new", "--disable-gpu",
                             f"--remote-debugging-port={PORT}",
                             f"--user-data-dir={udir}", "about:blank"])
    try:
        target = None
        for _ in range(60):
            try:
                lst = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list"))
                pages = [t for t in lst if t.get("type") == "page"]
                if pages:
                    target = pages[0]; break
            except Exception:
                pass
            time.sleep(0.25)
        if not target:
            print("NO_TARGET"); return
        async with websockets.connect(target["webSocketDebuggerUrl"], max_size=None) as ws:
            i = 0
            for dom in ("DOM.enable", "CSS.enable", "Page.enable"):
                i += 1; await call(ws, i, dom)
            url = "data:text/html;base64," + base64.b64encode(HARNESS.encode()).decode()
            i += 1; await call(ws, i, "Page.navigate", {"url": url})
            await asyncio.sleep(1.0)
            i += 1; doc = await call(ws, i, "DOM.getDocument", {"depth": -1})
            root = doc["root"]["nodeId"]

            async def bg(selector, hover):
                nonlocal i
                i += 1
                node = await call(ws, i, "DOM.querySelector", {"nodeId": root, "selector": selector})
                nid = node["nodeId"]
                i += 1
                await call(ws, i, "CSS.forcePseudoState",
                           {"nodeId": nid, "forcedPseudoClasses": ["hover"] if hover else []})
                i += 1
                cs = await call(ws, i, "CSS.getComputedStyleForNode", {"nodeId": nid})
                props = {p["name"]: p["value"] for p in cs["computedStyle"]}
                return props.get("background-color")

            print("depleted  (rest ):", await bg("#dep", False))
            print("depleted  (hover):", await bg("#dep", True))
            print("normal    (hover):", await bg("#norm", True))
    finally:
        proc.terminate()


asyncio.run(main())
