import os
import re
import subprocess
import tempfile
import unittest

INSTALL = os.path.join(os.path.dirname(__file__), "..", "install.sh")


def old_values(local_conf, unit):
    """Runs the two OLD_TZ / OLD_VT lines of install.sh against temp files (this is what a re-install reads)."""
    src = open(INSTALL).read()
    lines = [re.search(r"^%s=.*$" % name, src, re.M).group(0) for name in ("OLD_TZ", "OLD_VT")]
    with tempfile.TemporaryDirectory() as d:
        if local_conf is not None:
            open(os.path.join(d, "local.conf"), "w").write(local_conf)
        if unit is not None:
            open(os.path.join(d, "unit.service"), "w").write(unit)
        script = "set -euo pipefail\nUNITD=%s\nUNITF=%s\n%s\n%s\nprintf '%%s|%%s' \"$OLD_TZ\" \"$OLD_VT\"\n" % (
            d, os.path.join(d, "unit.service"), lines[0], lines[1])
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return tuple(r.stdout.split("|"))


class ReinstallKeepsChoices(unittest.TestCase):
    """A re-install must keep the time zone and the VT already chosen (a broken sed silently dropped them: the clock went UTC)."""

    def test_reads_zone_and_vt_from_the_drop_in(self):
        self.assertEqual(old_values("[Service]\nTTYPath=/dev/tty3\nEnvironment=TZ=Europe/Rome\n", None), ("Europe/Rome", "3"))

    def test_both_files_carry_the_zone_no_sigpipe_first_wins(self):
        conf = "[Service]\nEnvironment=TZ=Europe/Rome\n"
        self.assertEqual(old_values(conf, "[Service]\nEnvironment=TZ=Asia/Tokyo\n"), ("Europe/Rome", ""))

    def test_zone_from_the_old_unit_only(self):
        self.assertEqual(old_values("[Service]\nTTYPath=/dev/tty1\n", "[Service]\nEnvironment=TZ=Europe/Rome\n"), ("Europe/Rome", "1"))

    def test_nothing_installed_yet(self):
        self.assertEqual(old_values(None, None), ("", ""))


class ConfigStaysAndDistRefreshes(unittest.TestCase):
    def test_config_is_never_overwritten_but_the_dist_copy_is_refreshed(self):
        src = open(INSTALL).read()
        self.assertRegex(src, r"\[ -e /etc/nuc-console/config\.ini \] \|\| install .* /etc/nuc-console/config\.ini\b")
        self.assertIn("config/config.ini /etc/nuc-console/config.ini.dist", src)


if __name__ == "__main__":
    unittest.main()
