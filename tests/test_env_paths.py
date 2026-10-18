"""Partner and admin env files come from the environment, not a fixed machine path."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import orchestrator.run_demo as run_demo  # noqa: E402


class EnvPathTests(unittest.TestCase):
    def test_defaults_are_repo_local_files(self) -> None:
        partner = str(run_demo.DEFAULT_PARTNER_ENV)
        admin = str(run_demo.DEFAULT_ADMIN_ENV)
        self.assertTrue(partner.endswith(".env.partner"))
        self.assertTrue(admin.endswith(".env.admin"))
        self.assertNotIn("home/box", partner)
        self.assertNotIn("home/box", admin)
        self.assertNotIn("secrets/", partner)
        self.assertNotIn("secrets/", admin)

    def test_env_var_overrides_and_blank_falls_back(self) -> None:
        old_partner = os.environ.get("PARTNER_CALLER_ENV_FILE")
        old_admin = os.environ.get("ADMIN_ENV_FILE")
        try:
            os.environ["PARTNER_CALLER_ENV_FILE"] = "/tmp/partner.env"
            os.environ["ADMIN_ENV_FILE"] = "  "
            self.assertEqual(
                run_demo._env_file_from_environ("PARTNER_CALLER_ENV_FILE", ".env.partner"),
                Path("/tmp/partner.env"),
            )
            self.assertEqual(
                run_demo._env_file_from_environ("ADMIN_ENV_FILE", ".env.admin"),
                run_demo.ROOT / ".env.admin",
            )
        finally:
            if old_partner is None:
                os.environ.pop("PARTNER_CALLER_ENV_FILE", None)
            else:
                os.environ["PARTNER_CALLER_ENV_FILE"] = old_partner
            if old_admin is None:
                os.environ.pop("ADMIN_ENV_FILE", None)
            else:
                os.environ["ADMIN_ENV_FILE"] = old_admin

    def test_parser_help_names_the_env_vars(self) -> None:
        help_text = run_demo.build_parser().format_help()
        self.assertNotIn("/home/box", help_text)
        self.assertIn("PARTNER_CALLER_ENV_FILE", help_text)
        self.assertIn("ADMIN_ENV_FILE", help_text)

    def test_load_env_file_still_parses_local_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sample = Path(tmp) / "partner.env"
            sample.write_text(
                "\n".join(
                    [
                        "# comment",
                        "ARGENTINE_CALLER_ID=partner",
                        "ARGENTINE_CALLER_TOKEN='lab-token'",
                        "EMPTY=",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            loaded = run_demo.load_env_file(sample)
        self.assertEqual(loaded["ARGENTINE_CALLER_ID"], "partner")
        self.assertEqual(loaded["ARGENTINE_CALLER_TOKEN"], "lab-token")
        self.assertEqual(loaded["EMPTY"], "")


if __name__ == "__main__":
    unittest.main()
