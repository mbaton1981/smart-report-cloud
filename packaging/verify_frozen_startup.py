"""Check the packaged Qt application with an isolated profile before release."""

import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory


def verify_frozen_startup(executable):
    with TemporaryDirectory(prefix="smartreport-frozen-startup-") as directory:
        root = Path(directory)
        local = root / "Local"
        roaming = root / "Roaming"
        for folder in (local, roaming):
            folder.mkdir()
        system = Path(os.environ["SystemRoot"])
        result_path = root / "result.json"
        environment = {
            "SystemRoot": str(system),
            "SystemDrive": system.drive,
            "USERPROFILE": str(root),
            "LOCALAPPDATA": str(local),
            "APPDATA": str(roaming),
            "TEMP": str(root),
            "TMP": str(root),
            "PATH": os.pathsep.join((str(system / "System32"), str(system))),
            "QT_QPA_PLATFORM": "offscreen",
            "SMARTREPORT_DATA_DIR": str(local),
            "SMARTREPORT_DB_FILE": str(local / "startup-test.db"),
            "SMARTREPORT_CLOUD_URL": "http://127.0.0.1:1",
        }
        completed = subprocess.run(
            [str(executable), "--diagnostic", "theme-ram",
             "--work", str(root), "--out", str(result_path)],
            cwd=root, env=environment, timeout=40, check=False,
        )
        if completed.returncode != 0 or not result_path.is_file():
            raise RuntimeError(f"Packaged app startup failed (exit code {completed.returncode})")
        result = json.loads(result_path.read_text(encoding="utf-8"))
        if not (result.get("frozen") is True and result.get("database_opened") is False
                and result.get("server_contacted") is False and result.get("field_unchanged") is True):
            raise RuntimeError("Packaged Qt startup did not produce the expected diagnostic result")
        print("Packaged Qt startup verified with an isolated profile")


if __name__ == "__main__":
    verify_frozen_startup(Path(sys.argv[1]).resolve(strict=True))
