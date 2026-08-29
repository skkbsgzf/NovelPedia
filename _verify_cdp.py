# -*- coding: utf-8 -*-
"""CDP 真机渲染验证 index.html —— 捕获 JS 异常 + 检查 SVG 实际绘制 + 模拟点击切页。"""
import json, os, subprocess, sys, time, urllib.request
from pathlib import Path
import websocket

HTML = Path(r"D:\StoryScienceLab\novel_pipeline\outputs\诸神愚戏_20260828_235204\index.html")
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PORT = 9222
OUT = Path(r"D:\StoryScienceLab\novel_pipeline\_verify_shots")
OUT.mkdir(parents=True, exist_ok=True)

proc = subprocess.Popen([
    EDGE, "--headless=new", f"--remote-debugging-port={PORT}",
    "--remote-allow-origins=*", "--no-first-run", "--no-default-browser-check",
    f"--user-data-dir={OUT / 'profile'}", "--window-size=1440,960",
    "about:blank",
], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def get_ws():
    for i in range(40):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json", timeout=2) as r:
                tabs = json.loads(r.read())
            for t in tabs:
                if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                    return t["webSocketDebuggerUrl"]
        except Exception:
            pass
        time.sleep(0.5)
    return None

ws_url = get_ws()
if not ws_url:
    print("FAIL: 无法连接 Edge 调试端口"); proc.kill(); sys.exit(1)

ws = websocket.create_connection(ws_url, timeout=40)
_id = [0]
errors, logs = [], []

def send(method, params=None, sid=None):
    _id[0] += 1
    msg = {"id": _id[0], "method": method, "params": params or {}}
    if sid: msg["sessionId"] = sid
    ws.send(json.dumps(msg))
    while True:
        resp = json.loads(ws.recv())
        if resp.get("id") == _id[0]:
            return resp.get("result", {})
        # 事件
        if resp.get("method") == "Runtime.exceptionThrown":
            d = resp["params"].get("exceptionDetails", {})
            errors.append(f"{d.get('text')} @ {d.get('url','')}:{d.get('lineNumber')}")
        elif resp.get("method") == "Runtime.consoleAPICalled":
            if resp["params"].get("type") == "error":
                logs.append(" ".join(str(a.get("value")) for a in resp["params"].get("args", [])))

send("Runtime.enable")
send("Page.enable")
send("Runtime.enable")
send("Log.enable")

def evaluate(expr):
    r = send("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True})
    return r.get("result", {}).get("value")

def shot(name):
    r = send("Page.captureScreenshot", {"format": "png"})
    import base64
    data = r.get("data", "")
    if data:
        (OUT / f"{name}.png").write_bytes(base64.b64decode(data))
        return f"{name}.png ({(OUT / (name + '.png')).stat().st_size // 1024}KB)"
    return f"{name} 截图失败"

url = HTML.as_uri()
send("Page.navigate", {"url": url})
time.sleep(5)  # 大 JSON 解析 + 力导向迭代

report = {"errors": [], "checks": {}, "shots": []}

# ---- 1) 概览页 ----
report["checks"]["标题"] = evaluate("document.querySelector('h1.art')?.textContent")
report["checks"]["统计卡"] = evaluate("document.querySelectorAll('.stat .v').length")
report["checks"]["统计值"] = evaluate("[...document.querySelectorAll('.stat')].map(s=>s.querySelector('.k').textContent+'='+s.querySelector('.v').textContent).join(' | ')")
report["checks"]["侧栏人物数"] = evaluate("document.querySelectorAll('#sl-char a').length")
report["checks"]["侧栏设定分类数"] = evaluate("document.querySelectorAll('.grp .gh').length")
report["checks"]["顶栏计数"] = evaluate("document.getElementById('topcnt').textContent")

# ---- 2) 设定图谱 SVG 实际绘制 ----
report["checks"]["图谱circle数"] = evaluate("document.querySelectorAll('#sgSvg circle').length")
report["checks"]["图谱text数"] = evaluate("document.querySelectorAll('#sgSvg text').length")
report["checks"]["图谱line数"] = evaluate("document.querySelectorAll('#sgSvg line').length")
report["checks"]["图谱尺寸"] = evaluate("(()=>{const s=document.getElementById('sgSvg');if(!s)return 'no-svg';const r=s.getBoundingClientRect();return r.width.toFixed(0)+'x'+r.height.toFixed(0)+' bbox='+(s.getBBox?JSON.stringify(s.getBBox().width.toFixed(0)+'x'+s.getBBox().height.toFixed(0)):'?')})()")
report["checks"]["第一个circle填充"] = evaluate("(()=>{const c=document.querySelector('#sgSvg circle');if(!c)return 'none';const r=c.getBoundingClientRect();return c.getAttribute('fill')+' rect='+r.width.toFixed(1)+'x'+r.height.toFixed(1)})()")
report["shots"].append(shot("01_overview"))

# ---- 3) 点第一个人物 -> 简历页 ----
evaluate("(()=>{const a=document.querySelector('#sl-char a');if(a)a.click()})()")
time.sleep(1.2)
report["checks"]["人物页_标题"] = evaluate("document.querySelector('h1.art')?.textContent")
report["checks"]["人物页_Infobox"] = evaluate("!!document.querySelector('.infobox')")
report["checks"]["人物页_Infobox行数"] = evaluate("document.querySelectorAll('.infobox tr').length")
report["checks"]["人物页_时间线柱数"] = evaluate("document.querySelectorAll('.tl .b').length")
report["checks"]["人物页_语录数"] = evaluate("document.querySelectorAll('.quote').length")
report["checks"]["人物页_关系标签"] = evaluate("document.querySelectorAll('.tag.rel').length")
report["checks"]["人物页_章节标题"] = evaluate("[...document.querySelectorAll('.sec>h2')].map(h=>h.textContent).join(' / ')")
report["checks"]["人物页_内链数"] = evaluate("document.querySelectorAll('a.wl').length")
report["shots"].append(shot("02_char"))

# ---- 4) 点设定词条 ----
# 注意: 人物组默认带 open 且无 .gh, 必须用 .gh[data-cat] 精确定位设定分类,
# 再用其 nextElementSibling 取该分类的 items, 否则会误选到人物链接。
evaluate("(()=>{const g=document.querySelector('.gh[data-cat]');if(g)g.click()})()")
time.sleep(0.6)
report["checks"]["设定_首个分类"] = evaluate("document.querySelector('.gh[data-cat]')?.textContent")
evaluate("(()=>{const g=document.querySelector('.gh[data-cat]');const items=g&&g.nextElementSibling;const a=items&&items.querySelector('a');if(a)a.click()})()")
time.sleep(2.0)
report["checks"]["设定页_标题"] = evaluate("document.querySelector('h1.art')?.textContent")
report["checks"]["设定页_Infobox"] = evaluate("!!document.querySelector('.infobox')")
report["checks"]["设定页_关系数"] = evaluate("document.querySelectorAll('.card.info li').length")
report["checks"]["设定页_局部图谱circle"] = evaluate("document.querySelectorAll('#sgSvg circle').length")
report["checks"]["Infobox局部小图circle"] = evaluate("document.querySelectorAll('#miniSvg circle').length")
report["checks"]["Infobox局部小图line"] = evaluate("document.querySelectorAll('#miniSvg line').length")
report["checks"]["Infobox局部小图尺寸"] = evaluate("(()=>{const s=document.getElementById('miniSvg');return s?s.getBoundingClientRect().width.toFixed(0)+'x'+s.getBoundingClientRect().height.toFixed(0):'none'})()")
report["checks"]["Infobox大字元素"] = evaluate("!!document.querySelector('.ib-av .ini')")
report["checks"]["来源徽章数"] = evaluate("document.querySelectorAll('.src-badge').length")
report["checks"]["设定页_同类标签"] = evaluate("document.querySelectorAll('.tag[data-go=\"term\"]').length")
report["shots"].append(shot("03_term"))

# 4b) 再测一个「高关联」词条, 确认局部图谱(focus 模式)能正常展开
evaluate("(()=>{const deg={};(SG.relations||[]).forEach(r=>{deg[r.from]=(deg[r.from]||0)+1;deg[r.to]=(deg[r.to]||0)+1;});"
         "const top=Object.keys(deg).filter(n=>TERMS[n]).sort((a,b)=>deg[b]-deg[a])[0];go('term',top)})()")
time.sleep(2.0)
report["checks"]["高关联词条"] = evaluate("document.querySelector('h1.art')?.textContent")
report["checks"]["高关联_关系条数"] = evaluate("document.querySelectorAll('.card.info li').length")
report["checks"]["高关联_局部图谱circle"] = evaluate("document.querySelectorAll('#sgSvg circle').length")
report["checks"]["高关联_局部图谱line"] = evaluate("document.querySelectorAll('#sgSvg line').length")
report["shots"].append(shot("03b_term_hub"))

# ---- 5) 剧情推理页 ----
evaluate("(()=>{const a=[...document.querySelectorAll('.side .nav a')].find(x=>x.textContent.indexOf('剧情')>=0);if(a)a.click()})()")
time.sleep(2.5)
report["checks"]["剧情页_结论卡数"] = evaluate("document.querySelectorAll('.card.clk').length")
report["checks"]["剧情页_线索图谱circle"] = evaluate("document.querySelectorAll('#clueSvg circle').length")
report["shots"].append(shot("04_plot"))

# ---- 6) 搜索 ----
evaluate("(()=>{const q=document.getElementById('q');q.value='程';q.dispatchEvent(new Event('input'))})()")
time.sleep(0.5)
report["checks"]["搜索建议数"] = evaluate("document.querySelectorAll('#sug div').length")
report["shots"].append(shot("05_search"))

report["errors"] = errors + logs
ws.close()
proc.terminate()
try: proc.wait(timeout=5)
except Exception: proc.kill()

print("=" * 70)
for k, v in report["checks"].items():
    print(f"  {k:24s} = {v}")
print("-" * 70)
print("  截图:", ", ".join(report["shots"]))
print("  JS错误:", report["errors"] if report["errors"] else "无 ✅")
print("=" * 70)
