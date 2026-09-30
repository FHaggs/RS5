import contextlib
import io
import tempfile
import unittest
from pathlib import Path

import signature_diff


class SignatureDiffTest(unittest.TestCase):
    def write_signature(self, directory: Path, name: str, content: str) -> Path:
        path = directory / name
        path.write_text(content, encoding="ascii")
        return path

    def run_main(self, *arguments: str) -> tuple[int, str]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = signature_diff.main(arguments)
        return result, output.getvalue()

    def test_matching_signatures_show_all_number_formats(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            left = self.write_signature(directory, "dut.signature", "ffffffff\n2a\n")
            right = self.write_signature(directory, "ref.signature", "ffffffff\n2a\n")

            result, output = self.run_main(str(left), str(right))

            self.assertEqual(0, result)
            self.assertIn("0xffffffff", output)
            self.assertIn("4294967295", output)
            self.assertIn("-1", output)
            self.assertIn("0x0000002a", output)
            self.assertIn("Signatures match.", output)

    def test_different_and_missing_words_are_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            left = self.write_signature(directory, "dut.signature", "1\n2\n")
            right = self.write_signature(directory, "ref.signature", "1\n3\n4\n")

            result, output = self.run_main(str(left), str(right))

            self.assertEqual(1, result)
            self.assertEqual(1, output.count("MATCH"))
            self.assertEqual(2, output.count("DIFF"))
            self.assertIn("<missing>", output)
            self.assertIn("Signatures differ.", output)

    def test_invalid_hexadecimal_returns_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            left = self.write_signature(directory, "dut.signature", "not-hex\n")
            right = self.write_signature(directory, "ref.signature", "0\n")

            result, output = self.run_main(str(left), str(right))

            self.assertEqual(2, result)
            self.assertIn("invalid hexadecimal word", output)


if __name__ == "__main__":
    unittest.main()
