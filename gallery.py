#!/usr/bin/env python3
"""Build an HTML photo gallery that shows each picture with its description.

Descriptions are looked up, in order, from:
  1. XMP dc:description (darktable etc.)
  2. EXIF ImageDescription
  3. EXIF UserComment
  4. digiKam sidecar  <folder>/.comments/<file>.xml  <caption>

Usage:  python3 gallery.py [SOURCE_DIR] [OUTPUT_DIR]
Then open OUTPUT_DIR/index.html in a browser.
"""
import html
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from PIL import Image, ImageOps

SRC = Path(sys.argv[1] if len(sys.argv) > 1 else "~/Pictures/export").expanduser().resolve()
OUT = Path(sys.argv[2] if len(sys.argv) > 2 else Path(__file__).parent / "gallery").resolve()
THUMB = 480  # longest side of grid thumbnails, px

XMP_DESC_ATTR = re.compile(r'dc:description="([^"]*)"')
XMP_DESC_ELEM = re.compile(r"<dc:description>.*?<rdf:li[^>]*>(.*?)</rdf:li>", re.S)


def fix_mojibake(s: str) -> str:
    """Repair UTF-8 text that was decoded as Latin-1 (e.g. 'HyvÃ¶nen')."""
    if "Ã" in s or "Â" in s:
        try:
            return s.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    return s


def decode_user_comment(v) -> str:
    if isinstance(v, str):
        return fix_mojibake(v)
    head, body = v[:8], v[8:]
    if head.startswith(b"UNICODE"):
        for enc in ("utf-16-be", "utf-16-le"):
            try:
                return body.decode(enc)
            except UnicodeDecodeError:
                pass
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        return body.decode("latin-1")


def clean(s) -> str:
    if not s:
        return ""
    s = html.unescape(str(s)).replace("\x00", "").strip()
    return "" if s.lower() in ("charset=ascii", "default", "ascii", "user comments") else s


def embedded_description(im: Image.Image) -> str:
    """Description stored inside the picture file (XMP / EXIF)."""
    exif = im.getexif()
    sub = exif.get_ifd(0x8769)
    xmp = _xmp(im)
    m = XMP_DESC_ATTR.search(xmp) or XMP_DESC_ELEM.search(xmp)
    if m and clean(m.group(1)):
        return clean(m.group(1))
    d = clean(fix_mojibake(str(exif.get(270, ""))))
    if d:
        return d
    if 37510 in sub:
        return clean(decode_user_comment(sub[37510]))
    return ""


def sidecar(path: Path) -> dict:
    """caption / note / place from digiKam's .comments/<file>.xml."""
    side = path.parent / ".comments" / (path.name + ".xml")
    if not side.exists():
        return {}
    try:
        root = ET.parse(side).getroot()
    except ET.ParseError:
        return {}
    return {k: clean(root.findtext(k)) for k in ("caption", "note", "place")}


XMP_DATE = re.compile(r'(?:exif:DateTimeOriginal|photoshop:DateCreated)'
                      r'(?:="|>)\s*(\d{4}[-:]\d\d[-:]\d\d(?:[T ]\d\d:\d\d(?::\d\d(?:\.\d+)?)?)?)')
_FI = str.maketrans({"å": "z\u0001", "ä": "z\u0002", "ö": "z\u0003", "æ": "z\u0002", "ø": "z\u0003",
                     "ü": "y", "é": "e", "è": "e", "á": "a", "à": "a"})


def natkey(name: str):
    """Natural, case-insensitive sort key with Finnish letter order:
    'kuva2' < 'kuva10', and å ä ö after z."""
    s = name.lower().translate(_FI)
    return [(0, int(t), "") if t.isdigit() else (1, 0, t) for t in re.split(r"(\d+)", s) if t]


def _xmp(im: Image.Image) -> str:
    """The picture's XMP packet as text (JPEG, PNG or TIFF), '' if none."""
    x = im.info.get("xmp") or im.info.get("XML:com.adobe.xmp") or b""
    if not x and hasattr(im, "tag_v2"):
        x = im.tag_v2.get(700) or b""
    if isinstance(x, (tuple, list)):
        x = bytes(x)
    return x.decode("utf-8", "replace") if isinstance(x, bytes) else str(x)


def taken_date(sub, im: Image.Image) -> str:
    """Time the picture was taken (exposed) as 'YYYY-MM-DD HH:MM:SS[.ff]',
    possibly only 'YYYY-MM-DD'; '' if unknown. File-change and
    create/digitized dates are deliberately not used: for scanned film they
    are the scanning or editing time, not the time of exposure."""
    v = str(sub.get(36867) or "").strip().replace("\x00", "")  # DateTimeOriginal
    if re.match(r"\d{4}[:-]\d\d[:-]\d\d", v) and not v.startswith("0000"):
        d = v[:10].replace(":", "-") + v[10:19]
        sub_s = str(sub.get(37521) or "").strip().replace("\x00", "")  # SubSecTimeOriginal
        if len(d) == 19 and sub_s.isdigit():
            d += "." + sub_s
        return d
    xmp = _xmp(im)
    m = XMP_DATE.search(xmp)
    if not m:
        return ""
    v = m.group(1)
    return v[:10].replace(":", "-") + v[10:].replace("T", " ")


def describe(path: Path, im: Image.Image) -> tuple[str, str]:
    """Return (description, date). The description may have several lines:
    .comments caption, embedded description, .comments note, .comments place —
    duplicates left out."""
    exif = im.getexif()
    sub = exif.get_ifd(0x8769)
    date = taken_date(sub, im)

    sc = sidecar(path)
    lines, seen = [], set()
    for text, prefix in ((sc.get("caption"), ""), (embedded_description(im), ""),
                         (sc.get("note"), ""), (sc.get("place"), "📍 ")):
        key = " ".join((text or "").lower().split())
        if key and key not in seen:
            seen.add(key)
            lines.append((key, prefix + text))
    # leave out a line whose text is contained in a longer one
    keep = [t for k, t in lines if not any(k != k2 and k in k2 for k2, _ in lines)]
    return "\n".join(keep), date


# Google Photos keeps an edited copy next to the original ("IMG_1-muokattu.JPG")
# and shows only the edited one; so do we.
EDITED_SUFFIXES = ("-muokattu", "-edited", "-redigerad", "-bearbeitet", "-modifié", "-editado", "-modificato")


def has_edited_copy(p: Path) -> bool:
    return any(p.with_name(p.stem + s + p.suffix).exists() for s in EDITED_SUFFIXES)


FOLDER_FILE = "kansio.txt"
# kansio.txt: line 1 = the folder's name to show, further lines = description,
# and an optional line "Kansikuva: <picture file>" choosing the cover picture.
COVER_PREFIX = "kansikuva:"


def folder_info(d: Path) -> tuple[str, str, str]:
    """(display name, description, cover file name) from kansio.txt; '' for missing parts."""
    try:
        text = (d / FOLDER_FILE).read_text("utf-8", errors="replace").replace("\r", "")
    except OSError:
        return "", "", ""
    lines, cover = [], ""
    for l in text.rstrip().split("\n"):
        if l.strip().lower().startswith(COVER_PREFIX):
            cover = l.strip()[len(COVER_PREFIX):].strip()
        else:
            lines.append(l)
    if not lines:
        return "", "", cover
    return lines[0].strip(), "\n".join(lines[1:]).strip(), cover


def write_folder_info(d: Path, title: str, about: str, cover: str) -> None:
    """Write kansio.txt; remove it when everything is empty."""
    f = d / FOLDER_FILE
    if title or about or cover:
        f.write_text(title + "\n" + (about + "\n" if about else "")
                     + (f"Kansikuva: {cover}\n" if cover else ""), "utf-8")
    elif f.exists():
        f.unlink()


def chosen_cover(d: Path) -> Path | None:
    """The cover picture named in d/kansio.txt, if it exists inside d."""
    name = folder_info(d)[2]
    if not name:
        return None
    p = (d / name).resolve()
    return p if p.is_file() and d.resolve() in p.parents else None


def process(path: Path) -> dict:
    rel = path.relative_to(SRC)
    thumb = OUT / "thumbs" / rel
    with Image.open(path) as im:
        desc, date = describe(path, im)
        if not thumb.exists() or thumb.stat().st_mtime < path.stat().st_mtime:
            thumb.parent.mkdir(parents=True, exist_ok=True)
            im.draft("RGB", (THUMB * 2, THUMB * 2))  # fast JPEG downscale
            t = ImageOps.exif_transpose(im).convert("RGB")
            t.thumbnail((THUMB, THUMB))
            t.save(thumb, "JPEG", quality=82)
    return {
        "name": path.name,
        "thumb": os.path.relpath(thumb, OUT),
        "full": path.as_uri(),
        "desc": desc,
        "date": date[:19],
        "taken": date,
    }


PAGE = """<!doctype html>
<html lang="fi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>
:root{--bg:#f6f4f0;--fg:#1d1b18;--muted:#6b665e;--card:#fff;--line:#e2ddd4;--accent:#8a5a2b}
@media (prefers-color-scheme:dark){:root{--bg:#161513;--fg:#ece8e1;--muted:#9a948a;--card:#211f1c;--line:#34312c;--accent:#d9a066}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,sans-serif}
header{position:sticky;top:0;z-index:5;background:var(--bg);border-bottom:1px solid var(--line);
  padding:12px 20px;display:flex;flex-wrap:wrap;gap:12px;align-items:center}
header h1{font-size:20px;margin:0 auto 0 0}
header a{color:var(--accent);text-decoration:none}
header input{padding:6px 10px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--fg);min-width:200px}
header label{color:var(--muted);font-size:14px}
main{padding:20px;display:grid;gap:18px;grid-template-columns:repeat(auto-fill,minmax(230px,1fr))}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;overflow:hidden;cursor:pointer;
  display:flex;flex-direction:column;text-decoration:none;color:inherit}
.card img{width:100%;aspect-ratio:4/3;object-fit:contain;background:#0001;display:block}
.card .cap{padding:8px 10px 10px;white-space:pre-line}
.card .d{font-size:15px}
.card .m{font-size:12px;color:var(--muted);margin-top:3px}
.card .d:empty{display:none}
.intro{padding:16px 20px 0;max-width:900px;white-space:pre-line;color:var(--muted)}
.intro:empty{display:none}
header h1 small{font-weight:400;color:var(--muted);font-size:14px;margin-left:8px}
#lb{position:fixed;inset:0;background:#000e;display:none;flex-direction:column;z-index:10}
#lb.on{display:flex}
#lb img{flex:1;min-height:0;object-fit:contain;width:100%;padding:16px 60px 0}
#lb .cap{color:#eee;text-align:center;padding:14px 20px 20px;font-size:18px;white-space:pre-line}
#lb .cap small{display:block;color:#aaa;font-size:13px;margin-top:4px}
#lb button{position:absolute;top:50%;transform:translateY(-50%);background:none;border:0;color:#fff;font-size:44px;cursor:pointer;padding:10px}
#lb .prev{left:4px}#lb .next{right:4px}
#lb .close{top:28px;right:8px;font-size:32px}
</style></head><body>
<header><h1>__HEADING__</h1>__CONTROLS__</header>
<div class="intro">__INTRO__</div><main id="grid"></main>
<div id="lb"><img alt=""><div class="cap"></div>
<button class="prev" aria-label="Edellinen">‹</button><button class="next" aria-label="Seuraava">›</button>
<button class="close" aria-label="Sulje">✕</button></div>
<script>
const DATA=__DATA__, MODE="__MODE__";
const grid=document.getElementById('grid'), lb=document.getElementById('lb');
const esc=s=>s.replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
let shown=[], cur=0;
function render(){
  const q=(document.getElementById('q')?.value||'').toLowerCase();
  const only=document.getElementById('only')?.checked;
  shown=DATA.filter(p=>(!q||(p.desc+' '+p.name+' '+(p.folder||'')).toLowerCase().includes(q))&&(!only||p.desc));
  grid.innerHTML=shown.map((p,i)=>MODE==='index'
    ? `<a class="card" href="${esc(p.href)}"><img loading="lazy" src="${esc(p.thumb)}" alt=""><div class="cap"><div class="d">${esc(p.title||p.folder)}</div><div class="m">${p.title?esc(p.folder)+' · ':''}${p.count} kuvaa · ${p.described} kuvatekstiä</div></div></a>`
    : `<div class="card" data-i="${i}"><img loading="lazy" src="${esc(p.thumb)}" alt="${esc(p.desc)}"><div class="cap"><div class="d">${esc(p.desc)}</div><div class="m">${[p.date,p.name].filter(Boolean).map(esc).join(' · ')}</div></div></div>`).join('');
}
function show(i){cur=(i+shown.length)%shown.length;const p=shown[cur];
  lb.querySelector('img').src=p.full;
  lb.querySelector('.cap').innerHTML=esc(p.desc||'')+`<small>${[p.date,p.name].filter(Boolean).map(esc).join(' · ')}</small>`;
  lb.classList.add('on');}
grid.addEventListener('click',e=>{const c=e.target.closest('.card[data-i]');if(c)show(+c.dataset.i)});
lb.querySelector('.prev').onclick=()=>show(cur-1);
lb.querySelector('.next').onclick=()=>show(cur+1);
lb.querySelector('.close').onclick=()=>lb.classList.remove('on');
document.addEventListener('keydown',e=>{if(!lb.classList.contains('on'))return;
  if(e.key==='ArrowLeft')show(cur-1);else if(e.key==='ArrowRight')show(cur+1);else if(e.key==='Escape')lb.classList.remove('on');});
document.querySelectorAll('header input').forEach(el=>el.addEventListener('input',render));
render();
</script></body></html>"""


def page(title, heading, controls, data, mode, intro=""):
    return (PAGE.replace("__TITLE__", html.escape(title))
                .replace("__INTRO__", html.escape(intro))
                .replace("__HEADING__", heading)
                .replace("__CONTROLS__", controls)
                .replace("__MODE__", mode)
                .replace("__DATA__", json.dumps(data, ensure_ascii=False).replace("</", "<\\/")))


def main():
    files = sorted(p for p in SRC.rglob("*")
                   if p.suffix.lower() in (".jpg", ".jpeg")
                   and not any(part.startswith(".") for part in p.relative_to(SRC).parts)
                   and not has_edited_copy(p))
    print(f"{len(files)} kuvaa kansiossa {SRC}")
    OUT.mkdir(parents=True, exist_ok=True)
    with ProcessPoolExecutor() as ex:
        results = list(ex.map(process, files, chunksize=8))

    folders: dict[str, list] = {}
    for f, r in zip(files, results):
        folders.setdefault(str(f.parent.relative_to(SRC)), []).append(r)

    search = '<input id="q" type="search" placeholder="Hae kuvatekstistä…">'
    index = []
    for folder, items in sorted(folders.items(), key=lambda kv: natkey(kv[0])):
        items.sort(key=lambda r: (not r["taken"], r["taken"], natkey(r["name"])))  # undated last
        fname = "album-" + re.sub(r"[^\w.-]+", "_", folder) + ".html"
        controls = (search + '<label><input id="only" type="checkbox"> vain kuvatekstilliset</label>'
                    '<a href="index.html">← Kaikki albumit</a>')
        title, about, _ = folder_info(SRC / folder)
        chosen = chosen_cover(SRC / folder)
        heading = (f"{html.escape(title)}<small>{html.escape(folder)}</small>" if title else html.escape(folder))
        (OUT / fname).write_text(page(title or folder, heading, controls, items, "album", about), "utf-8")
        cover = (next((r for r in items if chosen and r["name"] == chosen.name), None)
                 or next((r for r in items if r["desc"]), items[0]))
        index.append({"folder": folder, "title": title, "href": fname, "thumb": cover["thumb"], "count": len(items),
                      "described": sum(1 for r in items if r["desc"]), "desc": " ".join([title, about] + [r["desc"] for r in items]),
                      "name": ""})

    (OUT / "index.html").write_text(page("Kuva-albumit", "Kuva-albumit", search + '<a href="kuvatekstit.html">Kaikki kuvatekstit →</a>', index, "index"), "utf-8")
    # A flat page of every captioned picture, searchable
    allc = [dict(r, folder=k) for k, v in sorted(folders.items()) for r in v if r["desc"]]
    (OUT / "kuvatekstit.html").write_text(
        page("Kaikki kuvatekstit", "Kaikki kuvatekstit", search + '<a href="index.html">← Albumit</a>', allc, "album"), "utf-8")

    n = sum(1 for r in results if r["desc"])
    print(f"{n}/{len(results)} kuvalla on kuvateksti. Avaa: {OUT / 'index.html'}")


if __name__ == "__main__":
    main()
