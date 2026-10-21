"""Explicit folder/UID allowlist for a local, manually approved mail trial."""
from __future__ import annotations

import hashlib
import imaplib
import json
import os
import re
import ssl
from pathlib import Path
from urllib.parse import urlparse

import requests

from mail_intake import MAX_MESSAGE, mailbox_key
from mail_worker import Heartbeat, WorkerError, imap_number, ok


def quoted_folder(folder: str) -> str:
    if (not isinstance(folder, str) or not folder.strip() or folder.casefold() == "inbox"
            or not folder.isascii() or any(ord(char) < 32 or ord(char) == 127 for char in folder)):
        raise WorkerError("explicit_non_inbox_ascii_folder_required")
    return '"' + folder.replace("\\", "\\\\").replace('"', '\\"') + '"'


def validate_selection(value, mailbox, folder):
    if (not isinstance(value, dict) or value.get("version") != 1
            or value.get("mailbox") != mailbox or value.get("folder") != folder):
        raise WorkerError("selection_identity_mismatch")
    epoch, uids = value.get("epoch"), value.get("uids")
    if type(epoch) is not int or not 1 <= epoch <= 4294967295:
        raise WorkerError("invalid_selection_epoch")
    if (not isinstance(uids, list) or not 1 <= len(uids) <= 5
            or any(type(uid) is not int or not 1 <= uid <= 4294967295 for uid in uids)
            or uids != sorted(set(uids))):
        raise WorkerError("invalid_selection_uids")
    return value


def save_selection(path: Path, selection):
    # Exclusive creation is the durable boundary before any message body is read.
    # An interrupted/invalid file must be inspected, never silently regenerated.
    with path.open("x", encoding="utf-8") as stream:
        json.dump(selection, stream, ensure_ascii=True)
        stream.flush()
        os.fsync(stream.fileno())
    if os.name != "nt":
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def selected_uid(header: bytes, expected: int):
    matches = re.findall(rb"\bUID\s+(\d+)\b", header)
    if len(matches) != 1 or int(matches[0]) != expected:
        raise WorkerError("fetch_uid_mismatch")


def run_limited(api, host: str, username: str, password: str, folder: str,
                selection_path: Path, *, client_factory=imaplib.IMAP4_SSL, replay_only=False):
    """First pass pins <=5 UIDs; every retry fetches only those same UIDs."""
    quoted = quoted_folder(folder)
    parsed = urlparse(api.url)
    if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "::1", "localhost"):
        raise WorkerError("limited_trial_requires_loopback_api")
    if not host or not username or not password:
        raise WorkerError("explicit_imap_credentials_required")
    mailbox = mailbox_key(host, username, folder)
    selection_path = Path(selection_path)
    if not selection_path.is_absolute() or not selection_path.parent.is_dir():
        raise WorkerError("explicit_existing_selection_directory_required")
    selection = None
    if selection_path.exists():
        try:
            selection = validate_selection(json.loads(selection_path.read_text(encoding="utf-8")), mailbox, folder)
        except (ValueError, UnicodeError):
            raise WorkerError("selection_file_invalid_do_not_reset") from None
    if replay_only and selection is None:
        raise WorkerError("replay_requires_existing_selection")
    status = api.call("GET", "/status")
    if selection is None and (any(status["counts"].values()) or status["epoch"] is not None):
        raise WorkerError("first_limited_pass_requires_empty_intake")
    if selection is not None and status["epoch"] not in (None, str(selection["epoch"])):
        raise WorkerError("intake_epoch_changed_do_not_reset")
    lease = api.call("POST", "/lease", json={"mailbox": mailbox})
    heartbeat = Heartbeat(api, lease)
    heartbeat.thread.start()
    client = None
    reports = []
    try:
        client = client_factory(host, 993, ssl_context=ssl.create_default_context(), timeout=30)
        ok(client.login(username, password), "login")
        # imaplib translates readonly=True to EXAMINE, not SELECT.
        counts = ok(client.select(quoted, readonly=True), "readonly_folder")
        if (not counts or not isinstance(counts[0], bytes) or not counts[0].isdigit()
                or not 1 <= int(counts[0]) <= 5):
            raise WorkerError("folder_must_contain_one_to_five_messages")
        epoch = imap_number(client, "UIDVALIDITY")
        if selection is not None:
            if epoch != selection["epoch"]:
                raise WorkerError("uidvalidity_changed_do_not_rescan")
        else:
            found = ok(client.uid("SEARCH", None, "ALL"), "selected_folder_uid_search")
            if len(found) != 1 or not isinstance(found[0], bytes):
                raise WorkerError("invalid_selected_folder_search")
            try:
                uids = [int(raw) for raw in found[0].split()]
            except ValueError:
                raise WorkerError("invalid_selected_folder_search") from None
            selection = validate_selection({
                "version": 1, "mailbox": mailbox, "folder": folder, "epoch": epoch, "uids": sorted(uids),
            }, mailbox, folder)
            if len(uids) != int(counts[0]):
                raise WorkerError("folder_changed_before_selection")
            save_selection(selection_path, selection)
        api.control(lease, "epoch", epoch=epoch)
        for uid in selection["uids"]:
            heartbeat.check()
            metadata = ok(client.uid("FETCH", str(uid), "(UID RFC822.SIZE)"), "selected_message_size")
            headers = [item for item in metadata if isinstance(item, bytes) and b"RFC822.SIZE" in item]
            if len(headers) != 1:
                raise WorkerError("selected_message_missing")
            selected_uid(headers[0], uid)
            size = re.search(rb"\bRFC822.SIZE\s+(\d+)\b", headers[0])
            if not size or int(size.group(1)) > MAX_MESSAGE:
                raise WorkerError("selected_message_too_large")
            body = ok(client.uid("FETCH", str(uid), "(UID BODY.PEEK[])"), "selected_message_body")
            tuples = [item for item in body if isinstance(item, tuple) and len(item) == 2]
            if len(tuples) != 1 or not all(isinstance(item, bytes) for item in tuples[0]):
                raise WorkerError("selected_message_missing")
            header, raw = tuples[0]
            selected_uid(header, uid)
            if not raw or len(raw) > MAX_MESSAGE:
                raise WorkerError("selected_message_too_large")
            report = api.call("POST", "/messages",
                              params={**lease, "epoch": epoch, "uid": uid},
                              headers={"X-Content-SHA256": hashlib.sha256(raw).hexdigest(),
                                       "Content-Type": "message/rfc822"}, data=raw)
            reports.append({"uid": uid, **report})
        # Only recognize drafts referenced by the frozen receipt set, not /pending.
        drafts = {item["draft_id"]: item["sha256"] for report in reports
                  for item in report["attachments"] if item["outcome"] == "draft"}
        for draft_id, digest in drafts.items():
            heartbeat.check()
            api.call("POST", "/recognize", json={**lease, "draft_id": draft_id, "sha256": digest})
        heartbeat.stop()
        heartbeat.check()
        api.control(lease, "finish", backlog=0)
        return {"completed": True, "folder": folder, "epoch": epoch,
                "uids": selection["uids"], "messages": reports, "drafts_in_selection": len(drafts)}
    except (WorkerError, requests.RequestException, imaplib.IMAP4.error, OSError, ValueError) as error:
        heartbeat.stop()
        try:
            api.control(lease, "fail")
        except (WorkerError, requests.RequestException, OSError, ValueError):
            raise WorkerError("limited_pass_failed_status_unavailable") from error
        raise
    finally:
        heartbeat.stop()
        if client is not None:
            try:
                client.logout()
            except (imaplib.IMAP4.error, OSError) as error:
                raise WorkerError("limited_trial_logout_failed") from error
