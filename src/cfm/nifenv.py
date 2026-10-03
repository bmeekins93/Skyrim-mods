"""Import PyNifly's bpy-free `pyn` layer (NIF read/write via NiflyDLL).

Run tools/nifly/build_niflydll.py first (Linux/macOS), or place the official
NiflyDLL.dll in third_party/PyNifly/io_scene_nifly/ (Windows).
"""
import sys

from .paths import PYNIFLY

_ADDON = PYNIFLY / "io_scene_nifly"
if not (_ADDON / "NiflyDLL.dll").exists():
    raise RuntimeError(f"NiflyDLL not found in {_ADDON}; run tools/nifly/build_niflydll.py")
if str(_ADDON) not in sys.path:
    sys.path.insert(0, str(_ADDON))

from pyn import pynifly  # noqa: E402
from pyn.pynifly import NifFile  # noqa: E402,F401
from pyn import nifdefs  # noqa: E402,F401
