import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import svn_diff_check as tool


DIFF = "Index: app.xml\n===\n--- app.xml (old)\n+++ app.xml (new)\n@@ -1,2 +1,2 @@\n-old\n+new\n context\n"


class ParserTests(unittest.TestCase):
    def test_headers_and_context_excluded(self):
        self.assertEqual(tool.extract_changes(DIFF), ["-old", "+new"])

    def test_unknown_output_and_extra_changes_fail(self):
        for diff in ["localized binary notification\n", "@@ malformed @@\n-old\n+new\n", DIFF + "+extra\n", DIFF + "Property changes on: app.xml\n+property\n"]:
            with self.subTest(diff=diff), self.assertRaises(tool.CheckError):
                tool.extract_changes(diff)

    def test_unicode_separators_are_content(self):
        self.assertEqual(tool.extract_changes("@@ -1 +1 @@\n-a\u2028b\n+c\x85d\n"), ["-a\u2028b", "+c\x85d"])

    def test_header_like_content_duplicates_and_whitespace(self):
        diff = "@@ -1,2 +1,2 @@\n---literal  \n---literal  \n+++literal\n+++literal\n"
        self.assertEqual(tool.extract_changes(diff), ["---literal  ", "---literal  ", "+++literal", "+++literal"])

    def test_empty_and_multiple_hunks(self):
        self.assertEqual(tool.extract_changes(""), [])
        self.assertEqual(tool.extract_changes(DIFF + "@@ -5,0 +6 @@\n+added\n"), ["-old", "+new", "+added"])

    def test_no_final_newline_marker(self):
        self.assertEqual(tool.extract_changes("@@ -1 +1 @@\n-old\n\\ No newline at end of file\n+new\n\\ No newline at end of file\n"), ["-old", "+new"])

    def test_binary_and_truncated_hunks_fail(self):
        for diff in ["Cannot display: file marked as a binary type.\n", "Binary files a and b differ\n", "@@ -1 +1 @@\n-old\n", "@@ -1 +1 @@\n-invalid\n-extra\n"]:
            with self.subTest(diff=diff), self.assertRaises(tool.CheckError):
                tool.extract_changes(diff)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "expected").mkdir()
        self.expected = self.root / "expected" / "app.diff"
        self.expected.write_text("-old\n+new\n", encoding="utf-8")
        self.tsv = self.root / "checks.tsv"
        self.write_tsv(1)

    def write_tsv(self, count):
        self.tsv.write_text("staging_path\tproduction_path\texpected_path\n" + "https://example.test/stg/app.xml\thttps://example.test/prod/app.xml\texpected/app.diff\n" * count, encoding="utf-8-sig")

    def run_cli(self, result=None, error=None, extra=()):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(tool.subprocess, "run", return_value=result or subprocess.CompletedProcess([], 0, DIFF, ""), side_effect=error) as run:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = tool.main([str(self.tsv), *extra])
        return code, out.getvalue(), err.getvalue(), run

    def test_multiple_rows_relative_to_tsv_and_revision(self):
        self.write_tsv(2)
        code, out, err, run = self.run_cli(extra=["--revision", "123:456"])
        self.assertEqual(code, 0)
        self.assertIn("OK=2", out)
        self.assertEqual(run.call_count, 2)
        command = run.call_args.args[0]
        self.assertIn("--ignore-properties", command)
        self.assertIn("--non-interactive", command)
        self.assertIn("--internal-diff", command)
        self.assertEqual(command[command.index("--extensions") + 1], "")
        self.assertIn("123:456", command)
        self.assertIn("--old=https://example.test/stg/app.xml", command)
        self.assertEqual(run.call_args.kwargs["timeout"], 60)
        self.assertFalse(run.call_args.kwargs.get("shell", False))
        self.assertEqual(err, "")

    def test_mismatch_log_and_exit_one(self):
        self.expected.write_text("-other\n+new\n", encoding="utf-8")
        code, out, _, _ = self.run_cli()
        self.assertEqual(code, 1)
        self.assertIn("--- expected", out)
        self.assertIn("+++ actual", out)
        self.assertIn("--other", out)
        self.assertIn("+-old", out)

    def test_svn_error_stderr_and_continue(self):
        self.write_tsv(2)
        results = [subprocess.CompletedProcess([], 1, "", "authentication failed"), subprocess.CompletedProcess([], 0, DIFF, "")]
        code, out, err, run = self.run_cli(error=results)
        self.assertEqual(code, 2)
        self.assertEqual(run.call_count, 2)
        self.assertIn("authentication failed", err)
        self.assertIn("OK=1 NG=0 ERROR=1", out)

    def test_success_stderr_is_reported(self):
        code, _, err, _ = self.run_cli(subprocess.CompletedProcess([], 0, DIFF, "warning"))
        self.assertEqual(code, 0)
        self.assertIn("warning", err)

    def test_missing_executable_timeout_and_decode_error(self):
        for exc in [FileNotFoundError("svn missing"), subprocess.TimeoutExpired("svn", 60, stderr=b"slow server"), UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid")]:
            with self.subTest(exc=exc):
                code, _, err, _ = self.run_cli(error=exc)
                self.assertEqual(code, 2)
                self.assertIn("ERROR", err)

    def test_expected_missing_invalid_and_empty(self):
        self.expected.unlink()
        self.assertEqual(self.run_cli()[0], 2)
        self.expected.write_text("context\n", encoding="utf-8")
        self.assertEqual(self.run_cli()[0], 2)
        self.expected.write_text("", encoding="utf-8")
        self.assertEqual(self.run_cli(subprocess.CompletedProcess([], 0, "", ""))[0], 0)

    def test_unicode_expected_and_crlf(self):
        self.expected.write_bytes("-a\u2028b\r\n+c\x85d\r\n".encode("utf-8"))
        result = subprocess.CompletedProcess([], 0, "@@ -1 +1 @@\r\n-a\u2028b\r\n+c\x85d\r\n", "")
        self.assertEqual(self.run_cli(result)[0], 0)

    def test_unknown_svn_output_does_not_pass_empty_expected(self):
        self.expected.write_text("", encoding="utf-8")
        result = subprocess.CompletedProcess([], 0, "unsupported output\n", "")
        self.assertEqual(self.run_cli(result)[0], 2)

    def test_bad_tsv_and_urls_are_rejected_before_svn(self):
        header = "staging_path\tproduction_path\texpected_path\n"
        for content in ["bad\theader\n", header, header + "^/stg\thttps://example.test/prod\tx\n", header + "https://example.test/stg\t\tx\n", header + "a\tb\tc\td\n"]:
            with self.subTest(content=content):
                self.tsv.write_text(content, encoding="utf-8")
                code, _, _, run = self.run_cli()
                self.assertEqual(code, 2)
                run.assert_not_called()

    def test_error_takes_precedence_over_mismatch(self):
        self.write_tsv(2)
        results = [subprocess.CompletedProcess([], 0, "", ""), FileNotFoundError("missing")]
        self.assertEqual(self.run_cli(error=results)[0], 2)

    def test_peg_revisions_are_preserved(self):
        self.tsv.write_text("staging_path\tproduction_path\texpected_path\nhttps://example.test/stg@123\thttps://example.test/prod@456\texpected/app.diff\n", encoding="utf-8")
        run = self.run_cli()[3]
        self.assertIn("--new=https://example.test/prod@456", run.call_args.args[0])

    def test_single_revision_pins_both_sides(self):
        for revision in ("123", "HEAD"):
            with self.subTest(revision=revision):
                code, _, _, run = self.run_cli(extra=["--revision", revision])
                self.assertEqual(code, 0)
                command = run.call_args.args[0]
                self.assertEqual(command[command.index("--revision") + 1], f"{revision}:{revision}")

    def test_url_revision_conflicts_with_global_revision(self):
        self.tsv.write_text("staging_path\tproduction_path\texpected_path\nhttps://example.test/stg@123\thttps://example.test/prod\texpected/app.diff\n", encoding="utf-8")
        code, _, err, run = self.run_cli(extra=["--revision", "456"])
        self.assertEqual(code, 2)
        self.assertIn("Do not combine", err)
        run.assert_not_called()

    def test_escaped_at_sign_with_global_revision(self):
        self.tsv.write_text("staging_path\tproduction_path\texpected_path\nhttps://example.test/stg@name@\thttps://example.test/prod@name@\texpected/app.diff\n", encoding="utf-8")
        self.assertEqual(self.run_cli(extra=["--revision", "123"])[0], 0)

    def test_ssh_username_with_global_revision(self):
        staging = "svn+ssh://alice@example.test/repos/stg/app.xml"
        production = "svn+ssh://bob@example.test/repos/prod/app.xml"
        self.tsv.write_text(
            "staging_path\tproduction_path\texpected_path\n"
            f"{staging}\t{production}\texpected/app.diff\n", encoding="utf-8",
        )
        code, _, err, run = self.run_cli(extra=["--revision", "123"])
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        run.assert_called_once()
        command = run.call_args.args[0]
        self.assertIn("--old=" + staging, command)
        self.assertIn("--new=" + production, command)
        self.assertEqual(command[command.index("--revision") + 1], "123:123")

    def test_ssh_url_revision_conflicts_on_either_side(self):
        urls = ["svn+ssh://alice@example.test/repos/stg/app.xml",
                "svn+ssh://bob@example.test/repos/prod/app.xml"]
        for side in (0, 1):
            with self.subTest(side=side):
                targets = urls.copy()
                targets[side] += "@456"
                self.tsv.write_text(
                    "staging_path\tproduction_path\texpected_path\n"
                    f"{targets[0]}\t{targets[1]}\texpected/app.diff\n", encoding="utf-8",
                )
                code, _, err, run = self.run_cli(extra=["--revision", "123"])
                self.assertEqual(code, 2)
                self.assertIn("Do not combine", err)
                run.assert_not_called()

    def test_invalid_revision_encoding_and_missing_tsv(self):
        self.assertEqual(self.run_cli(extra=["--revision", "invalid"])[0], 2)
        self.assertEqual(self.run_cli(extra=["--encoding", "invalid-encoding"])[0], 2)
        self.tsv.unlink()
        self.assertEqual(self.run_cli()[0], 2)

    def test_non_text_codec_is_configuration_error(self):
        code, _, err, run = self.run_cli(extra=["--encoding", "base64_codec"])
        self.assertEqual(code, 2)
        self.assertIn("ERROR", err)
        run.assert_not_called()

    def test_real_child_process_cli_contract(self):
        # Python acts as the SVN executable and runs the temporary 'diff' script.
        # This exercises argument transport, decoding, logging, and process exits.
        fake_diff = self.root / "diff"
        fake_diff.write_text(
            "import sys\n"
            "sys.stdout.reconfigure(encoding='utf-8')\n"
            "sys.stderr.reconfigure(encoding='utf-8')\n"
            "args = sys.argv[1:]\n"
            "assert args[args.index('--extensions') + 1] == ''\n"
            "assert args[args.index('--revision') + 1] == '123:123'\n"
            "print('@@ -1 +1 @@')\n"
            "print('-古い値  ')\n"
            "print('+新しい値')\n"
            "print('warning from child', file=sys.stderr)\n",
            encoding="utf-8",
        )
        script = Path(tool.__file__).resolve()
        for expected, exit_code in [("-古い値  \n+新しい値\n", 0), ("", 1)]:
            with self.subTest(exit_code=exit_code):
                self.expected.write_text(expected, encoding="utf-8")
                result = subprocess.run(
                    [sys.executable, "-X", "utf8", str(script), str(self.tsv),
                     "--svn", sys.executable, "--revision", "123"],
                    cwd=self.root, capture_output=True, text=True, encoding="utf-8", timeout=10,
                )
                self.assertEqual(result.returncode, exit_code, result.stderr)
                self.assertIn("warning from child", result.stderr)
                self.assertIn("SUMMARY:", result.stdout)
        fake_diff.write_text("import sys\nprint('repository unavailable', file=sys.stderr)\nsys.exit(7)\n", encoding="utf-8")
        result = subprocess.run(
            [sys.executable, "-X", "utf8", str(script), str(self.tsv), "--svn", sys.executable],
            cwd=self.root, capture_output=True, text=True, encoding="utf-8", timeout=10,
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("SVN exit 7", result.stderr)
        self.assertIn("repository unavailable", result.stderr)


if __name__ == "__main__":
    unittest.main()
