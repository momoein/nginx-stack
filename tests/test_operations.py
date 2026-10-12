"""Exercise failure boundaries without Docker access or ACME requests."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


SOURCE = Path(__file__).resolve().parents[1]


class OperationsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="nginx stack tests ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        shutil.copytree(SOURCE / "scripts", self.root / "scripts")
        self.storage = self.root / "letsencrypt" / "conf"
        self.storage.mkdir(parents=True)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.calls = self.root / "docker-calls"
        docker = self.bin / "docker"
        docker.write_text("""#!/bin/sh
printf '%s|%s\\n' "$PWD" "$*" >> "$DOCKER_CALLS"
case "$*" in
  *'certbot renew --quiet') exit "${RENEW_EXIT:-0}" ;;
  *'nginx nginx -t') exit "${VALIDATE_EXIT:-0}" ;;
  *'nginx nginx -s reload') exit "${RELOAD_EXIT:-0}" ;;
  *) exit 99 ;;
esac
""")
        docker.chmod(0o755)
        self.environment = {
            **os.environ,
            "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
            "DOCKER_CALLS": str(self.calls),
            "RENEW_EXIT": "0",
            "VALIDATE_EXIT": "0",
            "RELOAD_EXIT": "0",
        }

    def run_script(self, name, **overrides):
        return subprocess.run(
            [str(self.root / "scripts" / name)],
            cwd="/",
            env={**self.environment, **overrides},
            text=True,
            capture_output=True,
            check=False,
        )

    def docker_commands(self):
        if not self.calls.exists():
            return []
        entries = self.calls.read_text().splitlines()
        for entry in entries:
            self.assertEqual(entry.split("|", 1)[0], str(self.root))
        return [entry.split("|", 1)[1] for entry in entries]

    def test_successful_renewal_validates_and_reloads_from_any_directory(self):
        result = self.run_script("renew.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.docker_commands()), 3)
        self.assertIn("run -T --rm --no-deps certbot renew --quiet", self.docker_commands()[0])
        self.assertTrue(self.docker_commands()[1].endswith("nginx -t"))
        self.assertTrue(self.docker_commands()[2].endswith("nginx -s reload"))

    def test_partial_renewal_failure_still_validates_and_reloads(self):
        result = self.run_script("renew.sh", RENEW_EXIT="7")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Certbot renewal failed (exit 7)", result.stderr)
        self.assertEqual(len(self.docker_commands()), 3)
        self.assertTrue(self.docker_commands()[-1].endswith("nginx -s reload"))

    def test_invalid_configuration_never_sends_reload(self):
        result = self.run_script("renew.sh", VALIDATE_EXIT="3")
        self.assertEqual(result.returncode, 1)
        self.assertIn("validation or reload failed (exit 3)", result.stderr)
        self.assertEqual(len(self.docker_commands()), 2)

    def test_reload_failure_is_reported(self):
        result = self.run_script("renew.sh", RELOAD_EXIT="4")
        self.assertEqual(result.returncode, 1)
        self.assertIn("validation or reload failed (exit 4)", result.stderr)

    def test_both_failures_are_reported(self):
        result = self.run_script("renew.sh", RENEW_EXIT="7", VALIDATE_EXIT="3")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Certbot renewal failed", result.stderr)
        self.assertIn("validation or reload failed", result.stderr)
        self.assertEqual(len(self.docker_commands()), 2)

    def test_standalone_reload_preserves_validation_failure(self):
        result = self.run_script("reload.sh", VALIDATE_EXIT="3")
        self.assertEqual(result.returncode, 3)
        self.assertEqual(len(self.docker_commands()), 1)

    def make_certificate(self, name, days):
        lineage = self.storage / "live" / name
        lineage.mkdir(parents=True)
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
             "-keyout", str(self.root / (name + ".key")),
             "-out", str(lineage / "fullchain.pem"),
             "-days", str(days), "-subj", "/CN=" + name],
            capture_output=True,
            check=True,
        )
        return lineage

    def test_empty_storage_is_a_visible_bootstrap_notice(self):
        result = self.run_script("check-certificates.sh")
        self.assertEqual(result.returncode, 0)
        self.assertIn("No managed certificates", result.stderr)

    def test_missing_storage_fails(self):
        self.storage.rmdir()
        result = self.run_script("check-certificates.sh")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Cannot read certificate storage", result.stderr)

    def test_broken_live_directory_fails(self):
        (self.storage / "live").symlink_to("missing-directory")
        result = self.run_script("check-certificates.sh")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Cannot read certificate lineages", result.stderr)

    def test_broken_lineage_directory_fails(self):
        (self.storage / "live").mkdir()
        (self.storage / "live" / "broken.example").symlink_to("missing-directory")
        result = self.run_script("check-certificates.sh")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Broken certificate lineage", result.stderr)

    def test_healthy_certificate_is_silent_and_supports_archive_symlinks(self):
        lineage = self.make_certificate("healthy.example", 30)
        archive = self.storage / "archive" / "healthy.example"
        archive.mkdir(parents=True)
        (lineage / "fullchain.pem").rename(archive / "fullchain1.pem")
        (lineage / "fullchain.pem").symlink_to("../../archive/healthy.example/fullchain1.pem")
        result = self.run_script("check-certificates.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout, "")

    def test_expiring_certificate_is_reported(self):
        self.make_certificate("expiring.example", 1)
        result = self.run_script("check-certificates.sh")
        self.assertEqual(result.returncode, 1)
        self.assertIn("expires within 14 days", result.stderr)
        self.assertIn("notAfter=", result.stderr)

    def test_expired_certificate_is_reported(self):
        lineage = self.make_certificate("expired.example", 1)
        certificate = lineage / "fullchain.pem"
        subprocess.run(
            ["openssl", "x509", "-in", str(certificate),
             "-signkey", str(self.root / "expired.example.key"),
             "-days", "0", "-out", str(certificate)],
            capture_output=True,
            check=True,
        )
        result = self.run_script("check-certificates.sh")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Certificate is expired", result.stderr)

    def test_missing_invalid_and_broken_certificates_are_all_reported(self):
        live = self.storage / "live"
        for name in ("missing.example", "invalid.example", "broken.example"):
            (live / name).mkdir(parents=True)
        (live / "invalid.example" / "fullchain.pem").write_text("invalid certificate")
        (live / "broken.example" / "fullchain.pem").symlink_to("../../missing.pem")
        result = self.run_script("check-certificates.sh")
        self.assertEqual(result.returncode, 1)
        for name in ("missing.example", "invalid.example", "broken.example"):
            self.assertIn(name, result.stderr)


if __name__ == "__main__":
    unittest.main()
