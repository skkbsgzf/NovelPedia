# -*- coding: utf-8 -*-
"""Demo HTTP 服务：拖入小说 → 识别格式 → 输入章节数/选模型 → 拆书 → 下载产物。
纯 stdlib，零依赖。用法: python _web_demo.py [port]
API:
  GET  /                 -> 上传/识别页面
  POST /identify         -> 上传文件, 识别格式/章数, 返回 job+info
  POST /start?job=&chapters=&backend=  -> 启动拆书
  GET  /progress?job=X   -> 进度轮询
  GET  /download?path=X  -> 下载产物(index.html)
"""
import os, sys, json, time, subprocess, zipfile, urllib.parse, http.server, re

ROOT = os.path.dirname(os.path.abspath(__file__))
JOBS = os.path.join(ROOT, "demo_jobs")
os.makedirs(JOBS, exist_ok=True)
PY = sys.executable

BACKENDS = [
    {"id": "xiaohongshu", "label": "小红书 dots.ai（默认）", "default": True},
    {"id": "glm", "label": "GLM-4-Flash（智谱·免费）"},
    {"id": "qwen3", "label": "本地 qwen3:8b"},
]

INDEX_HTML = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>小说拆书 · Demo</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;background:#f5f6f8;color:#1f2329;padding:40px 20px;min-height:100vh}
.wrap{max-width:720px;margin:0 auto}
h1{font-size:26px;margin-bottom:6px}
.sub{color:#6b7280;font-size:13px;margin-bottom:28px}
.drop{border:2px dashed #c7ccd4;border-radius:16px;padding:46px 24px;text-align:center;background:#fff;cursor:pointer;transition:.15s}
.drop.drag{border-color:#4f7cff;background:#f0f4ff}
.drop p{font-size:15px;color:#374151;margin:8px 0}
.drop small{color:#9ca3af;font-size:12px}
.card{background:#fff;border-radius:12px;padding:20px 24px;box-shadow:0 1px 3px rgba(0,0,0,.06);margin-top:20px;display:none}
.card.show{display:block}
label{font-size:13px;color:#374151;display:block;margin-bottom:6px}
input,select{width:100%;padding:10px 14px;border:1px solid #e5e7eb;border-radius:10px;font-size:14px;margin-bottom:14px;outline:none}
.btn{background:#4f7cff;color:#fff;border:none;border-radius:10px;padding:12px 20px;font-size:15px;font-weight:600;cursor:pointer;width:100%}
.btn:hover{background:#3b66e0}.btn:disabled{background:#c7d2e5;cursor:not-allowed}
.info{font-size:13px;color:#374151;background:#f8fafc;border-radius:10px;padding:12px 16px;margin-bottom:16px;line-height:1.9}
.info b{color:#4f7cff}
.bar{height:10px;background:#e5e7eb;border-radius:6px;overflow:hidden;margin:12px 0}
.fill{height:100%;background:#4f7cff;width:0;transition:width .3s}
.stage{font-size:12px;color:#6b7280;text-transform:uppercase;letter-spacing:.5px;margin-top:6px}
.msg{font-size:14px;color:#374151;margin-top:4px}
.dl{display:inline-block;background:#10b981;color:#fff;padding:12px 20px;border-radius:10px;text-decoration:none;font-weight:600;margin-top:14px}
.dl:hover{background:#0f9d6b}
.err{color:#b42318;font-size:14px;margin-top:14px}
</style></head><body><div class="wrap">
<h1>📖 小说拆书 Demo</h1>
<p class="sub">上传小说 → 识别格式 → 输入章节数 → 拆书 → 下载</p>
<div class="drop" id="drop">
  <p>📄 拖拽文件到此处，或 <b>点击选择</b></p>
  <p id="fname" style="color:#9ca3af">未选择文件</p>
  <small>支持 .epub / .txt</small>
</div>
<input type="file" id="file" accept=".epub,.txt" style="display:none">
<div class="card" id="formCard">
  <div class="info" id="fileInfo"></div>
  <label>要拆的章节数</label>
  <input type="number" id="chapters" value="50" min="1" max="200">
  <label>选择模型</label>
  <select id="backend"></select>
  <button class="btn" id="startBtn" onclick="startBook()">🚀 开始拆书</button>
</div>
<div class="card" id="progCard">
  <div class="stage" id="stage">—</div>
  <div class="bar"><div class="fill" id="fill"></div></div>
  <div class="msg" id="msg">准备中…</div>
  <div class="result" id="result"></div>
</div>
<div class="err" id="err"></div>
</div>
<script>
const drop=document.getElementById('drop'),file=document.getElementById('file');
const BACKENDS=__BACKENDS__;
(function(){const sel=document.getElementById('backend');BACKENDS.forEach(b=>{const o=document.createElement('option');o.value=b.id;o.textContent=(b.default?'⭐ ':'')+b.label;if(b.default)o.selected=true;sel.appendChild(o);});})();
drop.addEventListener('click',()=>file.click());
file.addEventListener('change',()=>{if(file.files[0])identify(file.files[0]);});
['dragenter','dragover'].forEach(e=>drop.addEventListener(e,ev=>{ev.preventDefault();drop.classList.add('drag');}));
['dragleave','drop'].forEach(e=>drop.addEventListener(e,ev=>{ev.preventDefault();drop.classList.remove('drag');}));
drop.addEventListener('drop',ev=>{ev.preventDefault();const f=ev.dataTransfer.files[0];if(f){file.files=ev.dataTransfer.files;identify(f);}});
function esc(s){return(s==null?'':String(s)).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
function identify(f){
  document.getElementById('fname').textContent=f.name;
  const fd=new FormData();fd.append('book',f);
  const xhr=new XMLHttpRequest();xhr.open('POST','/identify');xhr.send(fd);
  xhr.onload=()=>{const r=JSON.parse(xhr.responseText);if(r.error){document.getElementById('err').textContent=r.error;return;}
    document.getElementById('fileInfo').innerHTML='📕 <b>'+esc(r.name)+'</b> &nbsp;|&nbsp; 格式: <b>'+esc(r.format)+'</b> &nbsp;|&nbsp; 识别到 <b>'+r.chapters+'</b> 章';
    window.job=r.job;
    document.getElementById('formCard').classList.add('show');
  };
}
function startBook(){
  const chapters=document.getElementById('chapters').value;
  const backend=document.getElementById('backend').value;
  document.getElementById('startBtn').disabled=true;
  document.getElementById('progCard').classList.add('show');
  const xhr=new XMLHttpRequest();xhr.open('POST','/start?job='+window.job+'&chapters='+chapters+'&backend='+backend);xhr.send();
  xhr.onload=()=>poll(window.job);
}
function poll(job){
  const t=setInterval(()=>{
    const xhr=new XMLHttpRequest();xhr.open('GET','/progress?job='+job);xhr.send();
    xhr.onload=()=>{
      const r=JSON.parse(xhr.responseText);
      document.getElementById('fill').style.width=r.percent+'%';
      document.getElementById('msg').textContent=r.message;
      document.getElementById('stage').textContent=r.stage;
      if(r.done){clearInterval(t);
        if(r.result){
          document.getElementById('result').innerHTML='<a class="dl" href="/download?path='+encodeURIComponent(r.result.index)+'">📄 下载 '+esc(r.result.novel)+' 产物</a>';
          document.getElementById('startBtn').disabled=false;
        }else if(r.error){document.getElementById('err').textContent='❌ '+r.error;}
      }
    };
  },2000);
}
</script></body></html>"""


def identify_book(path):
    """识别书名/格式/章数(epub 精解 / txt 正则, 不建 db)。"""
    lower = path.lower()
    if lower.endswith('.epub'):
        import zipfile
        z = zipfile.ZipFile(path)
        opf = [n for n in z.namelist() if n.endswith('.opf')]
        n_ch = 0
        if opf:
            o = z.read(opf[0]).decode('utf-8', errors='replace')
            n_ch = len(re.findall(r'<itemref[^>]*idref="', o))
        # 精确: 用 ingest 只读章节数(不建库, 不设上限得真实总章数)
        try:
            sys.path.insert(0, os.path.join(ROOT, 'src'))
            from ingest import ingest
            _, chs = ingest(path, max_chapters=None)
            if chs:
                n_ch = len(chs)
        except Exception:
            pass
        return {'name': os.path.splitext(os.path.basename(path))[0],
                'format': 'epub', 'chapters': n_ch or 50}
    else:
        try:
            txt = open(path, encoding='utf-8', errors='ignore').read()
        except Exception:
            txt = ''
        chaps = re.findall(r'第[0-9一二三四五六七八九十百千]+章', txt)
        return {'name': os.path.splitext(os.path.basename(path))[0],
                'format': 'txt', 'chapters': len(chaps) or 50}


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, body, ctype='application/json; charset=utf-8'):
        if isinstance(body, str): body = body.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers(); self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path in ('/', ''):
            self._send(200, INDEX_HTML.replace('__BACKENDS__', json.dumps(BACKENDS)),
                       'text/html; charset=utf-8')
        elif u.path == '/progress':
            job = urllib.parse.parse_qs(u.query).get('job', [''])[0]
            p = os.path.join(JOBS, job, 'progress.json')
            self._send(200, open(p, encoding='utf-8').read() if os.path.exists(p)
                       else '{"stage":"init","percent":0,"message":"排队中","done":false}')
        elif u.path == '/download':
            path = urllib.parse.parse_qs(u.query).get('path', [''])[0]
            fp = os.path.normpath(os.path.join(ROOT, path))
            if os.path.isfile(fp):
                data = open(fp, 'rb').read()
                self.send_response(200)
                self.send_header('Content-Type', 'application/octet-stream')
                self.send_header('Content-Disposition',
                                 'attachment; filename="%s"' % os.path.basename(fp))
                self.send_header('Content-Length', str(len(data)))
                self.end_headers(); self.wfile.write(data)
            else:
                self._send(404, json.dumps({'error': '文件不存在: ' + path}))
        else:
            self._send(404, json.dumps({'error': 'not found'}))

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == '/identify':
            self._identify()
        elif u.path == '/start':
            self._start()
        else:
            self._send(400, json.dumps({'error': 'bad request'}))

    def _identify(self):
        ctype = self.headers.get('Content-Type', '')
        body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        try:
            boundary = ctype.split('boundary=')[1].strip('"').encode()
            fname, filedata = None, None
            # 按 boundary 切分, 找含 filename= 的部分, 取 header 之后到下一个 boundary 的全部字节
            for part in body.split(b'--' + boundary):
                hdr_end = part.find(b'\r\n\r\n')
                if hdr_end == -1 or b'filename=' not in part[:hdr_end]:
                    continue
                hdr = part[:hdr_end].decode('utf-8', errors='replace')
                m = re.search(r'filename="([^"]*)"', hdr)
                if m:
                    fname = m.group(1)
                    # 取 header 后直到末尾(去掉尾部的 \r\n--boundary-- 残段由 split 已排除)
                    filedata = part[hdr_end + 4:]
                    break
            if not filedata:
                self._send(400, json.dumps({'error': '未找到文件'})); return
            # 尾部可能残留 \r\n
            filedata = filedata.rstrip(b'\r\n') if filedata.endswith(b'\r\n') else filedata
            job = 'job' + str(int(time.time() * 1000))
            jd = os.path.join(JOBS, job); os.makedirs(jd, exist_ok=True)
            dst = os.path.join(jd, os.path.basename(fname))
            open(dst, 'wb').write(filedata)
            info = identify_book(dst)
            open(os.path.join(jd, 'info.json'), 'w', encoding='utf-8').write(
                json.dumps({'name': info['name'], 'format': info['format'],
                            'chapters': info['chapters'], 'file': os.path.basename(fname)},
                           ensure_ascii=False))
            self._send(200, json.dumps({'job': job, **info}))
        except Exception as e:
            self._send(500, json.dumps({'error': '识别失败: ' + str(e)[:120]}))

    def _start(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        job = q.get('job', [''])[0]; chapters = q.get('chapters', ['50'])[0]
        backend = q.get('backend', ['xiaohongshu'])[0]
        jd = os.path.join(JOBS, job)
        info = json.load(open(os.path.join(jd, 'info.json'), encoding='utf-8'))
        book = os.path.join(jd, info['file'])
        subprocess.Popen([PY, os.path.join(ROOT, '_demo_run.py'), jd, book,
                          str(chapters), backend], cwd=ROOT,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._send(200, json.dumps({'job': job, 'started': True}))


if __name__ == '__main__':
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    print('Demo 服务: http://localhost:%d' % port)
    http.server.HTTPServer(('0.0.0.0', port), Handler).serve_forever()