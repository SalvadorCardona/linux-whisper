"""The diagnostic's report: what is wrong, and the line that says how to fix it."""

from __future__ import annotations

import io
import unittest

from . import context  # noqa: F401

from whisper_desk.__main__ import _Report


class ReportTest(unittest.TestCase):
    def report(self) -> tuple[_Report, io.StringIO]:
        stream = io.StringIO()
        return _Report(stream), stream

    def test_a_success_is_one_line(self):
        report, stream = self.report()
        report.check("clipboard", True, "wl-copy", fix="install wl-clipboard")
        self.assertEqual(stream.getvalue(), "  ✓ clipboard — wl-copy\n")

    def test_a_failure_says_how_to_fix_it(self):
        report, stream = self.report()
        report.check("clipboard", False, "no clipboard tool", fix="install wl-clipboard")
        self.assertEqual(
            stream.getvalue().splitlines(),
            ["  ✗ clipboard — no clipboard tool", "      → install wl-clipboard"],
        )

    def test_an_optional_item_is_a_warning_not_a_problem(self):
        report, stream = self.report()
        report.check("NVIDIA GPU", False, fix="nothing to do", optional=True)
        report.summary()
        out = stream.getvalue()
        self.assertIn("⚠ NVIDIA GPU", out)
        self.assertIn("Everything is ready.", out)
        self.assertIn("1 optional item missing", out)

    def test_the_summary_counts_the_problems(self):
        report, stream = self.report()
        report.check("daemon", False, fix="systemctl --user start whisper-desk")
        report.check("socket", False)
        report.summary()
        self.assertIn("2 problems to fix", stream.getvalue())

    def test_no_colour_outside_a_terminal(self):
        report, stream = self.report()
        report.check("clipboard", False)
        self.assertNotIn("\033[", stream.getvalue())


if __name__ == "__main__":
    unittest.main()
