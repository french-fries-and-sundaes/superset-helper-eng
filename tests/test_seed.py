"""Every seed issue must parse, carry the sections the bot expects, and have a verify block that the
fork-side allowlist will actually accept (otherwise the verification check could never pass)."""
import importlib.util
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent


def load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


dv = load("devin_verify", "fork-files/.github/scripts/devin_verify.py")
seed = load("seed_fork", "scripts/seed_fork.py")


class SeedTests(unittest.TestCase):
    files = sorted((ROOT / "seed" / "issues").glob("*.md"))

    def test_there_are_seven_issues_with_unique_titles(self):
        titles = [seed.parse_issue(f)[0] for f in self.files]
        self.assertEqual(len(titles), 7)
        self.assertEqual(len(set(titles)), 7)

    def test_each_issue_has_the_expected_sections_and_an_allowed_verify_command(self):
        for f in self.files:
            title, labels, body = seed.parse_issue(f)
            for section in ("## Bug Description", "## How to Triage / Test", "## Verify command", "```verify"):
                self.assertIn(section, body, f.name)
            block = dv.VERIFY_BLOCK.search(body)
            self.assertIsNotNone(block, f.name)
            self.assertTrue(dv.validate(block.group(1)), f.name)  # raises if any command is disallowed
            self.assertTrue(any(l.startswith("type:") for l in labels), f.name)

    def test_the_expected_outcomes_are_documented_for_the_limit_cases(self):
        by = {f.name: f.read_text() for f in self.files}
        self.assertIn("FALSE POSITIVE", by["01-underscore-already-fixed.md"])
        self.assertIn("devin:blocked", by["06-paramiko-sha1-advisory.md"])
        self.assertIn("TOO BROAD", by["07-raise-api-coverage.md"])

    def test_issue_template_verify_field_renders_a_verify_fence(self):
        text = (ROOT / "fork-files/.github/ISSUE_TEMPLATE/devin-task.yml").read_text()
        self.assertIn("render: verify", text)
        self.assertIn('labels: ["devin:proposed"]', text)  # template must never apply devin:ready
        self.assertNotIn('"devin:ready"', text)

    def test_workflow_name_matches_the_bots_default(self):
        from app.config import Settings

        wf = (ROOT / "fork-files/.github/workflows/devin-verify.yml").read_text()
        self.assertIn(f"name: {Settings().verify_workflow_name}", wf)
        # the untrusted issue text must never be interpolated into the workflow
        self.assertNotIn("github.event.issue.body", wf)
        self.assertNotIn("github.event.pull_request.title", wf)


if __name__ == "__main__":
    unittest.main()
