"""The verify workflow lives in fork-files/ and is copied into the fork. It is not exercised by the other
tests, and a YAML mistake once broke it silently (an unquoted " #" starts a comment), so check the file itself."""
import pathlib
import re
import unittest

WORKFLOW = pathlib.Path(__file__).resolve().parents[1] / "fork-files/.github/workflows/devin-verify.yml"


class WorkflowFileTests(unittest.TestCase):
    def setUp(self):
        self.text = WORKFLOW.read_text()

    def test_no_unquoted_value_contains_a_space_hash(self):
        # In YAML, a space followed by # inside an unquoted value starts a comment and truncates the value.
        for n, line in enumerate(self.text.splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            m = re.match(r"^\s*(?:- )?[\w.-]+:\s+(\S.*)$", line)
            if not m:
                continue
            value = m.group(1)
            if value[0] in "\"'|>[{":
                continue
            self.assertNotIn(" #", value, f"line {n}: unquoted value contains ' #' (starts a YAML comment): {line.strip()}")

    def test_the_job_condition_is_a_complete_expression(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML is not installed")
        cond = yaml.safe_load(self.text)["jobs"]["verify"]["if"]
        self.assertEqual(cond.count("'") % 2, 0, cond)
        self.assertEqual(cond.count("("), cond.count(")"), cond)
        self.assertTrue(cond.rstrip().endswith(")"), cond)

    def test_the_workflow_name_matches_the_bots_default(self):
        self.assertRegex(self.text, r"(?m)^name: devin-verify$")


if __name__ == "__main__":
    unittest.main()
