"""Every console block in README.md must match what the scripts really print."""
import io
import os
import shlex
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "skills" / "llm-cost-audit"
sys.path.insert(0, str(SKILL / "scripts"))

import prefix_stability  # noqa: E402
import usage_report  # noqa: E402

SCRIPTS = {"scripts/usage_report.py": usage_report, "scripts/prefix_stability.py": prefix_stability}


def console_blocks(text):
    """Yield (command, expected output) for each ```console block."""
    block = None
    for line in text.splitlines():
        if line.strip() == "```console":
            block = []
        elif line.strip() == "```" and block is not None:
            yield block[0][2:], "\n".join(block[1:])
            block = None
        elif block is not None:
            block.append(line)


def run_in_skill_folder(command):
    words = shlex.split(command)
    module = SCRIPTS[words[1]]
    buffer = io.StringIO()
    previous = os.getcwd()
    os.chdir(str(SKILL))
    try:
        with redirect_stdout(buffer), redirect_stderr(buffer):
            module.main(words[2:])
    finally:
        os.chdir(previous)
    return buffer.getvalue().rstrip("\n")


class ReadmeExampleTests(unittest.TestCase):
    def test_console_examples_match_real_output(self):
        blocks = list(console_blocks((ROOT / "README.md").read_text(encoding="utf-8")))
        self.assertGreaterEqual(len(blocks), 3)
        for command, expected in blocks:
            self.assertTrue(command.startswith("python3 scripts/"), command)
            self.assertEqual(run_in_skill_folder(command), expected, command)


if __name__ == "__main__":
    unittest.main()
