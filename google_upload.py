#!/usr/bin/env python3
"""Upload a picture folder to Google Photos with the descriptions filled in.

Usage:  python3 google_upload.py DIR [--album NAME] [--dry-run]

Google Photos puts descriptions that are inside the files under "Other";
only its Library API can set the real description. This script uploads
the pictures of DIR into a new album (named after line 1 of DIR/kansio.txt,
or the folder name) in time-taken order, with each picture's description
(the same text the viewer shows, without 📍 place lines). The uploaded copy
leaves out the in-file description fields Google would otherwise show under
"Other" instead; the files on disk are not changed.

One-time setup (see the instructions printed when it is missing):
  ~/.config/kuva/google-client.json  — OAuth client of type "Desktop app"
On the first run a browser opens to sign in; the permission asked for only
allows adding photos (photoslibrary.appendonly), not reading or deleting.

Pictures already uploaded are remembered in
~/.local/share/kuva/google-upload.json and skipped on later runs, so the
script can be re-run after adding pictures or after an interruption.
"""
import argparse
import base64
import hashlib
import http.server
import json
import os
import secrets
import shutil
import subprocess
import tempfile
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

from PIL import Image

sys.argv, _argv = sys.argv[:1], sys.argv[1:]  # gallery.py reads sys.argv on import
from gallery import describe, folder_info, has_edited_copy, natkey  # noqa: E402

CONF = Path.home() / ".config/kuva"
CLIENT = CONF / "google-client.json"
TOKEN = CONF / "google-token.json"
STATE = Path.home() / ".local/share/kuva/google-upload.json"
SCOPE = "https://www.googleapis.com/auth/photoslibrary.appendonly"
API = "https://photoslibrary.googleapis.com/v1"
IMG_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".tif", ".tiff", ".bmp"}
PLACE = "📍 "

SETUP = f"""Google-yhteyden asetukset puuttuvat ({CLIENT}).

Tee kerran:
 1. Avaa https://console.cloud.google.com/ ja luo uusi projekti (esim. "kuva").
 2. APIs & Services → Library → hae "Photos Library API" → Enable.
 3. APIs & Services → OAuth consent screen (Google Auth Platform):
      User type: External; sovelluksen nimi esim. "kuva"; oma sähköpostisi.
      Audience → Test users → lisää oma Google-tilisi.
 4. APIs & Services → Credentials → Create credentials → OAuth client ID
      → Application type: Desktop app → Create → Download JSON.
 5. Tallenna ladattu tiedosto nimellä {CLIENT}
Aja sitten tämä komento uudelleen."""


# ---------- OAuth (installed-app flow with PKCE, loopback redirect) ----------

def _post(url, data, token=None):
    body = json.dumps(data).encode() if not isinstance(data, bytes) else data
    hdr = {"Content-Type": "application/json"}
    if token:
        hdr["Authorization"] = f"Bearer {token}"
    return urllib.request.Request(url, body, hdr)


def _form(url, fields):
    return urllib.request.urlopen(urllib.request.Request(
        url, urllib.parse.urlencode(fields).encode(),
        {"Content-Type": "application/x-www-form-urlencoded"}), timeout=60)


def access_token() -> str:
    c = json.loads(CLIENT.read_text())
    c = c.get("installed") or c.get("web") or c
    if TOKEN.exists():
        t = json.loads(TOKEN.read_text())
        if t.get("expires", 0) > time.time() + 60:
            return t["access_token"]
        if t.get("refresh_token"):
            try:
                r = json.load(_form(c["token_uri"], {
                    "client_id": c["client_id"], "client_secret": c["client_secret"],
                    "refresh_token": t["refresh_token"], "grant_type": "refresh_token"}))
                t.update(access_token=r["access_token"], expires=time.time() + r["expires_in"])
                _save_token(t)
                return t["access_token"]
            except urllib.error.HTTPError:
                pass  # refresh token revoked/expired: sign in again
    return _sign_in(c)


def _save_token(t):
    CONF.mkdir(parents=True, exist_ok=True)
    TOKEN.write_text(json.dumps(t))
    os.chmod(TOKEN, 0o600)


def _sign_in(c) -> str:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    got = {}

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if q.get("state", [""])[0] == state:
                got.update({k: v[0] for k, v in q.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("<p>Valmis. Voit sulkea tämän välilehden.</p>".encode())

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    redirect = f"http://127.0.0.1:{srv.server_port}"
    url = c["auth_uri"] + "?" + urllib.parse.urlencode({
        "client_id": c["client_id"], "redirect_uri": redirect, "response_type": "code",
        "scope": SCOPE, "state": state, "code_challenge": challenge,
        "code_challenge_method": "S256", "access_type": "offline", "prompt": "consent"})
    print("Kirjaudu Google-tilillesi selaimessa. Jos selain ei aukea, avaa:\n" + url, flush=True)
    webbrowser.open(url)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    while "code" not in got and "error" not in got:
        time.sleep(0.2)
    srv.shutdown()
    if "error" in got:
        sys.exit(f"Kirjautuminen peruttiin: {got['error']}")
    r = json.load(_form(c["token_uri"], {
        "client_id": c["client_id"], "client_secret": c["client_secret"], "code": got["code"],
        "code_verifier": verifier, "redirect_uri": redirect, "grant_type": "authorization_code"}))
    t = {"access_token": r["access_token"], "expires": time.time() + r["expires_in"],
         "refresh_token": r.get("refresh_token")}
    _save_token(t)
    return t["access_token"]


# ---------- Photos Library API ----------

def api(path, data):
    for attempt in range(5):
        try:
            with urllib.request.urlopen(_post(f"{API}/{path}", data, access_token()), timeout=120) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 503) and attempt < 4:
                time.sleep(2 ** attempt * 5)
                continue
            raise RuntimeError(f"{path}: {e.code} {e.read().decode(errors='replace')[:500]}") from None


def upload_data(p: Path) -> bytes:
    """The file's bytes without the description fields Google Photos reads
    (XMP dc:description, IPTC caption). If they are present Google shows
    them under "Other" and leaves the real description empty. The original
    file is not changed: exiftool writes a temporary copy."""
    if shutil.which("exiftool") and p.suffix.lower() in (".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / ("x" + p.suffix)
            r = subprocess.run(["exiftool", "-q", "-q", "-m", "-o", str(out), "-XMP-dc:Description=",
                                "-IPTC:Caption-Abstract=", str(p)], capture_output=True)
            if r.returncode == 0 and out.exists():
                return out.read_bytes()
    return p.read_bytes()


def upload_bytes(p: Path) -> str:
    data = upload_data(p)
    for attempt in range(5):
        req = urllib.request.Request(f"{API}/uploads", data, {
            "Authorization": f"Bearer {access_token()}",
            "Content-Type": "application/octet-stream",
            "X-Goog-Upload-Content-Type": {".png": "image/png", ".gif": "image/gif", ".webp": "image/webp",
                                           ".tif": "image/tiff", ".tiff": "image/tiff",
                                           ".heic": "image/heic", ".bmp": "image/bmp"}
                                          .get(p.suffix.lower(), "image/jpeg"),
            "X-Goog-Upload-Protocol": "raw"})
        try:
            with urllib.request.urlopen(req, timeout=600) as r:
                return r.read().decode()
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 503) and attempt < 4:
                time.sleep(2 ** attempt * 5)
                continue
            raise RuntimeError(f"{p.name}: {e.code} {e.read().decode(errors='replace')[:300]}") from None


# ---------- main ----------

def pictures(d: Path):
    out = []
    for p in d.iterdir():
        if p.is_file() and not p.name.startswith(".") and p.suffix.lower() in IMG_EXT \
                and not has_edited_copy(p):
            try:
                with Image.open(p) as im:
                    desc, taken = describe(p, im)
            except Exception:
                desc, taken = "", ""
            text = "\n".join(l for l in desc.split("\n") if not l.startswith(PLACE)).strip()
            out.append((p, text[:1000], taken))  # API limit: 1000 characters
    out.sort(key=lambda r: (not r[2], r[2], natkey(r[0].name)))
    return out


def main():
    ap = argparse.ArgumentParser(description="Lähetä kansion kuvat Google Kuviin kuvateksteineen.")
    ap.add_argument("dir")
    ap.add_argument("--album", help="albumin nimi (oletus: kansio.txt:n 1. rivi tai kansion nimi)")
    ap.add_argument("--dry-run", action="store_true", help="näytä mitä tehtäisiin, älä lähetä")
    a = ap.parse_args(_argv)
    d = Path(a.dir).expanduser().resolve()
    if not d.is_dir():
        sys.exit(f"Ei kansio: {d}")
    title, about, _ = folder_info(d)
    album_name = a.album or title or d.name

    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    st = state.setdefault(str(d), {"album": None, "uploaded": {}})
    pics = [r for r in pictures(d) if r[0].name not in st["uploaded"]]
    print(f"Albumi: {album_name}\n{len(pics)} kuvaa lähetettävänä "
          f"({len(st['uploaded'])} lähetetty jo aiemmin).", flush=True)
    if a.dry_run or not pics:
        for p, text, taken in pics:
            print(f"  {p.name}  {taken[:19] or '-'}  {text[:60]!r}")
        return
    if not CLIENT.exists():
        sys.exit(SETUP)

    def save():
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1))

    if not st["album"]:
        alb = api("albums", {"album": {"title": album_name[:500]}})
        st["album"] = alb["id"]
        save()
        print(f"Luotiin albumi: {alb.get('productUrl', album_name)}", flush=True)
        if about:
            api(f"albums/{alb['id']}:addEnrichment",
                {"newEnrichmentItem": {"textEnrichment": {"text": about[:1000]}},
                 "albumPosition": {"position": "FIRST_IN_ALBUM"}})

    done = failed = 0
    for i in range(0, len(pics), 50):  # batchCreate takes at most 50 items
        batch = pics[i:i + 50]
        items = []
        for p, text, _ in batch:
            print(f"  lähetetään {p.name}", flush=True)
            items.append({"description": text, "simpleMediaItem": {"uploadToken": upload_bytes(p),
                                                                   "fileName": p.name}})
        r = api("mediaItems:batchCreate", {"albumId": st["album"], "newMediaItems": items})
        for (p, _, _), res in zip(batch, r.get("newMediaItemResults", [])):
            m = res.get("mediaItem")
            if m:
                st["uploaded"][p.name] = m["id"]
                done += 1
            else:
                failed += 1
                print(f"  EI ONNISTUNUT {p.name}: {res.get('status')}")
        save()
    print(f"Valmis: {done} kuvaa lähetetty" + (f", {failed} epäonnistui" if failed else "") + ".")


if __name__ == "__main__":
    main()
