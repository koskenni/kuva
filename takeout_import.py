#!/usr/bin/env python3
"""Import Google Photos albums from Google Takeout, keeping the descriptions.

Reads the Takeout .zip files directly (no need to unpack them first), copies
the pictures of each album into OUT_DIR/<album>/ and writes the description
you wrote in Google Photos, plus shared-album comments as "Name: text",
into each copy (XMP dc:description and
EXIF ImageDescription — the fields viewer.py and gallery.py read).
Also fills in the date taken and GPS position when the file lacks them.

Usage:
    python3 takeout_import.py TAKEOUT_DIR OUT_DIR [--all] [--dry-run]

TAKEOUT_DIR  folder holding takeout-*.zip files (or an already unpacked Takeout)
--all        also import the "Photos from YYYY" folders, not only albums
--dry-run    only report what would be done; nothing is written

Needs exiftool:  sudo apt install libimage-exiftool-perl
"""
import argparse
import html
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath

MEDIA = {".jpg", ".jpeg", ".png", ".bmp", ".heic", ".heif", ".gif", ".webp", ".tif", ".tiff",
         ".mp4", ".mov", ".m4v", ".3gp", ".avi", ".mkv", ".dng", ".cr2", ".nef", ".arw"}
# Suffixes Google adds to edited copies, in various UI languages
EDITED = re.compile(r"-(edited|muokattu|redigerad|bearbeitet|modifié|editado|modificato)$", re.I)
DUP = re.compile(r"^(.*)\((\d+)\)$")
# Takeout's per-year folders (not albums), in various UI languages
YEAR_DIR = re.compile(r"^(Photos from|Valokuvat vuodelta|Kuvat vuodelta|Fotos von|Foton från|Photos de|Fotos de) \d{4}$", re.I)


class Source:
    """All files in the Takeout, whether in several zips or unpacked."""

    def __init__(self, root: Path):
        self.files: dict[str, tuple] = {}  # posix path inside Takeout -> (zip or None, name)
        self.zips = []
        zips = sorted(root.glob("*.zip")) + sorted(root.glob("*.tgz"))
        if any(z.suffix == ".tgz" for z in zips):
            sys.exit("Valitse Takeoutissa .zip-muoto (.tgz ei ole tuettu).")
        for z in zips:
            zf = zipfile.ZipFile(z)
            self.zips.append(zf)
            for n in zf.namelist():
                if not n.endswith("/"):
                    self.files[n] = (zf, n)
        if not zips:
            for p in root.rglob("*"):
                if p.is_file():
                    self.files[p.relative_to(root).as_posix()] = (None, p)

    def read(self, key: str) -> bytes:
        zf, n = self.files[key]
        return zf.read(n) if zf else Path(n).read_bytes()

    def copy_to(self, key: str, dest: Path):
        zf, n = self.files[key]
        dest.parent.mkdir(parents=True, exist_ok=True)
        if zf:
            with zf.open(n) as s, open(dest, "wb") as d:
                shutil.copyfileobj(s, d, 1 << 20)
        else:
            shutil.copy2(n, dest)


def load_json(src: Source, key: str):
    try:
        return json.loads(src.read(key).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None


def match_key(name: str) -> tuple[str, int]:
    """Media file name -> (original title, duplicate number).
    'IMG_1(1).jpg' -> ('IMG_1.jpg', 1);  'IMG_1-edited.jpg' -> ('IMG_1.jpg', 0)"""
    p = PurePosixPath(name)
    stem, n = p.stem, 0
    m = DUP.match(stem)
    if m:
        stem, n = m.group(1), int(m.group(2))
    stem = EDITED.sub("", stem)
    return stem + p.suffix, n


FILE_UNSAFE = re.compile(r'[?*:"<>|\\/]')


def json_dup_number(json_name: str) -> int:
    m = re.search(r"\((\d+)\)\.json$", json_name)
    return int(m.group(1)) if m else 0


def find_meta(media_name: str, metas: dict[tuple[str, int], dict]) -> dict | None:
    title, n = match_key(media_name)
    if (title, n) in metas:
        return metas[(title, n)]
    # Takeout replaces characters not allowed in file names (e.g. '?') with '_'
    low = {(FILE_UNSAFE.sub("_", t).lower(), k): v for (t, k), v in metas.items()}
    if (title.lower(), n) in low:
        return low[(title.lower(), n)]
    # Takeout truncates long names: match on the longest common start
    stem = PurePosixPath(title).stem.lower()
    cands = [v for (t, k), v in low.items()
             if k == n and len(stem) >= 20 and (t.startswith(stem[:40]) or stem.startswith(PurePosixPath(t).stem[:40]))]
    return cands[0] if len(cands) == 1 else None


def full_text(meta: dict) -> str:
    """Description, then shared-album comments as 'Name: text', oldest first."""
    lines = [(meta.get("description") or "").strip()]
    comments = sorted((c for c in meta.get("sharedAlbumComments") or [] if (c.get("text") or "").strip()),
                      key=lambda c: int((c.get("creationTime") or {}).get("timestamp") or 0))
    for c in comments:
        who = (c.get("contentOwnerName") or "").strip()
        text = c["text"].strip()
        lines.append(f"{who}: {text}" if who else text)
    return "\n".join(l for l in lines if l)


def name_year(*names: str) -> int | None:
    """First plausible year (1850-2099) at the start of a word in the names."""
    for n in names:
        m = re.search(r"(?<!\d)(18[5-9]\d|19\d\d|20\d\d)(?!\d)", n or "")
        if m:
            return int(m.group(1))
    return None


def exif_args(meta: dict, has_date: bool, has_gps: bool, hint_year: int | None = None) -> list[str]:
    a = []
    desc = full_text(meta)
    if desc:
        v = html.escape(desc, quote=False).replace("\r", "").replace("\n", "&#xa;")  # read back by -E
        a += [f"-XMP-dc:Description={v}", f"-EXIF:ImageDescription={v}",
              f"-EXIF:XPComment={v}"]
    ts = (meta.get("photoTakenTime") or {}).get("timestamp")
    taken = datetime.fromtimestamp(int(ts)) if ts else None
    # A scanned old photo often has the scan/upload time as Google's "date taken":
    # skip it when the file or album name says the picture is much older.
    if taken and hint_year and taken.year > hint_year + 1:
        taken = None
    if taken and not has_date:
        d = taken.strftime("%Y:%m:%d %H:%M:%S")
        a += [f"-DateTimeOriginal={d}", f"-CreateDate={d}"]
    geo = meta.get("geoDataExif") or meta.get("geoData") or {}
    lat, lon = geo.get("latitude", 0), geo.get("longitude", 0)
    if (lat or lon) and not has_gps:
        a += [f"-GPSLatitude={abs(lat)}", f"-GPSLatitudeRef={'N' if lat >= 0 else 'S'}",
              f"-GPSLongitude={abs(lon)}", f"-GPSLongitudeRef={'E' if lon >= 0 else 'W'}"]
    return a


def safe_name(s: str) -> str:
    return re.sub(r'[/\\:*?"<>|\x00-\x1f]', "_", s).strip(" .") or "_"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("takeout", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--all", action="store_true", help='tuo myös "Photos from YYYY" -kansiot')
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if not a.dry_run and not shutil.which("exiftool"):
        sys.exit("exiftool puuttuu: sudo apt install libimage-exiftool-perl")

    src = Source(a.takeout.expanduser())
    if not src.files:
        sys.exit(f"Ei tiedostoja: {a.takeout}")

    # Group by folder: the folder holding the media (album or year folder)
    folders: dict[str, dict] = {}
    for key in src.files:
        p = PurePosixPath(key)
        f = folders.setdefault(str(p.parent), {"media": [], "json": [], "album": None})
        if p.name == "metadata.json" or re.fullmatch(r"metadata(\(\d+\))?\.json", p.name):
            f["album"] = load_json(src, key)
        elif p.suffix.lower() == ".json":
            f["json"].append(key)
        elif p.suffix.lower() in MEDIA:
            f["media"].append(key)

    out = a.out.expanduser()
    stats = {"albums": 0, "media": 0, "described": 0, "no_json": 0, "skipped_dirs": 0, "exists": 0, "converted": 0}
    unmatched, jobs = [], []  # jobs: (dest, meta)
    used_names: set[Path] = set()

    for folder, f in sorted(folders.items()):
        if not f["media"]:
            continue
        is_album = f["album"] is not None and not YEAR_DIR.match(PurePosixPath(folder).name)
        if not is_album and not a.all:
            stats["skipped_dirs"] += 1
            continue
        title = (f["album"] or {}).get("title") or PurePosixPath(folder).name
        album_dir = out / safe_name(title)
        while album_dir in used_names:          # two albums with the same name
            album_dir = album_dir.with_name(album_dir.name + "_")
        used_names.add(album_dir)
        stats["albums"] += 1
        if a.dry_run:
            print(f"  {title}  ({len(f['media'])} tiedostoa)")

        metas = {}
        for jk in f["json"]:
            m = load_json(src, jk)
            if isinstance(m, dict) and m.get("title"):
                metas[(m["title"], json_dup_number(PurePosixPath(jk).name))] = m

        album_desc = ((f["album"] or {}).get("description") or "").strip()
        if album_desc and not a.dry_run:
            album_dir.mkdir(parents=True, exist_ok=True)
            (album_dir / "albumin-kuvaus.txt").write_text(album_desc + "\n", "utf-8")

        for mk in sorted(f["media"]):
            name = PurePosixPath(mk).name
            meta = find_meta(name, metas)
            dest = album_dir / name
            convert = dest.suffix.lower() == ".bmp"   # BMP can't hold a description
            if convert:
                dest = dest.with_suffix(".png")
            stats["media"] += 1
            if meta is None:
                stats["no_json"] += 1
                unmatched.append(mk)
            elif full_text(meta):
                stats["described"] += 1
            if a.dry_run:
                continue
            if dest.exists():
                stats["exists"] += 1
                continue
            if convert:
                from PIL import Image
                dest.parent.mkdir(parents=True, exist_ok=True)
                with Image.open(io.BytesIO(src.read(mk))) as im:
                    im.save(dest, "PNG")  # lossless
                stats["converted"] += 1
            else:
                src.copy_to(mk, dest)
            if meta:
                jobs.append((dest, meta, name_year(name, title)))

    if not a.dry_run and jobs:
        write_metadata(jobs)

    print(f"Albumeja {stats['albums']}, tiedostoja {stats['media']}, "
          f"joista kuvaus {stats['described']}.")
    if stats["skipped_dirs"]:
        print(f"Ohitettiin {stats['skipped_dirs']} kansiota, jotka eivät ole albumeja "
              f"(esim. 'Photos from 2019'); ota mukaan valitsimella --all.")
    if stats["converted"]:
        print(f"{stats['converted']} BMP-kuvaa muunnettiin PNG:ksi (BMP ei voi sisältää kuvausta).")
    if stats["exists"]:
        print(f"{stats['exists']} tiedostoa oli jo kohteessa, niitä ei kopioitu uudelleen.")
    if unmatched:
        print(f"{len(unmatched)} tiedostolle ei löytynyt .json-tietoja, esim.:")
        for u in unmatched[:10]:
            print("   ", u)
    print("Valmis (kokeiluajo, mitään ei kirjoitettu)." if a.dry_run else f"Valmis: {out}")


def write_metadata(jobs):
    """Write descriptions/dates/GPS with one exiftool run."""
    # What do the copies already have? (don't overwrite camera date / GPS)
    have = {}
    paths = [str(d) for d, _, _ in jobs]
    for i in range(0, len(paths), 500):
        r = subprocess.run(["exiftool", "-j", "-q", "-DateTimeOriginal", "-GPSLatitude", *paths[i:i + 500]],
                           capture_output=True, text=True)
        for e in json.loads(r.stdout or "[]"):
            have[e["SourceFile"]] = e
    with tempfile.NamedTemporaryFile("w", suffix=".args", delete=False, encoding="utf-8") as af:
        n = 0
        for dest, meta, hint in jobs:
            e = have.get(str(dest), {})
            args = exif_args(meta, "DateTimeOriginal" in e, "GPSLatitude" in e, hint)
            if not args:
                continue
            af.write("\n".join(["-overwrite_original", "-m", "-E", *args, str(dest), "-execute"]) + "\n")
            n += 1
    if n:
        print(f"Kirjoitetaan tiedot {n} tiedostoon…")
        r = subprocess.run(["exiftool", "-charset", "utf8", "-charset", "filename=utf8", "-q",
                            "-@", af.name], capture_output=True, text=True)
        errs = [l for l in r.stderr.splitlines() if l.strip()]
        if errs:
            print(f"exiftool ilmoitti {len(errs)} virhettä, esim.:")
            for l in errs[:10]:
                print("   ", l)
    Path(af.name).unlink()


if __name__ == "__main__":
    main()
