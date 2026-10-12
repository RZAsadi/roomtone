"""Isolated dispatch tests for the real RoomTone installer entry point.

The harness copies install.sh to a temporary directory and replaces only the
host-specific root/platform guard and destructive lifecycle function bodies.
Dashboard parsing, menu loop, default dispatch, prompts, and CLI dispatch remain
the production code. No system services or host paths are touched.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent
INSTALLER = ROOT / "install.sh"
GIT_BASH = Path(r"C:\Program Files\Git\bin\bash.exe")
BASH = str(GIT_BASH) if GIT_BASH.exists() else shutil.which("bash")


def replace_function(source: str, name: str, replacement: str) -> str:
    pattern = rf"(?ms)^{re.escape(name)}\(\) \{{.*?^\}}"
    result, count = re.subn(pattern, replacement, source, count=1)
    if count != 1:
        raise AssertionError(f"Could not isolate {name}() in install.sh")
    return result


class InstallerDashboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not BASH or not Path(BASH).exists():
            raise unittest.SkipTest("Bash is required for installer dashboard tests")
        try:
            probe = subprocess.run([BASH, "--version"], capture_output=True, text=True, timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            raise unittest.SkipTest("The configured Bash executable is not runnable")
        if probe.returncode != 0 or "GNU bash" not in (probe.stdout + probe.stderr):
            raise unittest.SkipTest("The configured Bash executable is unavailable (for example, WSL has no installed distro)")
        source = INSTALLER.read_text(encoding="utf-8")
        source = replace_function(source, "require_root", "require_root() { return 0; }")
        # A pipe is used to provide deterministic menu input in CI. Only the
        # terminal-presence guard is bypassed in this disposable test copy.
        source = source.replace(
            '[[ -t 0 ]] || { usage >&2; die "No interactive terminal. Pass install [domain], update, or uninstall explicitly."; }',
            ': # test harness supplies menu input through stdin',
            1,
        )
        source = replace_function(
            source,
            "install_or_update",
            'install_or_update() { printf "ACTION:%s DOMAIN:%s\\n" "$1" "${DOMAIN:-}"; }',
        )
        source = replace_function(
            source,
            "uninstall_roomtone",
            'uninstall_roomtone() { printf "ACTION:uninstall\\n"; }',
        )
        cls.temp = tempfile.TemporaryDirectory(prefix="roomtone-installer-dashboard-")
        cls.script = Path(cls.temp.name) / "install.sh"
        cls.script.write_text(source, encoding="utf-8", newline="\n")
        noninteractive_source = source.replace(
            ': # test harness supplies menu input through stdin',
            '[[ -t 0 ]] || { usage >&2; die "No interactive terminal. Pass install [domain], update, or uninstall explicitly."; }',
            1,
        )
        cls.noninteractive_script = Path(cls.temp.name) / "install-noninteractive.sh"
        cls.noninteractive_script.write_text(noninteractive_source, encoding="utf-8", newline="\n")

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "temp"):
            cls.temp.cleanup()

    def run_installer(self, input_text: str = "", *args: str, script: Path | None = None):
        input_path = Path(self.temp.name) / "menu-input.txt"
        input_path.write_text(input_text, encoding="utf-8", newline="\n")
        with input_path.open("r", encoding="utf-8", newline="\n") as menu_input:
            return subprocess.run(
                [BASH, str(script or self.script), *args], stdin=menu_input,
                text=True, capture_output=True, timeout=5, check=False,
                env={**os.environ, "ROOMTONE_REPO": "https://example.invalid/roomtone.git"},
            )

    def test_default_invocation_opens_dashboard_without_installing(self):
        result = self.run_installer("4\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("RoomTone dashboard", result.stdout)
        self.assertIn("1) Install", result.stdout)
        self.assertIn("2) Update", result.stdout)
        self.assertIn("3) Uninstall", result.stdout)
        self.assertNotIn("ACTION:install", result.stdout)

    def test_noninteractive_default_fails_clearly_without_hanging(self):
        result = self.run_installer("", script=self.noninteractive_script)
        self.assertEqual(result.returncode, 1)
        self.assertIn("No interactive terminal", result.stderr)
        self.assertIn("install [domain], update, or uninstall", result.stderr)

    def test_invalid_selection_reprompts_then_exit(self):
        result = self.run_installer("x\n9\n4\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count("Invalid selection"), 2)
        self.assertGreaterEqual(result.stdout.count("RoomTone dashboard"), 3)

    def test_domain_prompt_eof_or_blank_input_cancels_without_dispatch(self):
        eof = self.run_installer("1\n")
        self.assertEqual(eof.returncode, 0, eof.stderr)
        self.assertIn("Cancelled.", eof.stdout)
        self.assertNotIn("ACTION:install", eof.stdout)

        blank = self.run_installer("1\n\n4\n")
        self.assertEqual(blank.returncode, 0, blank.stderr)
        self.assertIn("Cancelled: no domain entered.", blank.stdout)
        self.assertNotIn("ACTION:install", blank.stdout)

    def test_dashboard_dispatches_install_update_and_uninstall(self):
        install = self.run_installer("1\nvoice.example.com\n4\n")
        self.assertEqual(install.returncode, 0, install.stderr)
        self.assertIn("ACTION:install DOMAIN:voice.example.com", install.stdout)

        update = self.run_installer("2\n4\n")
        self.assertEqual(update.returncode, 0, update.stderr)
        self.assertIn("ACTION:update DOMAIN:", update.stdout)

        uninstall = self.run_installer("3\n4\n")
        self.assertEqual(uninstall.returncode, 0, uninstall.stderr)
        self.assertIn("ACTION:uninstall", uninstall.stdout)

    def test_explicit_cli_action_dispatch_is_preserved(self):
        install = self.run_installer("", "install", "voice.example.com")
        self.assertEqual(install.returncode, 0, install.stderr)
        self.assertIn("ACTION:install DOMAIN:voice.example.com", install.stdout)

        update = self.run_installer("", "update")
        self.assertEqual(update.returncode, 0, update.stderr)
        self.assertIn("ACTION:update DOMAIN:", update.stdout)

        uninstall = self.run_installer("", "uninstall")
        self.assertEqual(uninstall.returncode, 0, uninstall.stderr)
        self.assertIn("ACTION:uninstall", uninstall.stdout)

    def test_unknown_cli_argument_retains_legacy_domain_behavior(self):
        result = self.run_installer("", "voice.example.com")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ACTION:install DOMAIN:voice.example.com", result.stdout)

    def test_help_is_available_without_root_or_platform_guard(self):
        # This path exercises the actual, unmodified production script because
        # --help now dispatches before require_root and touches no host paths.
        result = subprocess.run(
            [BASH, str(INSTALLER), "--help"], text=True, capture_output=True,
            timeout=5, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Usage:", result.stdout)

    def test_fresh_install_rollback_removes_the_entire_new_app_tree(self):
        source = INSTALLER.read_text(encoding="utf-8")
        self.assertIn(
            'elif [[ "$UNINSTALL_ACTIVE" != 1 && ! -d "$BACKUP_DIR/opt/roomtone" ]]; then',
            source,
        )
        self.assertIn(
            "# Remove the whole newly-created tree, including any database or other",
            source,
        )

    def test_uninstall_preserves_extended_turn_env_and_coturn_defaults(self):
        source = INSTALLER.read_text(encoding="utf-8")
        self.assertIn('if cmp -s "$TURN_ENV" "$TMP_DIR/expected-turn.env"; then', source)
        self.assertIn('Preserving $TURN_ENV because it contains additional or edited settings', source)
        self.assertIn("sed -i 's/^TURNSERVER_ENABLED=.*/TURNSERVER_ENABLED=1/' /etc/default/coturn", source)
        self.assertIn('printf \'\\nTURNSERVER_ENABLED=1\\n\' >> /etc/default/coturn', source)

    def test_caddy_adopts_exact_unmarked_site_and_rejects_duplicate_domain(self):
        source = INSTALLER.read_text(encoding="utf-8")
        self.assertIn('expected_unmarked="$(printf \'%s {\\n    encode zstd gzip\\n    reverse_proxy 127.0.0.1:8000\\n}\' "$DOMAIN")"', source)
        self.assertIn('if [[ "$(cat /etc/caddy/Caddyfile)" == "$expected_unmarked" ]]; then', source)
        self.assertIn('Caddyfile already defines $DOMAIN outside the RoomTone managed block', source)

    def test_pip_requirements_staging_is_accessible_to_service_account(self):
        source = INSTALLER.read_text(encoding="utf-8")
        self.assertIn('STAGE_REQUIREMENTS="$APP_DIR/.stage-requirements-$$.txt"', source)
        self.assertEqual(source.count('install -r "$STAGE_REQUIREMENTS"'), 2)
        self.assertIn('[[ -z "$STAGE_REQUIREMENTS" ]] || rm -f -- "$STAGE_REQUIREMENTS"', source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
