from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(prog="drivesync")
    parser.add_argument("--changes", required=True, help="Path to mock changes JSON")
    args = parser.parse_args()

    doc = json.loads(Path(args.changes).read_text(encoding="utf-8"))
    changes = doc.get("changes", [])
    print(f"Loaded {len(changes)} change(s)")
    for c in changes:
        print(f"- {c.get('fileId')}  {c.get('name')}  ({c.get('mimeType')})")

    raise SystemExit(0)
