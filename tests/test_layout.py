"""The code layout: one configuration dict per process, and the flat modules render.py's pieces were moved to
(ui.py, ansi.py, exposure.py): what each holds, and that render.py's re-exports are the very same objects."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ["NUC_CONSOLE_CONFIG"] = "/nonexistent"
import nuc_config  # noqa: E402
import render  # noqa: E402


class SharedConfig(unittest.TestCase):
    def test_current_is_loaded_once_and_always_the_same_object(self):
        self.assertIs(nuc_config.current(), nuc_config.current())

    def test_render_cfg_is_that_object(self):
        # the tests change render.CFG in place (~150 places): the other modules see it because it is one dict
        self.assertIs(render.CFG, nuc_config.current())
        render.CFG["spacing"], saved = 7, render.CFG["spacing"]
        try:
            self.assertEqual(nuc_config.current()["spacing"], 7)
        finally:
            render.CFG["spacing"] = saved

    def test_load_still_reads_the_file_again(self):
        a, b = nuc_config.load("/nonexistent"), nuc_config.load("/nonexistent")
        self.assertIsNot(a, b)
        self.assertIsNot(a, nuc_config.current())
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
