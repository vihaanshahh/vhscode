from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fleet"))
import fleet_cursor_login as auth
from fleet_models import set_cli_command


class CursorSignInTests(unittest.TestCase):
    def test_worker_uses_native_url_then_verifies_the_catalog(self):
        url = "https://cursor.com/loginDeepControl?challenge=fixture"
        with tempfile.TemporaryDirectory() as tmp:
            cli = Path(tmp) / "agent with spaces"
            cli.write_text(f'#!/bin/sh\ntest "$1" = --fixture && test "$2" = login && test "$NO_OPEN_BROWSER" = 1 || exit 1\n'
                           f'echo "Open {url}"\necho "private native output"\n')
            cli.chmod(0o700)
            with patch("fleet_models._models", return_value=[{"id": "auto"}]) as catalog, \
                 patch.dict(os.environ, {"XDG_STATE_HOME": tmp}):
                set_cli_command(tmp, "cursor", f'"{cli}" --fixture')
                out = io.StringIO()
                with redirect_stdout(out):
                    auth._on_host(tmp)
                self.assertEqual([json.loads(line) for line in out.getvalue().splitlines()],
                                 [{"url": url}, {"verified": True}])
                catalog.assert_called_once_with(Path(tmp).resolve(), "cursor")

    def test_untrusted_urls_and_unsuccessful_verification(self):
        for url in ("https://cursor.com.evil.test/login?code=1", "http://cursor.com/login?c=1",
                    "https://user@cursor.com/login?c=1", "https://cursor.com:8000/login?c=1",
                    "https://cursor.com/login?c=1\n"):
            self.assertFalse(auth.cursor_url(url), url)

        def no_verification(_, __, receive):
            receive(b'{"url":"https://evil.test/login?fixture=1"}')
            return 0
        with patch.object(auth, "_lines", side_effect=no_verification), patch("subprocess.run") as opener:
            with self.assertRaisesRegex(ValueError, "did not finish"):
                auth.login("", str(Path.cwd()))
            opener.assert_not_called()
