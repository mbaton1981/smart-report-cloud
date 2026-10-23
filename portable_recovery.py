"""Password-protected recovery bundles; no Windows profile or DPAPI required."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import getpass
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import secrets
import sqlite3
import stat
import sys
import tempfile
import zipfile

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

MAGIC = b"SRBACK01"
MAX_BYTES = 512 * 1024 * 1024


class RecoveryError(ValueError):
    pass


def derive(password, salt):
    if len(password) < 16:
        raise RecoveryError("Use a recovery password of at least 16 characters.")
    return Scrypt(salt=salt, length=32, n=2**17, r=8, p=1).derive(password.encode("utf-8"))


def encrypt(files, password):
    if "manifest.json" in files:
        raise RecoveryError("Reserved manifest filename.")
    manifest = {
        "format": 1, "created_at": datetime.now(timezone.utc).isoformat(),
        "files": {name: hashlib.sha256(content).hexdigest() for name, content in files.items()},
    }
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            safe_name(name)
            archive.writestr(name, content)
        archive.writestr("manifest.json", json.dumps(manifest).encode())
    if len(stream.getvalue()) > MAX_BYTES or sum(map(len, files.values())) > MAX_BYTES:
        raise RecoveryError("Bundle exceeds the 512 MiB limit; nothing has been published.")
    salt, nonce = secrets.token_bytes(16), secrets.token_bytes(12)
    header = MAGIC + salt + nonce
    return header + AESGCM(derive(password, salt)).encrypt(nonce, stream.getvalue(), header)


def safe_name(name):
    path = PurePosixPath(name)
    if (not name or "\\" in name or ":" in name or path.is_absolute()
            or any(part in {"", ".", ".."} for part in name.split("/"))):
        raise RecoveryError("Unsafe archive path.")
    return path


def decrypt(payload, password):
    if len(payload) > MAX_BYTES + 52 or len(payload) < 52 or payload[:8] != MAGIC:
        raise RecoveryError("Unsupported or incomplete recovery bundle.")
    header, salt, nonce = payload[:36], payload[8:24], payload[24:36]
    try:
        plain = AESGCM(derive(password, salt)).decrypt(nonce, payload[36:], header)
    except InvalidTag:
        raise RecoveryError("Incorrect password or damaged bundle; no files restored.") from None
    try:
        with zipfile.ZipFile(io.BytesIO(plain)) as archive:
            infos = archive.infolist()
            if sum(info.file_size for info in infos) > MAX_BYTES:
                raise RecoveryError("Expanded bundle exceeds the safety limit.")
            if len({info.filename for info in infos}) != len(infos):
                raise RecoveryError("Duplicate archive entries.")
            for info in infos:
                safe_name(info.filename)
                if stat.S_ISLNK(info.external_attr >> 16):
                    raise RecoveryError("Archive links are not permitted.")
            files = {info.filename: archive.read(info) for info in infos}
        manifest = json.loads(files.pop("manifest.json"))
        if manifest["format"] != 1 or manifest["files"] != {
            name: hashlib.sha256(content).hexdigest() for name, content in files.items()
        }:
            raise RecoveryError("Recovery manifest mismatch.")
        return files
    except (zipfile.BadZipFile, KeyError, json.JSONDecodeError) as error:
        raise RecoveryError("Invalid recovery archive.") from error


def publish(folder, files, password):
    """Never replace or prune earlier copies, including when a new backup fails."""
    encrypted = encrypt(files, password)
    if decrypt(encrypted, password) != files:
        raise RecoveryError("Encrypted backup verification failed.")
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = folder / f"SmartReport-{stamp}-{secrets.token_hex(6)}.srbackup"
    try:
        with path.open("xb") as output:
            output.write(encrypted)
            output.flush()
            os.fsync(output.fileno())
    except OSError:
        # An incomplete new file is not a usable version; old versions are untouched.
        raise RecoveryError("Cannot finish writing the new backup. Check storage and retry.") from None
    if decrypt(path.read_bytes(), password) != files:
        raise RecoveryError("Stored backup verification failed; retain earlier versions.")
    return path


def restore(files, destination):
    destination = Path(destination)
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    for name, content in files.items():
        target = destination.joinpath(*safe_name(name).parts)
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with target.open("xb") as output:
            if os.name != "nt":
                os.chmod(target, 0o600)
            output.write(content)


def table_hashes(conn):
    return {
        name: hashlib.sha256(repr(sorted(map(tuple, conn.execute(
            'SELECT * FROM "' + name.replace('"', '""') + '"')), key=repr)).encode()).hexdigest()
        for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name != 'sqlite_sequence'"
        )
    }


def verify_server(directory):
    """Boot the bundled compatible server against the restored copy, never production."""
    directory = Path(directory).resolve()
    database = directory / "backup" / "server_snapshot.db"
    config = json.loads((directory / "recovery" / "configuration.json").read_text())
    key = (directory / "recovery" / "employee-card.key").read_text().strip()
    source = directory / "source"
    sys.path.insert(0, str(source))
    os.environ.update(
        DATABASE_PATH=str(database), SECRET_KEY=config["SECRET_KEY"],
        SYNC_API_KEY=config["SYNC_API_KEY"], ADMIN_PASSWORD=config["ADMIN_PASSWORD"],
        SMARTREPORT_EMPLOYEE_CARD_KEY=key,
        SMARTREPORT_DOCUMENTS_DIR=str(directory / "backup" / "documents"),
        USE_SECURE_COOKIES="true",
    )
    if config.get("ADMIN_USERNAME"):
        os.environ["ADMIN_USERNAME"] = config["ADMIN_USERNAME"]
    os.chdir(directory)
    import socket

    def no_network(*args, **kwargs):
        raise RecoveryError("Network access is forbidden during isolated recovery.")

    socket.socket.connect = no_network
    socket.socket.connect_ex = no_network
    socket.create_connection = no_network
    with closing(sqlite3.connect(database)) as connection:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RecoveryError("Restored database integrity check failed.")
        before = table_hashes(connection)
    import server
    import employee_cards
    from cryptography.fernet import Fernet
    with closing(server.get_db_connection()) as connection:
        if table_hashes(connection) != before:
            raise RecoveryError("Server initialization changed restored tables.")
        rows = connection.execute(
            "SELECT id,card_cipher FROM meta_employees WHERE card_cipher IS NOT NULL AND card_cipher != ''"
        ).fetchall()
        if not rows:
            raise RecoveryError("No encrypted cards available to verify.")
        for row in rows:
            employee_cards.decrypt_card(row["card_cipher"], row["id"])
        os.environ["SMARTREPORT_EMPLOYEE_CARD_KEY"] = Fernet.generate_key().decode()
        try:
            employee_cards.decrypt_card(rows[0]["card_cipher"], rows[0]["id"])
        except employee_cards.CardError:
            pass
        else:
            raise RecoveryError("Wrong card key unexpectedly accepted.")
        os.environ["SMARTREPORT_EMPLOYEE_CARD_KEY"] = key
        # Check the protected HTTP reader without issuing tokens or writing the database.
        client = server.app.test_client()
        import employee_cards_api
        # Register only the existing card routes in an isolated authenticated test app.
        from flask import Flask
        test_app = Flask("isolated_card_reader")
        employee_cards_api.register_employee_cards(
            test_app, server.get_db_connection, lambda: {"id": 0, "role": "admin"}, None,
        )
        path = f"/api/employee-cards/{rows[0]['id']}"
        from sync_dataset import HEADER, file_dataset
        headers = {HEADER: file_dataset(database)}
        if client.get(path, base_url="https://isolated.invalid", headers=headers).status_code != 403:
            raise RecoveryError("Unauthenticated production reader was not denied.")
        response = test_app.test_client().get(path, base_url="https://isolated.invalid")
        if response.status_code != 200:
            raise RecoveryError("Restored card reader failed.")
        if table_hashes(connection) != before:
            raise RecoveryError("Recovery verification changed database contents.")
    return {
        "database_integrity": True, "all_business_table_hashes_unchanged": True,
        "encrypted_cards_read": len(rows), "wrong_key_denied": True,
        "unauthenticated_http_denied": True, "authenticated_http_read": True,
        "dpapi_used": "desktop_auth" in sys.modules, "production_accessed": False,
        "network_disabled_in_verifier": True, "python_prefix": sys.prefix,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--password-stdin", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    password = (sys.stdin.buffer.readline().decode("utf-8").rstrip("\r\n")
                if args.password_stdin else getpass.getpass("Recovery password (not stored): "))
    try:
        files = decrypt(args.bundle.read_bytes(), password)
        if args.verify_only:
            with tempfile.TemporaryDirectory(prefix="smartreport-independent-") as temp:
                destination = Path(temp) / "restored"
                restore(files, destination)
                original_cwd = Path.cwd()
                try:
                    result = verify_server(destination)
                finally:
                    os.chdir(original_cwd)
                print(json.dumps(result))
        else:
            if not args.destination:
                raise RecoveryError("Specify a NEW empty --destination, never a production path.")
            restore(files, args.destination)
            print("Restored to a NEW directory. Do not replace any live database.")
    except RecoveryError as error:
        print(str(error), file=sys.stderr)
        return 1
    except (OSError, sqlite3.Error) as error:
        import traceback
        frames = [(Path(frame.filename).name, frame.name, frame.lineno)
                  for frame in traceback.extract_tb(error.__traceback__)]
        print(json.dumps({
            "error": type(error).__name__, "errno": getattr(error, "errno", None),
            "sqlite_code": getattr(error, "sqlite_errorname", None), "frames": frames,
        }), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
