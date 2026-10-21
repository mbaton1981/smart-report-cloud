import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from limited_mail_launch import LocalTrial
from mail_worker import WorkerError
from test_limited_mail import FOLDER, HOST, USERNAME, RestrictedIMAP
from test_mail_automation import eml, pdf


class LocalTrialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        RestrictedIMAP.messages = {11: eml(pdf())}
        RestrictedIMAP.commands = []
        RestrictedIMAP.epoch = 50
        RestrictedIMAP.count_override = RestrictedIMAP.search_override = None
        RestrictedIMAP.fail_uid = RestrictedIMAP.response_uid = None

    def test_single_pass_local_launcher_real_parser_no_live_imap(self):
        trial = LocalTrial(self.root, client_factory=RestrictedIMAP)
        with patch("imaplib.IMAP4_SSL", side_effect=AssertionError("Live IMAP prohibited")):
            trial.run(HOST, USERNAME, FOLDER, "isolated-only")
        result = json.loads((self.root / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(result["expenses_created"], 0)
        self.assertEqual(result["uids"], [11])
        self.assertEqual(result["drafts"][0]["result"]["fields"]["amount_due"], "125.00")
        with self.assertRaisesRegex(WorkerError, "one_manual_pass_only"):
            trial.run(HOST, USERNAME, FOLDER, "isolated-only")
        for path in self.root.rglob("*"):
            if path.is_file():
                self.assertNotIn(b"isolated-only", path.read_bytes())

    def test_folder_unavailable_does_not_search_or_fetch(self):
        trial = LocalTrial(self.root, client_factory=RestrictedIMAP)
        with patch.object(RestrictedIMAP, "select", return_value=("NO", [b"absent"])):
            with self.assertRaisesRegex(WorkerError, "readonly_folder_failed"):
                trial.run(HOST, USERNAME, FOLDER, "isolated-only")
        self.assertFalse(any(item[0] in ("SEARCH", "FETCH") for item in RestrictedIMAP.commands))
        self.assertFalse((self.root / "result.json").exists())

    def test_replay_requires_existing_queue_before_network(self):
        with self.assertRaisesRegex(WorkerError, "replay_requires"):
            LocalTrial(self.root, replay=True, client_factory=RestrictedIMAP)
        self.assertEqual(RestrictedIMAP.commands, [])

    def test_five_uid_replay_preserves_original_results_and_drafts(self):
        RestrictedIMAP.messages = {uid: eml(pdf(f"Fixture {uid}"))
                                   for uid in (11, 20, 33, 41, 58)}
        first = LocalTrial(self.root, client_factory=RestrictedIMAP)
        first.run(HOST, USERNAME, FOLDER, "isolated-only")
        result_before = (self.root / "result.json").read_bytes()
        manifest_before = (self.root / "selected-uids.json").read_bytes()
        original_before = {p.name: p.read_bytes() for p in (self.root / "intake" / "originals").iterdir()}
        RestrictedIMAP.commands.clear()
        replay = LocalTrial(self.root, replay=True, client_factory=RestrictedIMAP)
        with patch("imaplib.IMAP4_SSL", side_effect=AssertionError("Live IMAP prohibited")):
            replay.run(HOST, USERNAME, FOLDER, "isolated-only")
        self.assertEqual(replay.result["status"]["counts"],
                         {"messages": 5, "drafts": 5, "attachments": 5})
        self.assertEqual(replay.result["drafts"], first.result["drafts"])
        self.assertEqual(replay.result["expenses_created"], 0)
        self.assertEqual((self.root / "result.json").read_bytes(), result_before)
        self.assertEqual((self.root / "selected-uids.json").read_bytes(), manifest_before)
        self.assertEqual({p.name: p.read_bytes() for p in (self.root / "intake" / "originals").iterdir()},
                         original_before)
        self.assertFalse(any(c[0] == "SEARCH" for c in RestrictedIMAP.commands))
        self.assertEqual({int(c[1]) for c in RestrictedIMAP.commands if c[0] == "FETCH"},
                         {11, 20, 33, 41, 58})
        self.assertTrue(replay.output_path("result.json").exists())
