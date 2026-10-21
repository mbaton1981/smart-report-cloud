"""One-pass, read-only IMAP worker. Credentials are environment-only."""
from __future__ import annotations

import argparse
import hashlib
import imaplib
import json
import os
import re
import ssl
import sys
import threading
import time
from urllib.parse import urlparse

import requests

from mail_intake import MAX_MESSAGE, mailbox_key


class WorkerError(RuntimeError):
    pass


class IntakeClient:
    def __init__(self, url, token, allow_local=False):
        parsed = urlparse(url)
        local = parsed.hostname in ("127.0.0.1", "::1", "localhost")
        if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise WorkerError("invalid_api_url")
        if parsed.scheme != "https" and not (allow_local and local and parsed.scheme == "http"):
            raise WorkerError("https_required")
        if len(token) < 32:
            raise WorkerError("dedicated_token_required")
        self.url, self.token = url.rstrip("/") + "/api/mail-intake", token

    def call(self, method, path, **kwargs):
        response = requests.request(
            method, self.url + path, headers={
                "Authorization": f"Bearer {self.token}",
                **kwargs.pop("headers", {}),
            }, timeout=(10, 150), allow_redirects=False, **kwargs)
        if not 200 <= response.status_code < 300:
            raise WorkerError(f"api_http_{response.status_code}")
        return response.json()

    def control(self, lease, action, **data):
        return self.call("POST", "/control", json={**lease, "action": action, **data})


class Heartbeat:
    def __init__(self, api, lease):
        self.api, self.lease = api, lease
        self.stop_event = threading.Event()
        self.failed = threading.Event()
        self.thread = threading.Thread(target=self.run, name="mail-lease-renewal", daemon=True)

    def run(self):
        while not self.stop_event.wait(30):
            try:
                self.api.control(self.lease, "renew")
            except (requests.RequestException, WorkerError, ValueError):
                self.failed.set()
                return

    def check(self):
        if self.failed.is_set():
            raise WorkerError("lease_renewal_failed")

    def stop(self):
        self.stop_event.set()
        if self.thread.ident is not None:
            self.thread.join()


def ok(response, stage):
    if response[0] != "OK":
        raise WorkerError(stage + "_failed")
    return response[1]


def imap_number(client, name):
    response = client.response(name)[1]
    if not response or not response[0] or not response[0].isdigit():
        raise WorkerError("missing_" + name.casefold())
    number = int(response[0])
    if not 1 <= number <= 4294967295:
        raise WorkerError("invalid_" + name.casefold())
    return number


def run_once(api, host, username, password, folder="INBOX", *, client_factory=imaplib.IMAP4_SSL,
             max_messages=100, max_windows=100, window=500):
    if not host or not username or not password or folder != "INBOX":
        raise WorkerError("explicit_imap_credentials_and_inbox_required")
    if not 1 <= max_messages <= 1000 or not 1 <= window <= 1000 or not 1 <= max_windows <= 1000:
        raise WorkerError("invalid_batch_limits")
    lease = api.call("POST", "/lease", json={"mailbox": mailbox_key(host, username, folder)})
    heartbeat = Heartbeat(api, lease)
    heartbeat.thread.start()
    client = None
    processed = 0
    try:
        client = client_factory(host, 993, ssl_context=ssl.create_default_context(), timeout=30)
        ok(client.login(username, password), "login")
        ok(client.select(folder, readonly=True), "readonly_select")
        epoch, highwater = imap_number(client, "UIDVALIDITY"), imap_number(client, "UIDNEXT") - 1
        cursor = api.control(lease, "epoch", epoch=epoch)["cursor"]
        if cursor > highwater:
            raise WorkerError("uidnext_regressed_manual_review_required")
        for _ in range(max_windows):
            heartbeat.check()
            if cursor >= highwater or processed >= max_messages:
                break
            end = min(cursor + window, highwater)
            found = ok(client.uid("SEARCH", None, "UID", f"{cursor + 1}:{end}"), "uid_search")
            try:
                uids = sorted(set(int(value) for value in (found[0] or b"").split()))
            except (ValueError, IndexError):
                raise WorkerError("invalid_uid_search") from None
            if any(uid <= cursor or uid > end for uid in uids):
                raise WorkerError("out_of_range_uid")
            completed_window = True
            for uid in uids:
                heartbeat.check()
                if processed >= max_messages:
                    completed_window = False
                    break
                metadata = ok(client.uid("FETCH", str(uid), "(RFC822.SIZE)"), "message_size")
                sizes = [re.search(rb"RFC822.SIZE\s+(\d+)", item) for item in metadata if isinstance(item, bytes)]
                sizes = [int(match.group(1)) for match in sizes if match]
                if len(sizes) != 1:
                    raise WorkerError("missing_message_size")
                # Stop visibly rather than silently skipping a message or advancing its cursor.
                if sizes[0] > MAX_MESSAGE:
                    raise WorkerError("message_too_large_manual_review_required")
                bodies = ok(client.uid("FETCH", str(uid), "(BODY.PEEK[])"), "message_body")
                raw = [item[1] for item in bodies if isinstance(item, tuple) and len(item) == 2]
                if len(raw) != 1 or not isinstance(raw[0], bytes) or len(raw[0]) > MAX_MESSAGE:
                    raise WorkerError("invalid_message_body")
                api.call("POST", "/messages",
                         params={**lease, "epoch": epoch, "uid": uid},
                         headers={"X-Content-SHA256": hashlib.sha256(raw[0]).hexdigest(),
                                  "Content-Type": "message/rfc822"}, data=raw[0])
                cursor = uid
                processed += 1
            if completed_window:
                api.control(lease, "advance", epoch=epoch, cursor=end)
                cursor = end
        # Receipt checkpoints are already durable. Recognition can resume independently
        # after a killed request, without fetching or overwriting old results.
        recognized = 0
        for draft in api.call("GET", "/pending")["drafts"]:
            heartbeat.check()
            api.call("POST", "/recognize", json={
                **lease, "draft_id": draft["id"], "sha256": draft["sha256"]})
            recognized += 1
        heartbeat.stop()
        heartbeat.check()
        backlog = max(0, highwater - cursor)
        api.control(lease, "finish", backlog=backlog)
        return {"completed": True, "processed": processed, "cursor": cursor,
                "backlog_uid_span": backlog, "epoch": epoch, "recognized": recognized}
    except (requests.RequestException, WorkerError, imaplib.IMAP4.error, OSError, ValueError) as error:
        heartbeat.stop()
        try:
            api.control(lease, "fail", code=str(error) if isinstance(error, WorkerError) else "worker_failed")
        except (requests.RequestException, WorkerError, ValueError):
            # An unreachable API leaves the renewable lease to expire; exit remains a failure.
            print(json.dumps({"event": "failure_status_unavailable"}), file=sys.stderr)
        raise
    finally:
        heartbeat.stop()
        if client is not None:
            try:
                client.logout()
            except (imaplib.IMAP4.error, OSError):
                print(json.dumps({"event": "imap_logout_failed"}), file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-local", action="store_true")
    parser.add_argument("--monitor", action="store_true")
    parser.add_argument("--max-age", type=int, default=900)
    parser.add_argument("--max-messages", type=int, default=100)
    parser.add_argument("--max-windows", type=int, default=100)
    args = parser.parse_args()
    try:
        api = IntakeClient(os.environ["SMARTREPORT_MAIL_API_URL"],
                           os.environ["SMARTREPORT_MAIL_INTAKE_TOKEN"], args.allow_local)
        if args.monitor:
            status = api.call("GET", "/status")
            operational = (status["last_success"] is not None
                           and time.time() - status["last_success"] <= args.max_age
                           and status["last_error"] is None and status["backlog"] == 0
                           and status["pending_recognition"] == 0)
            attention = bool(status["recognition_attention"] or status["conflicts"] or status["invalid_attachments"])
            healthy = operational and not attention
            print(json.dumps({"event": "mail_monitor", "healthy": healthy,
                              "operational_healthy": operational, "review_attention": attention, **status}))
            return 0 if healthy else 1
        result = run_once(api, os.environ["SMARTREPORT_IMAP_HOST"],
                          os.environ["SMARTREPORT_IMAP_USERNAME"], os.environ["SMARTREPORT_IMAP_PASSWORD"],
                          max_messages=args.max_messages, max_windows=args.max_windows)
        print(json.dumps({"event": "mail_pass", **result}))
        return 0
    except (KeyError, WorkerError, requests.RequestException, imaplib.IMAP4.error, OSError, ValueError) as error:
        # Exception strings from IMAP/HTTP can contain credentials or message headers.
        print(json.dumps({"event": "mail_pass_failed", "error_type": type(error).__name__,
                          "code": str(error) if isinstance(error, WorkerError) else "external_failure"}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
