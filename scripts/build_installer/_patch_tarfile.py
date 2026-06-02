"""Apply _win_longpath patch to a tarfile.py to bypass Windows MAX_PATH."""
import pathlib
import sys

PATCH_DEF = r'''
def _win_longpath(p):
    """Return a \\?\-prefixed path on Windows to bypass MAX_PATH (260-char) limit."""
    if sys.platform != "win32":
        return p
    p = str(p).replace("/", "\\")
    if not p.startswith("\\\\?\\"):
        p = "\\\\?\\" + p
    return p

'''

STAT_OLD = (
    "        # Use os.stat or os.lstat, depending on if symlinks shall be resolved.\n"
    "        if fileobj is None:\n"
    "            if not self.dereference:\n"
    "                statres = os.lstat(name)\n"
    "            else:\n"
    "                statres = os.stat(name)\n"
)
STAT_NEW = (
    "        # Use os.stat or os.lstat, depending on if symlinks shall be resolved.\n"
    "        # _win_longpath: bypass MAX_PATH on Windows.\n"
    "        if fileobj is None:\n"
    "            if not self.dereference:\n"
    "                statres = os.lstat(_win_longpath(name))\n"
    "            else:\n"
    "                statres = os.stat(_win_longpath(name))\n"
)

OPEN_OLD = '            with bltn_open(name, "rb") as f:\n'
OPEN_NEW = '            with bltn_open(_win_longpath(name), "rb") as f:\n'

ANCHOR = "import re\nimport warnings\n"

for target in sys.argv[1:]:
    path = pathlib.Path(target)
    src = path.read_text(encoding="utf-8")
    if "_win_longpath" in src:
        print(f"already patched: {path}")
        continue
    src = src.replace(ANCHOR, ANCHOR + PATCH_DEF, 1)
    src = src.replace(STAT_OLD, STAT_NEW, 1)
    src = src.replace(OPEN_OLD, OPEN_NEW, 1)
    path.write_text(src, encoding="utf-8")
    print(f"patched: {path}")
