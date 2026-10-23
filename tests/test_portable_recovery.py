import tempfile
from pathlib import Path
import unittest

from portable_recovery import RecoveryError, decrypt, encrypt, publish, restore, safe_name


class PortableRecoveryTests(unittest.TestCase):
    password = "synthetic-password-for-tests-only"
    files = {"backup/test.db": b"synthetic data", "recovery/key": b"synthetic key"}

    def test_roundtrip_no_profile_or_dpapi(self):
        self.assertEqual(decrypt(encrypt(self.files, self.password), self.password), self.files)

    def test_wrong_password_and_tampering_rejected(self):
        data = encrypt(self.files, self.password)
        for payload, password in (
            (data, "another-synthetic-password"), (data[:-1] + bytes([data[-1] ^ 1]), self.password),
            (data[:30], self.password), (b"unknown!" + data[8:], self.password),
        ):
            with self.subTest(length=len(payload)), self.assertRaises(RecoveryError):
                decrypt(payload, password)

    def test_password_minimum(self):
        with self.assertRaises(RecoveryError):
            encrypt(self.files, "short")

    def test_paths_rejected(self):
        for name in ("../key", "/key", "C:/key", "a\\key", "a//b", "./key", ""):
            with self.subTest(name=name), self.assertRaises(RecoveryError):
                safe_name(name)

    def test_new_versions_never_overwrite_old_even_after_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            first = publish(temp, self.files, self.password)
            original = first.read_bytes()
            second = publish(temp, self.files, self.password)
            self.assertNotEqual(first, second)
            self.assertEqual(first.read_bytes(), original)
            with self.assertRaises(RecoveryError):
                publish(temp, self.files, "short")
            self.assertEqual(len(list(Path(temp).glob("*.srbackup"))), 2)

    def test_restore_only_into_new_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / "new"
            restore(self.files, destination)
            self.assertEqual((destination / "recovery" / "key").read_bytes(), b"synthetic key")
            with self.assertRaises(FileExistsError):
                restore(self.files, destination)

    def test_randomized_encryption_and_no_plaintext(self):
        first = encrypt(self.files, self.password)
        self.assertNotEqual(first, encrypt(self.files, self.password))
        self.assertNotIn(b"synthetic key", first)
        self.assertNotIn(b"recovery/key", first)


if __name__ == "__main__":
    unittest.main()
