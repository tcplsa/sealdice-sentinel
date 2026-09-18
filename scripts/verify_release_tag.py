from __future__ import annotations

import os
import pathlib
import re
import sys


def main() -> int:
    tag = os.environ.get("RELEASE_TAG", "").removeprefix("v")
    pyproject = pathlib.Path("pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, flags=re.MULTILINE)
    if match is None:
        print("project version not found in pyproject.toml", file=sys.stderr)
        return 1
    version = match.group(1)
    if tag != version:
        print(f"release tag {tag!r} does not match project version {version!r}", file=sys.stderr)
        return 1
    print(f"release version verified: {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

