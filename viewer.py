#!/usr/bin/env python3
"""Browse a picture folder with descriptions, live — no build step.

Usage:  python3 viewer.py [DIR] [--port N] [--no-browser] [--read-only]

Starts a small web server on this computer (127.0.0.1 only) and opens the
browser. Descriptions are read from the files every time a folder is opened,
so edits to captions show up on reload. Subfolders can be browsed too.
Descriptions can be edited in the full-size view (needs exiftool); every
edit is logged with the old text to ~/.local/share/kuva/muokkaukset.jsonl.
Thumbnails are cached in ~/.cache/kuva-thumbs.
"""
import argparse
import hashlib
import html
import io
import json
import secrets
import shutil
import subprocess
import sys
import threading
import webbrowser
import xml.etree.ElementTree as ET
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from PIL import Image, ImageOps

from gallery import describe, natkey  # same caption lookup as the static gallery

IMG_EXT = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"}
THUMB = 480
CACHE = Path.home() / ".cache" / "kuva-thumbs"
EDIT_LOG = Path.home() / ".local" / "share" / "kuva" / "muokkaukset.jsonl"
TOKEN = secrets.token_urlsafe(24)
READ_ONLY = False

ROOT: Path
_meta_cache: dict = {}
_lock = threading.Lock()


def safe(rel: str) -> Path | None:
    """Resolve a path relative to ROOT; refuse anything outside it."""
    p = (ROOT / rel).resolve()
    return p if p == ROOT or ROOT in p.parents else None


def meta(p: Path) -> tuple[str, str]:
    key = (p, p.stat().st_mtime_ns, _sidecar_mtime(p))
    with _lock:
        if key in _meta_cache:
            return _meta_cache[key]
    try:
        with Image.open(p) as im:
            val = describe(p, im)
    except Exception:
        val = ("", "")
    with _lock:
        _meta_cache[key] = val
    return val


def _sidecar_mtime(p: Path) -> int:
    s = p.parent / ".comments" / (p.name + ".xml")
    return s.stat().st_mtime_ns if s.exists() else 0


def children(d: Path) -> tuple[list[Path], list[Path]]:
    """(subfolders, pictures) directly in d, hidden ones left out."""
    subs, pics = [], []
    try:
        entries = sorted(d.iterdir(), key=lambda x: natkey(x.name))
    except OSError:
        return [], []
    for c in entries:
        if c.name.startswith("."):
            continue
        if c.is_dir():
            subs.append(c)
        elif c.suffix.lower() in IMG_EXT:
            pics.append(c)
    return subs, pics


def cover(d: Path, depth: int = 4) -> Path | None:
    """First picture in d, or else in its subfolders (a few levels down)."""
    subs, pics = children(d)
    if pics:
        return pics[0]
    if depth:
        for s in subs:
            c = cover(s, depth - 1)
            if c:
                return c
    return None


def listing(d: Path) -> dict:
    dirs, imgs = [], []
    for c in sorted(d.iterdir(), key=lambda x: natkey(x.name)):
        if c.name.startswith("."):
            continue
        if c.is_dir():
            subs, pics = children(c)
            first = cover(c)
            dirs.append({"name": c.name, "path": rel(c), "cover": rel(first) if first else None,
                         "pics": len(pics), "subs": len(subs)})
        elif c.suffix.lower() in IMG_EXT:
            desc, taken = meta(c)
            imgs.append({"name": c.name, "path": rel(c), "desc": desc, "date": taken[:10], "taken": taken})
    # by time of exposure; pictures without one last, in name order
    imgs.sort(key=lambda r: (not r["taken"], r["taken"], natkey(r["name"])))
    return {"path": rel(d), "root": ROOT.name, "dirs": dirs, "images": imgs}


def rel(p: Path) -> str:
    return p.relative_to(ROOT).as_posix() if p != ROOT else ""


def thumbnail(p: Path) -> bytes:
    st = p.stat()
    h = hashlib.sha1(f"{p}|{st.st_mtime_ns}|{THUMB}".encode()).hexdigest()
    cf = CACHE / h[:2] / (h + ".jpg")
    if cf.exists():
        return cf.read_bytes()
    with Image.open(p) as im:
        im.draft("RGB", (THUMB * 2, THUMB * 2))
        t = ImageOps.exif_transpose(im).convert("RGB")
        t.thumbnail((THUMB, THUMB))
        buf = io.BytesIO()
        t.save(buf, "JPEG", quality=82)
    data = buf.getvalue()
    cf.parent.mkdir(parents=True, exist_ok=True)
    cf.write_bytes(data)
    return data


def as_jpeg(p: Path, size: int = 3000) -> bytes:
    """Full-view JPEG of a TIFF (cached like thumbnails)."""
    st = p.stat()
    h = hashlib.sha1(f"{p}|{st.st_mtime_ns}|full{size}".encode()).hexdigest()
    cf = CACHE / h[:2] / (h + ".jpg")
    if not cf.exists():
        with Image.open(p) as im:
            t = ImageOps.exif_transpose(im)
            if t.mode not in ("RGB", "L"):
                t = t.convert("RGB")
            t.thumbnail((size, size))
            cf.parent.mkdir(parents=True, exist_ok=True)
            t.save(cf, "JPEG", quality=90)
    return cf.read_bytes()


PLACE = "📍 "


def save_description(p: Path, text: str) -> str:
    """Write TEXT as the picture's description; return the description as shown.

    The text goes into XMP dc:description and EXIF ImageDescription. Older
    copies of the description that would otherwise still be shown (EXIF
    UserComment, IPTC caption, caption/note in .comments/<file>.xml) are
    cleared; a place in the .comments file is kept. The file's modification
    time is preserved. Every change is logged to EDIT_LOG with the old text.
    """
    text = "\n".join(l.rstrip() for l in text.replace("\r", "").strip().split("\n"))
    old, _ = meta(p)
    v = html.escape(text, quote=False).replace("\n", "&#xa;")  # decoded by -E
    args = ["exiftool", "-charset", "utf8", "-charset", "filename=utf8", "-E", "-P", "-m", "-q",
            "-overwrite_original", f"-XMP-dc:Description={v}", f"-EXIF:ImageDescription={v}",
            "-EXIF:UserComment=", "-IPTC:Caption-Abstract=", str(p)]
    r = subprocess.run(args, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "exiftool epäonnistui")

    side = p.parent / ".comments" / (p.name + ".xml")
    if side.exists():
        try:
            tree = ET.parse(side)
            changed = False
            for tag in ("caption", "note"):
                e = tree.getroot().find(tag)
                if e is not None and (e.text or "").strip():
                    e.text = ""
                    changed = True
            if changed:
                tree.write(side, encoding="UTF-8", xml_declaration=True)
        except ET.ParseError:
            pass

    with _lock:  # mtime is preserved, so drop cached metadata explicitly
        for k in [k for k in _meta_cache if k[0] == p]:
            del _meta_cache[k]
    new, _ = meta(p)
    EDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(EDIT_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps({"time": datetime.now().isoformat(timespec="seconds"), "file": str(p),
                            "old": old, "new": new}, ensure_ascii=False) + "\n")
    return new


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # keep the terminal quiet
        pass

    def do_POST(self):
        # Only our own page may save: right Host (no DNS rebinding) and the
        # per-run token (other websites can't read it or send the header).
        if (self.headers.get("Host") != f"127.0.0.1:{self.server.server_port}"
                or self.headers.get("X-Kuva-Token") != TOKEN or READ_ONLY):
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        if urlparse(self.path).path != "/api/save":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length", 0)), 1 << 20)))
            p = safe(body["p"])
            if p is None or not p.is_file() or p.suffix.lower() not in IMG_EXT:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            desc = save_description(p, str(body["text"]))
            self.send(json.dumps({"desc": desc}, ensure_ascii=False).encode(), "application/json")
        except (ValueError, KeyError, TypeError):
            self.send_error(HTTPStatus.BAD_REQUEST)
        except RuntimeError as e:
            self.send_response(HTTPStatus.INTERNAL_SERVER_ERROR)
            msg = json.dumps({"error": str(e)}, ensure_ascii=False).encode()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)

    def send(self, body: bytes, ctype: str, cache: bool = False):
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=3600" if cache else "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        p = safe(q.get("p", [""])[0])
        try:
            if u.path == "/":
                page = PAGE.replace("__TOKEN__", "" if READ_ONLY else TOKEN)
                self.send(page.encode(), "text/html; charset=utf-8")
            elif p is None or not p.exists():
                self.send_error(HTTPStatus.NOT_FOUND)
            elif u.path == "/api/list" and p.is_dir():
                self.send(json.dumps(listing(p), ensure_ascii=False).encode(), "application/json")
            elif u.path == "/thumb" and p.is_file():
                self.send(thumbnail(p), "image/jpeg", cache=True)
            elif u.path == "/img" and p.is_file():
                if p.suffix.lower() in (".tif", ".tiff"):  # browsers can't show TIFF
                    self.send(as_jpeg(p), "image/jpeg", cache=True)
                else:
                    ctype = {".png": "image/png", ".webp": "image/webp"}.get(p.suffix.lower(), "image/jpeg")
                    self.send(p.read_bytes(), ctype, cache=True)
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
        except (BrokenPipeError, ConnectionResetError):
            pass


PAGE = r"""<!doctype html>
<html lang="fi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Kuvat</title>
<style>
:root{--bg:#f6f4f0;--fg:#1d1b18;--muted:#6b665e;--card:#fff;--line:#e2ddd4;--accent:#8a5a2b}
@media (prefers-color-scheme:dark){:root{--bg:#161513;--fg:#ece8e1;--muted:#9a948a;--card:#211f1c;--line:#34312c;--accent:#d9a066}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,sans-serif}
header{position:sticky;top:0;z-index:5;background:var(--bg);border-bottom:1px solid var(--line);
  padding:12px 20px;display:flex;flex-wrap:wrap;gap:12px;align-items:center}
#crumbs{font-size:20px;font-weight:600;margin-right:auto}
#crumbs a{color:var(--accent);text-decoration:none}
header input[type=search]{padding:6px 10px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--fg);min-width:200px}
header label{color:var(--muted);font-size:14px}
main{padding:20px;display:grid;gap:18px;grid-template-columns:repeat(auto-fill,minmax(230px,1fr))}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;overflow:hidden;cursor:pointer;
  display:flex;flex-direction:column;text-decoration:none;color:inherit}
.card img{width:100%;aspect-ratio:4/3;object-fit:contain;background:#0001;display:block}
.card .ph{aspect-ratio:4/3;display:grid;place-items:center;font-size:48px;background:#0001}
.card .cap{padding:8px 10px 10px;white-space:pre-line}
.card .d:empty{display:none}
.card .m{font-size:12px;color:var(--muted);margin-top:3px}
.card.dir .d{font-weight:600}
#msg{padding:20px;color:var(--muted)}
#lb{position:fixed;inset:0;background:#000e;display:none;flex-direction:column;z-index:10}
#lb.on{display:flex}
#lb img{flex:1;min-height:0;object-fit:contain;width:100%;padding:16px 60px 0}
#lb .cap{color:#eee;padding:12px 20px 16px;font-size:18px}
#lb .row{display:grid;grid-template-columns:minmax(6em,1fr) minmax(0,4fr) minmax(6em,1fr);gap:16px;align-items:center}
#lb .dt,#lb .fn{color:#aaa;font-size:13px;overflow-wrap:anywhere}
#lb .dt{text-align:left}#lb .fn{text-align:right}
#lb .mid{display:flex;flex-wrap:wrap;gap:6px 14px;align-items:center;justify-content:center;text-align:center;min-width:0}
#lb .mid .d{white-space:pre-line}
#lb .cap small{display:block;color:#aaa;font-size:13px;margin-top:6px;text-align:center}
#lb .cap button.edit{position:static;transform:none;font-size:14px;padding:4px 10px;border-radius:6px;
  border:1px solid #fff5;background:#ffffff14;color:#ddd;white-space:nowrap}
#lb .cap button.edit:hover{background:#ffffff2a;color:#fff}
@media (max-width:700px){#lb .row{grid-template-columns:1fr 1fr}#lb .mid{grid-column:1/-1;order:-1}}
#lb button{position:absolute;top:50%;transform:translateY(-50%);background:none;border:0;color:#fff;font-size:44px;cursor:pointer;padding:10px}
#lb .prev{left:4px}#lb .next{right:4px}
#lb .close{top:28px;right:8px;font-size:32px}
#lb .cap .tools{margin-top:10px;display:flex;gap:8px;justify-content:center;flex-wrap:wrap}
#lb .cap .tools button,#lb .cap .tools span{position:static;transform:none;font-size:15px;padding:6px 14px;
  border-radius:6px;border:1px solid #fff5;background:#ffffff14;color:#eee;white-space:normal}
#lb .cap .tools button:hover{background:#ffffff2a}
#lb .cap .tools .save{background:#d9a066;color:#1d1b18;border-color:#d9a066}
#lb .cap .tools span{border:0;background:none;color:#f99}
#lb textarea{width:min(760px,100%);min-height:5.5em;font:inherit;font-size:17px;line-height:1.4;padding:8px 10px;
  border-radius:6px;border:1px solid #fff6;background:#111;color:#eee;resize:vertical;display:block;margin:0 auto}
#lb .placehint{color:#aaa;font-size:14px;margin-top:6px}
</style></head><body>
<header><div id="crumbs"></div>
<input id="q" type="search" placeholder="Hae kuvatekstistä…">
<label><input id="only" type="checkbox"> vain kuvatekstilliset</label></header>
<div id="msg"></div><main id="grid"></main>
<div id="lb"><img alt=""><div class="cap"></div>
<button class="prev" aria-label="Edellinen">‹</button><button class="next" aria-label="Seuraava">›</button>
<button class="close" aria-label="Sulje">✕</button></div>
<script>
const $=s=>document.querySelector(s), grid=$('#grid'), lb=$('#lb');
const esc=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const enc=encodeURIComponent;
let data={dirs:[],images:[]}, shown=[], cur=0;

async function load(){
  const path=decodeURIComponent(location.hash.slice(1));
  $('#msg').textContent='Luetaan kuvatietoja…'; grid.innerHTML='';
  const r=await fetch('/api/list?p='+enc(path));
  if(!r.ok){$('#msg').textContent='Kansiota ei löydy.';return}
  data=await r.json();
  const parts=data.path?data.path.split('/'):[];
  $('#crumbs').innerHTML=`<a href="#">${esc(data.root)}</a>`+parts.map((p,i)=>
    ` / <a href="#${enc(parts.slice(0,i+1).join('/'))}">${esc(p)}</a>`).join('');
  document.title=parts.at(-1)||data.root;
  const n=data.images.length, d=data.images.filter(x=>x.desc).length;
  $('#msg').textContent=n?`${n} kuvaa, ${d} kuvatekstillä`:(data.dirs.length?'':'Ei kuvia.');
  render();
}
function dirInfo(x){const a=[];
  if(x.pics) a.push(`${x.pics} ${x.pics==1?'kuva':'kuvaa'}`);
  if(x.subs) a.push(`${x.subs} ${x.subs==1?'kansio':'kansiota'}`);
  return a.join(' · ')||'tyhjä';}
function render(){
  const q=$('#q').value.toLowerCase(), only=$('#only').checked;
  shown=data.images.filter(p=>(!q||(p.desc+' '+p.name).toLowerCase().includes(q))&&(!only||p.desc));
  const dirs=q?data.dirs.filter(x=>x.name.toLowerCase().includes(q)):data.dirs;
  grid.innerHTML=dirs.map(x=>`<a class="card dir" href="#${enc(x.path)}">${x.cover
      ?`<img loading="lazy" src="/thumb?p=${enc(x.cover)}" alt="">`:'<div class="ph">📁</div>'}
      <div class="cap"><div class="d">📁 ${esc(x.name)}</div><div class="m">${dirInfo(x)}</div></div></a>`).join('')
   +shown.map((p,i)=>`<div class="card" data-i="${i}"><img loading="lazy" src="/thumb?p=${enc(p.path)}" alt="${esc(p.desc)}">
      <div class="cap"><div class="d">${esc(p.desc)}</div><div class="m">${[p.date,p.name].filter(Boolean).map(esc).join(' · ')}</div></div></div>`).join('');
}
const TOKEN="__TOKEN__", PLACE="📍 ";
const cap=lb.querySelector('.cap');
let editing=false;
const placeLines=d=>d.split('\n').filter(l=>l.startsWith(PLACE));
const textLines=d=>d.split('\n').filter(l=>!l.startsWith(PLACE)).join('\n');

const row=(p,mid)=>`<div class="row"><span class="dt">${esc(p.date)}</span><div class="mid">${mid}</div><span class="fn">${esc(p.name)}</span></div>`;
function caption(){const p=shown[cur]; editing=false;
  cap.innerHTML=row(p,`<span class="d">${esc(p.desc)}</span>`
    +(TOKEN?'<button class="edit" title="Muokkaa kuvatekstiä">✎ Muokkaa</button>':''));
  cap.querySelector('.edit')?.addEventListener('click',edit);}
function show(i){cur=(i+shown.length)%shown.length;
  lb.querySelector('img').src='/img?p='+enc(shown[cur].path);
  caption(); lb.classList.add('on');}
function edit(){const p=shown[cur]; editing=true;
  const places=placeLines(p.desc);
  cap.innerHTML=row(p,`<div style="width:100%"><textarea aria-label="Kuvateksti">${esc(textLines(p.desc))}</textarea>`
    +(places.length?`<div class="placehint">${esc(places.join(', '))} (paikka säilyy)</div>`:'')
    +`<div class="tools"><button class="save">Tallenna</button><button class="cancel">Peru</button><span class="err"></span></div>`
    +`<small>Ctrl+Enter tallentaa, Esc peruu</small></div>`);
  const ta=cap.querySelector('textarea'); ta.focus(); ta.setSelectionRange(ta.value.length,ta.value.length);
  cap.querySelector('.save').onclick=save; cap.querySelector('.cancel').onclick=caption;}
async function save(){const p=shown[cur], btn=cap.querySelector('.save'), err=cap.querySelector('.err');
  btn.disabled=true; btn.textContent='Tallennetaan…'; err.textContent='';
  try{
    const r=await fetch('/api/save',{method:'POST',headers:{'Content-Type':'application/json','X-Kuva-Token':TOKEN},
      body:JSON.stringify({p:p.path,text:cap.querySelector('textarea').value})});
    const j=await r.json().catch(()=>({}));
    if(!r.ok) throw new Error(j.error||('virhe '+r.status));
    p.desc=j.desc; render();
    const n=data.images.length, d=data.images.filter(x=>x.desc).length;
    $('#msg').textContent=`${n} kuvaa, ${d} kuvatekstillä`;
    const idx=shown.indexOf(p);
    if(idx>=0){cur=idx; caption();}
    else {editing=false; cap.innerHTML=row(p,`<span class="d">${esc(p.desc)}</span><small>Tallennettu (ei näy nykyisellä haulla).</small>`);}
  }catch(e){btn.disabled=false; btn.textContent='Tallenna'; err.textContent='Tallennus epäonnistui: '+e.message;}
}
grid.addEventListener('click',e=>{const c=e.target.closest('.card[data-i]');if(c)show(+c.dataset.i)});
lb.querySelector('.prev').onclick=()=>show(cur-1);
lb.querySelector('.next').onclick=()=>show(cur+1);
lb.querySelector('.close').onclick=()=>lb.classList.remove('on');
document.addEventListener('keydown',e=>{if(!lb.classList.contains('on'))return;
  if(editing){
    if(e.key==='Enter'&&(e.ctrlKey||e.metaKey)){e.preventDefault();save();}
    else if(e.key==='Escape'){e.preventDefault();caption();}
    return;}
  if(e.key==='ArrowLeft')show(cur-1);else if(e.key==='ArrowRight')show(cur+1);else if(e.key==='Escape')lb.classList.remove('on');});
addEventListener('beforeunload',e=>{if(editing)e.preventDefault();});
$('#q').addEventListener('input',render); $('#only').addEventListener('change',render);
addEventListener('hashchange',()=>{$('#q').value='';lb.classList.remove('on');load();scrollTo(0,0)});
load();
</script></body></html>"""


def main():
    global ROOT, READ_ONLY
    ap = argparse.ArgumentParser(description="Näytä ja muokkaa kansion kuvien kuvatekstejä.")
    ap.add_argument("dir", nargs="?", default=".", help="kuvakansio (oletus: nykyinen)")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--read-only", action="store_true", help="vain katselu, ei muokkausta")
    a = ap.parse_args()
    ROOT = Path(a.dir).expanduser().resolve()
    if not ROOT.is_dir():
        sys.exit(f"Ei kansio: {ROOT}")
    READ_ONLY = a.read_only
    if not READ_ONLY and not shutil.which("exiftool"):
        print("exiftool puuttuu, joten muokkaus ei ole käytössä "
              "(sudo apt install libimage-exiftool-perl).", flush=True)
        READ_ONLY = True
    for port in range(a.port, a.port + 20):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            continue
    else:
        sys.exit("Vapaata porttia ei löytynyt.")
    url = f"http://127.0.0.1:{port}/"
    print(f"Näytetään {ROOT}\nAvaa {url}  (lopeta: Ctrl+C)"
          + ("" if READ_ONLY else f"\nMuokkaukset kirjataan: {EDIT_LOG}"), flush=True)
    if not a.no_browser:
        threading.Timer(0.5, webbrowser.open, [url]).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
