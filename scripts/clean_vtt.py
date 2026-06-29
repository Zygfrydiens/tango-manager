"""Turn YouTube auto-caption .vtt files into clean, de-duplicated plain text.

Auto-captions repeat each line as a rolling window and embed per-word <timestamp>
tags. This strips the tags and the rolling duplicates, leaving readable prose.

Usage:
    python scripts/clean_vtt.py                 # clean all data/transcripts/*.en.vtt
    python scripts/clean_vtt.py path/to/file.vtt
Outputs <name>.txt next to each .vtt.
"""
import re
import sys
from pathlib import Path

TAG = re.compile(r"<[^>]+>")          # <00:00:00.630> and <c>...</c>
TS_LINE = re.compile(r"^\d\d:\d\d:\d\d\.\d\d\d\s+-->")


def clean(vtt_path: Path) -> Path:
    lines = vtt_path.read_text(encoding="utf-8", errors="replace").splitlines()
    out = []
    for ln in lines:
        if ln.startswith("WEBVTT") or ln.startswith("Kind:") or ln.startswith("Language:"):
            continue
        if TS_LINE.match(ln):
            continue
        ln = TAG.sub("", ln).strip()
        if not ln:
            continue
        # drop consecutive duplicate (rolling-window) lines
        if out and out[-1] == ln:
            continue
        out.append(ln)
    # second pass: collapse the "previous line repeated then extended" pattern
    deduped = []
    for ln in out:
        if deduped and (ln == deduped[-1] or (deduped[-1] and ln.startswith(deduped[-1]))):
            deduped[-1] = ln
        else:
            deduped.append(ln)
    text = " ".join(deduped)
    text = re.sub(r"\s+", " ", text).strip()
    txt_path = vtt_path.with_suffix("").with_suffix(".txt")
    txt_path.write_text(text, encoding="utf-8")
    return txt_path


def main():
    args = sys.argv[1:]
    if args:
        paths = [Path(a) for a in args]
    else:
        base = Path(__file__).resolve().parent.parent / "data" / "transcripts"
        paths = sorted(base.glob("*.en.vtt"))
    for p in paths:
        out = clean(p)
        words = len(out.read_text(encoding="utf-8").split())
        print(f"{p.name} -> {out.name} ({words} words)")


if __name__ == "__main__":
    main()
