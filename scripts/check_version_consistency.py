#!/usr/bin/env python3
"""Ensure every active product surface follows the canonical source version."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def match(pattern: str, text: str, label: str) -> str:
    found = re.search(pattern, text, re.MULTILINE)
    if not found:
        raise SystemExit(f"version consistency error: could not read {label}")
    return found.group(1)


def main() -> int:
    version_file = ROOT.joinpath("src/ksound_hub/_version.py").read_text(encoding="utf-8")
    canonical = match(r'^__version__\s*=\s*"([^"]+)"', version_file, "canonical version")

    manifest = ROOT.joinpath("android-soundboard-apk/AndroidManifest.xml").read_text(encoding="utf-8")
    android = match(r'android:versionName="([^"]+)"', manifest, "Android versionName")
    # Android Remote is independently versioned from the Linux application.

    pyproject = ROOT.joinpath("pyproject.toml").read_text(encoding="utf-8")
    config = ROOT.joinpath("src/ksound_hub/config.py").read_text(encoding="utf-8")
    readme = ROOT.joinpath("README.md").read_text(encoding="utf-8")

    errors: list[str] = []
    if not android.strip():
        errors.append("Android Remote versionName must not be empty")
    if 'version = {attr = "ksound_hub._version.__version__"}' not in pyproject:
        errors.append("pyproject does not use the canonical version attribute")
    if "APP_VERSION = __version__" not in config:
        errors.append("config.py does not use the canonical version")
    if f"Current public version: {canonical}" not in readme:
        errors.append("README development version is stale")

    if errors:
        raise SystemExit("version consistency error: " + "; ".join(errors))

    print(f"version consistency: {canonical} (Android {android})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
