#!/usr/bin/env python3
"""Copy each picture's description into EXIF XPComment (Windows "Comments").

Usage:  python3 fill_xpcomment.py DIR [DIR ...] [--dry-run]

The description is the one the viewer shows (same lookup as gallery.py),
without the "📍 place" lines. Pictures without a description, or whose
XPComment already has the same text, are left alone. Nothing else in the
file is changed; the file's modification time is preserved. Every change is
logged to ~/.local/share/kuva/xpcomment.jsonl with the old XPComment.
"""
import html
import json
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from PIL import Image

sys.argv, _args = sys.argv[:1], sys.argv[1:]  # gallery.py reads sys.argv on import
from gallery import describe  # noqa: E402

IMG_EXT = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"}
PLACE = "📍 "
LOG = Path.home() / ".local/share/kuva/xpcomment.jsonl"


def pictures(dirs):
    for d in dirs:
        for p in sorted(Path(d).expanduser().rglob("*")):
            if (p.suffix.lower() in IMG_EXT and p.is_file()
                    and not any(part.startswith(".") for part in p.relative_to(d).parts)):
                yield p


def current_xpcomments(files) -> dict:
    """Existing XPComment of each file (one exiftool run)."""
    out = {}
    with tempfile.NamedTemporaryFile("w", suffix=".args", encoding="utf-8") as f:
        f.write("\n".join(str(p) for p in files))
        f.flush()
        r = subprocess.run(["exiftool", "-charset", "filename=utf8", "-j", "-q", "-m",
                            "-XPComment", "-@", f.name], capture_output=True, text=True)
    for row in json.loads(r.stdout or "[]"):
        out[row["SourceFile"]] = str(row.get("XPComment", ""))
    return out


def main():
    dry = "--dry-run" in _args
    dirs = [Path(a).expanduser() for a in _args if a != "--dry-run"]
    if not dirs:
        sys.exit(__doc__)
    todo, skipped_none, same = [], 0, 0
    files = list(pictures(dirs))
    print(f"{len(files)} kuvaa, luetaan kuvatekstit…", flush=True)
    for p in files:
        try:
            with Image.open(p) as im:
                desc, _ = describe(p, im)
        except Exception as e:
            print(f"  ohitetaan {p}: {e}")
            continue
        text = "\n".join(l for l in desc.split("\n") if not l.startswith(PLACE)).strip()
        if text:
            todo.append((p, text))
        else:
            skipped_none += 1
    old = current_xpcomments([p for p, _ in todo])
    work = []
    for p, text in todo:
        o = old.get(str(p), "")
        if o.replace("\r\n", "\n") == text:
            same += 1
        else:
            work.append((p, text, o))
    print(f"Kirjoitetaan {len(work)} kuvaan; kuvatekstittömiä {skipped_none}, "
          f"jo valmiina {same}.", flush=True)
    if dry or not work:
        for p, text, _ in work[:10]:
            print(f"  {p}: {text[:70]!r}")
        return

    with tempfile.NamedTemporaryFile("w", suffix=".args", encoding="utf-8") as f:
        for p, text, _ in work:
            v = html.escape(text, quote=False).replace("\n", "&#xa;")  # decoded by -E
            # options must be repeated in every -execute section of an arg file
            f.write(f"-E\n-P\n-m\n-overwrite_original\n-XPComment={v}\n{p}\n-execute\n")
        f.flush()
        r = subprocess.run(["exiftool", "-q", "-q", "-@", f.name],
                           capture_output=True, text=True)
    if r.stderr.strip():
        print(r.stderr.strip())
    after = current_xpcomments([p for p, _, _ in work])
    ok = 0
    LOG.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now().isoformat(timespec="seconds")
    with open(LOG, "a", encoding="utf-8") as log:
        for p, text, o in work:
            good = after.get(str(p), "").replace("\r\n", "\n") == text
            ok += good
            if not good:
                print(f"  EI ONNISTUNUT: {p}")
            log.write(json.dumps({"time": now, "file": str(p), "old": o, "new": text, "ok": good},
                                 ensure_ascii=False) + "\n")
    print(f"Valmis: {ok}/{len(work)} kuvaan kirjoitettu XPComment. Loki: {LOG}")


if __name__ == "__main__":
    main()
