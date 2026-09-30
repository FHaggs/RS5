import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import fuzzer_demo


class FuzzerDemoTest(unittest.TestCase):
    def test_generate_suite_writes_two_riscof_tests(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            suite_dir = Path(temporary_directory)

            generated = fuzzer_demo.generate_suite(suite_dir)

            sources = sorted((suite_dir / "rv32i_m" / "I" / "src").glob("*.S"))
            self.assertEqual(
                ["demo-add.S", "demo-branch-memory.S"],
                [source.name for source in sources],
            )
            self.assertEqual(3, len(generated))
            for source in sources:
                assembly = source.read_text(encoding="ascii")
                self.assertIn('RVTEST_ISA("RV32I")', assembly)
                self.assertIn(
                    'RVTEST_CASE(0,"//check ISA:=regex(.*32.*);'
                    'check ISA:=regex(.*I.*);def TEST_CASE_1=True;",',
                    assembly,
                )
                self.assertIn("RVMODEL_DATA_BEGIN", assembly)
                self.assertIn("RVMODEL_HALT", assembly)

    def test_generate_suite_removes_stale_generated_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            suite_dir = Path(temporary_directory)
            source_dir = suite_dir / "rv32i_m" / "I" / "src"
            source_dir.mkdir(parents=True)
            stale_source = source_dir / "stale.S"
            stale_source.write_text("stale", encoding="ascii")

            fuzzer_demo.generate_suite(suite_dir)

            self.assertFalse(stale_source.exists())

    def test_build_riscof_command_uses_generated_environment(self) -> None:
        suite_dir = Path("/tmp/demo-suite")
        work_dir = Path("/tmp/demo-work")
        config = Path("/tmp/demo.ini")

        command = fuzzer_demo.build_riscof_command(suite_dir, work_dir, config)

        self.assertEqual("riscof", command[0])
        self.assertIn("--suite=/tmp/demo-suite", command)
        self.assertIn("--env=/tmp/demo-suite/env", command)
        self.assertIn("--work-dir=/tmp/demo-work", command)
        self.assertIn("--config=/tmp/demo.ini", command)

    @mock.patch.dict(os.environ, {"TRIPLET": "riscv64-unknown-elf"}, clear=True)
    def test_required_tools_honors_triplet(self) -> None:
        tools = fuzzer_demo.required_tools()

        self.assertIn("riscv64-unknown-elf-gcc", tools)
        self.assertIn("riscv64-unknown-elf-objcopy", tools)


if __name__ == "__main__":
    unittest.main()
