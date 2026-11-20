# PyInstaller: сборка настольного клиента SmartReport (папка, без Python на целевом ПК)
import os
import ssl
from PyInstaller.utils.hooks import collect_all

root = os.path.abspath(os.path.join(SPECPATH, ".."))
datas = [(os.path.join(root, "ob_rules.json"), "."),
         (os.path.join(root, "assets"), "assets")]
# The Codex Python SSLContext reads this trust-policy file next to ssl.py.
tls_rejected_cas = os.path.join(os.path.dirname(ssl.__file__), "codex-rejected-cas.pem")
if os.path.isfile(tls_rejected_cas):
    datas.append((tls_rejected_cas, "."))
manifest = os.environ.get("SMARTREPORT_BUILD_MANIFEST")
if manifest:
    datas.append((manifest, "."))
binaries = []
hiddenimports = []
for package in ("rapidocr_onnxruntime", "onnxruntime", "tzdata"):
    package_datas, package_binaries, package_hidden = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hidden
icon = os.path.join(root, "Logo.ico")
if os.path.exists(icon):
    datas.append((icon, "."))
version_info = os.environ.get("SMARTREPORT_VERSION_INFO", os.path.join(SPECPATH, "version_info.txt"))

a = Analysis(
    [os.path.join(root, "app.py")],
    pathex=[root],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "flask", "gunicorn", "pytest"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True, name="SmartReport",
    console=False, icon=icon if os.path.exists(icon) else None,
    version=version_info,
)
coll = COLLECT(exe, a.binaries, a.datas, name="SmartReport", strip=False, upx=False)
