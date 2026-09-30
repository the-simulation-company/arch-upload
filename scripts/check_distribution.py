"""Fail if a release accidentally packages anything beyond the public helper."""

import tarfile
import zipfile
from pathlib import Path

wheel, = Path("dist").glob("*.whl")
assert wheel.stat().st_size < 100 * 1024, "Helper wheel exceeded 100 KiB"
with zipfile.ZipFile(wheel) as archive:
    for name in archive.namelist():
        assert name.startswith(("arch_upload/", "arch_upload-0.1.0.dist-info/")), name
        assert "__pycache__" not in name, name
sdist, = Path("dist").glob("*.tar.gz")
with tarfile.open(sdist) as archive:
    for name in archive.getnames():
        relative = name.split("/", 1)[1]
        assert relative.startswith("src/arch_upload/") or relative in {
            "pyproject.toml", "README.md", "NOTICE", "PKG-INFO", ".gitignore",
        }, name
print(f"Distribution contents verified; helper wheel {wheel.stat().st_size} bytes")
