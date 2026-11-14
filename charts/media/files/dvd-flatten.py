#!/usr/bin/env python3
"""SABnzbd post-processing script: turn a DVD rip into one file Radarr can import.

Radarr imports a single video file, so from a VIDEO_TS folder (or an ISO of one)
it takes whichever VOB is largest, which is often a fraction of the film. This
extracts an ISO's VIDEO_TS, joins the VOBs of the largest title set into
<job>.vob and removes the disc structure. VOB parts are one MPEG program stream
split at 1 GB, so joining them byte for byte is lossless.

Jobs without a DVD structure are left untouched. SABnzbd passes the job folder
as argv[1], the clean job name as argv[3] and the post-processing status
(0 = success) as argv[7].

Run with --self-test to check it against a fake disc.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

VOB = re.compile(r"^VTS_(\d\d)_([1-9])\.VOB$", re.I)


def extract_isos(job_dir):
    seven = shutil.which("7zz") or shutil.which("7z")
    for root, _, files in os.walk(job_dir):
        for f in files:
            if not f.lower().endswith(".iso"):
                continue
            iso = os.path.join(root, f)
            if not seven:
                print(f"no 7-Zip to open {f}, left as is")
                return
            subprocess.run([seven, "x", "-y", f"-o{job_dir}", iso, "VIDEO_TS"],
                           check=True, stdout=subprocess.DEVNULL)
            os.remove(iso)


def flatten(job_dir, job_name):
    extract_isos(job_dir)
    sets = {}
    for root, _, files in os.walk(job_dir):
        for f in files:
            m = VOB.match(f)
            if m:
                sets.setdefault((root, m.group(1)), []).append((int(m.group(2)), os.path.join(root, f)))
    if not sets:
        return "no DVD structure"
    (_, vts), parts = max(sets.items(), key=lambda kv: sum(os.path.getsize(p) for _, p in kv[1]))
    name = re.sub(r'[\\/:*?"<>|]', " ", job_name).strip() or "movie"
    out = os.path.join(job_dir, f"{name}.vob")
    with open(out + ".part", "wb") as o:
        for _, p in sorted(parts):
            with open(p, "rb") as i:
                shutil.copyfileobj(i, o, 16 << 20)
    os.replace(out + ".part", out)
    for root, dirs, _ in os.walk(job_dir):
        for d in dirs:
            if d.upper() in ("VIDEO_TS", "AUDIO_TS"):
                shutil.rmtree(os.path.join(root, d), ignore_errors=True)
    return f"joined VTS_{vts} ({len(parts)} parts) into {os.path.basename(out)}"


def self_test():
    with tempfile.TemporaryDirectory() as d:
        vt = os.path.join(d, "Film DVD", "VIDEO_TS")
        os.makedirs(vt)
        for n, data in (("VIDEO_TS.VOB", b"menu"), ("VTS_01_1.VOB", b"bonus"),
                        ("VTS_02_0.VOB", b"m"), ("VTS_02_1.VOB", b"AAAA"), ("VTS_02_2.VOB", b"BB")):
            open(os.path.join(vt, n), "wb").write(data)
        job = os.path.join(d, "Film DVD")
        assert flatten(job, "Film 2008 DVDRip").startswith("joined VTS_02 (2 parts)")
        assert open(os.path.join(job, "Film 2008 DVDRip.vob"), "rb").read() == b"AAAABB"
        assert not os.path.exists(vt)
        plain = os.path.join(d, "Other")
        os.makedirs(plain)
        open(os.path.join(plain, "film.mkv"), "wb").write(b"x")
        assert flatten(plain, "Other") == "no DVD structure"
    print("self-test passed")


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        self_test()
        sys.exit(0)
    if len(sys.argv) < 8 or sys.argv[7] != "0":
        print("skipped: job did not succeed")
        sys.exit(0)
    print(flatten(sys.argv[1], sys.argv[3]))
