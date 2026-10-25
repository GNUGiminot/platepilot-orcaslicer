"""Build the PlatePilot plugin wheel for OrcaSlicer.

Writes dist/platepilot-<version>-py3-none-any.whl and a copy named dist/platepilot_any.whl,
the name Orca Cloud expects (a target-OS suffix before the extension).
"""

import base64
import csv
import hashlib
import io
from pathlib import Path
import re
import shutil
import zipfile


ROOT = Path(__file__).resolve().parent
SOURCE = (ROOT / "platepilot.py").read_bytes()
VERSION = re.search(rb'^VERSION = "([^"]+)"', SOURCE, re.M).group(1).decode("ascii")
DIST_INFO = f"platepilot-{VERSION}.dist-info"
OUTPUT = ROOT / "dist" / f"platepilot-{VERSION}-py3-none-any.whl"
CLOUD_OUTPUT = ROOT / "dist" / "platepilot_any.whl"

files = {
    "platepilot/__init__.py": SOURCE,
    "platepilot/platepilot.svg": (ROOT / "platepilot.svg").read_bytes(),
    f"{DIST_INFO}/METADATA": (
        "Metadata-Version: 2.4\n"
        "Name: platepilot\n"
        f"Version: {VERSION}\n"
        "Summary: OrcaSlicer plugin: model library for STL bundles, multi-plate layout and one-click open in Prepare.\n"
        "Author: GNUGiminot\n"
        "License-Expression: MIT\n"
        "Project-URL: Homepage, https://github.com/GNUGiminot/platepilot-orcaslicer\n"
        "Requires-Python: >=3.9\n\n"
    ).encode("utf-8"),
    f"{DIST_INFO}/WHEEL": (
        "Wheel-Version: 1.0\n"
        "Generator: PlatePilot build_wheel.py\n"
        "Root-Is-Purelib: true\n"
        "Tag: py3-none-any\n\n"
    ).encode("utf-8"),
    f"{DIST_INFO}/top_level.txt": b"platepilot\n",
    f"{DIST_INFO}/licenses/LICENSE": (ROOT / "LICENSE").read_bytes(),
}

record = io.StringIO(newline="")
writer = csv.writer(record, lineterminator="\n")
for name, content in files.items():
    digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode("ascii")
    writer.writerow((name, "sha256=" + digest, len(content)))
writer.writerow((f"{DIST_INFO}/RECORD", "", ""))
files[f"{DIST_INFO}/RECORD"] = record.getvalue().encode("utf-8")

OUTPUT.parent.mkdir(exist_ok=True)
with zipfile.ZipFile(OUTPUT, "w", compression=zipfile.ZIP_DEFLATED) as wheel:
    for name, content in files.items():
        # Fixed timestamps make the wheel byte-for-byte reproducible.
        info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        wheel.writestr(info, content)
shutil.copyfile(OUTPUT, CLOUD_OUTPUT)

print(OUTPUT)
print(CLOUD_OUTPUT)
