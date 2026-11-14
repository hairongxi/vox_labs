"""VoxHUD launcher. Adds the local ``_vendor`` fallback used in this workspace."""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
vendor = ROOT / "_vendor"
if vendor.is_dir():
    sys.path.insert(0, str(vendor))


def _redirect_stdio_if_needed() -> None:
    """Windowed/hidden-console builds may not have stdout/stderr."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        if os.environ.get("VOXHUD_DATA_DIR"):
            base = Path(os.environ["VOXHUD_DATA_DIR"])
        else:
            base = Path(os.environ.get("APPDATA") or Path.home()) / "VoxHUD"
        base.mkdir(parents=True, exist_ok=True)
        log_path = base / "voxhud-crash.log"
        stream = open(log_path, "a", encoding="utf-8", buffering=1)
        if sys.stdout is None:
            sys.stdout = stream
        if sys.stderr is None:
            sys.stderr = stream
    except Exception:
        pass


def _main() -> int:
    _redirect_stdio_if_needed()
    from voxhud.app import main

    return main()


if __name__ == "__main__":
    try:
        raise SystemExit(_main())
    except SystemExit:
        raise
    except BaseException:
        traceback.print_exc()
        try:
            if os.environ.get("VOXHUD_DATA_DIR"):
                base = Path(os.environ["VOXHUD_DATA_DIR"])
            else:
                base = Path(os.environ.get("APPDATA") or Path.home()) / "VoxHUD"
            base.mkdir(parents=True, exist_ok=True)
            with open(base / "voxhud-crash.log", "a", encoding="utf-8") as fh:
                fh.write("\n--- VoxHUD crash ---\n")
                traceback.print_exc(file=fh)
        except Exception:
            pass
        raise
