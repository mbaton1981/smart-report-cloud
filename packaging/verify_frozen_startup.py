"""Check the packaged TLS runtime and Qt application with an isolated profile."""

import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory


def run_diagnostic(executable, action, root, environment):
    result_path = root / (action + ".json")
    completed = subprocess.run(
        [str(executable), "--diagnostic", action, "--work", str(root), "--out", str(result_path)],
        cwd=root, env=environment, timeout=40, check=False,
    )
    if completed.returncode != 0 or not result_path.is_file():
        raise RuntimeError(f"Packaged {action} failed (exit code {completed.returncode})")
    result = json.loads(result_path.read_text(encoding="utf-8"))
    if result.get("frozen") is not True:
        raise RuntimeError(f"Packaged {action} did not run in the frozen application")
    return result


def verify_frozen_startup(executable):
    with TemporaryDirectory(prefix="smartreport-frozen-startup-") as directory:
        root = Path(directory)
        local = root / "Local"
        roaming = root / "Roaming"
        for folder in (local, roaming):
            folder.mkdir()
        system = Path(os.environ["SystemRoot"])
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
        tls = run_diagnostic(executable, "tls-ram", root, environment)
        if tls.get("certificate_verification") is not True:
            raise RuntimeError("Packaged TLS runtime did not enable certificate and hostname verification")
        result = run_diagnostic(executable, "theme-ram", root, environment)
        if not (result.get("database_opened") is False
                and result.get("server_contacted") is False and result.get("field_unchanged") is True):
            raise RuntimeError("Packaged Qt startup did not produce the expected diagnostic result")
        print("Packaged TLS and Qt startup verified with an isolated profile")


if __name__ == "__main__":
    verify_frozen_startup(Path(sys.argv[1]).resolve(strict=True))
