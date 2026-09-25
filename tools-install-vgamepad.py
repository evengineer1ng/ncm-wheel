#!/usr/bin/env python3
"""Install vgamepad's package files without letting pip anywhere near it.

**vgamepad publishes an sdist and no wheel**, and its `setup.py` launches the ViGEmBus driver installer.
On a desktop that is a UAC prompt; on a headless CI runner it is a process waiting for input that never
arrives. Both `pip install` and `pip download` execute that setup code -- download does it to build metadata
-- so three builds hung for hours with no output and no release.

The package itself is pure Python plus a bundled `ViGEmClient.dll`, so none of that machinery is needed to
BUILD. Fetch the tarball, extract the package directory, copy it into site-packages. The driver is a
requirement on the player's machine, never on the builder's.
"""
import io
import json
import os
import shutil
import site
import sys
import tarfile
import tempfile
import urllib.request


def main():
    meta = json.load(urllib.request.urlopen("https://pypi.org/pypi/vgamepad/json", timeout=60))
    version = meta["info"]["version"]
    files = meta["releases"][version]
    sdist = next((f for f in files if f["packagetype"] == "sdist"), None)
    wheel = next((f for f in files if f["packagetype"] == "bdist_wheel"), None)
    if wheel:
        # If they ever publish one, prefer it: a wheel cannot execute code at install time.
        print("wheel published for %s; pip may be used safely" % version)
        return 0
    if not sdist:
        print("no sdist for vgamepad %s" % version, file=sys.stderr)
        return 1

    work = tempfile.mkdtemp(prefix="vgamepad-")
    archive = os.path.join(work, sdist["filename"])
    print("downloading %s (%d bytes)" % (sdist["filename"], sdist["size"]))
    urllib.request.urlretrieve(sdist["url"], archive)

    with tarfile.open(archive) as tf:
        # Guard against path traversal rather than trusting the archive. This runs in CI and on developer
        # machines, and an extract that can write outside its directory is not worth the convenience.
        members = [m for m in tf.getmembers()
                   if not (m.name.startswith("/") or ".." in m.name.split("/"))]
        # `filter="data"` is the safe extraction mode: no absolute paths, no links out of the tree, no
        # metadata surprises. It became the default in 3.14 and warns before then, so ask for it explicitly.
        try:
            tf.extractall(work, members=members, filter="data")
        except TypeError:
            tf.extractall(work, members=members)

    source = None
    for root, dirs, _ in os.walk(work):
        if "vgamepad" in dirs and os.path.exists(os.path.join(root, "vgamepad", "__init__.py")):
            source = os.path.join(root, "vgamepad")
            break
    if not source:
        print("vgamepad package directory not found inside the sdist", file=sys.stderr)
        return 1

    targets = site.getsitepackages()
    target = None
    for candidate in targets:
        if candidate.endswith("site-packages") and os.path.isdir(candidate):
            target = os.path.join(candidate, "vgamepad")
            break
    if target is None:
        target = os.path.join(targets[-1], "vgamepad")

    if os.path.isdir(target):
        shutil.rmtree(target)
    shutil.copytree(source, target)
    print("installed vgamepad %s -> %s" % (version, target))

    dll = os.path.join(target, "win", "vigem", "client", "x64", "ViGEmClient.dll")
    print("ViGEmClient.dll present:", os.path.exists(dll))
    return 0


if __name__ == "__main__":
    sys.exit(main())
