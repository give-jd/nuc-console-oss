"""A KPI whose feature is switched off in config.ini ([features] name = no) is never a figure that reads fine.

The firewall KPI answers '-' (info, "switched off in config.ini"); the others must say the same, not "0/0 ok" or a green count read off nothing.
Every fixture is written by hand (no host state).
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hermetic  # noqa: E402,F401  (first: the host's state stays out of the tests)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import cards  # noqa: E402
import nuc_config  # noqa: E402

OFF_HINT = "switched off in config.ini"
# the collector's answer for [features] containers = no (collector.collect), and what the console still hands over for health
CONT_OFF = {"ts": 0, "containers": [], "disabled": True}
HEALTH_OLD = {"report": {"findings": []}}  # an old history file read although the feature is off: zero findings, from before


def _cfg(feature):
    cfg = nuc_config.load("/nonexistent")
    cfg["features"][feature] = False
    return cfg


class KpiFeatureOffTests(unittest.TestCase):
    def test_a_feature_that_is_off_never_reads_fine(self):
        for feature, kpi_id, ctx_args in (
            ("containers", "containers", {"cont": CONT_OFF}),
            ("containers", "unhealthy", {"cont": CONT_OFF}),
            ("health", "health", {"health": HEALTH_OLD}),
        ):
            with self.subTest(feature=feature, kpi=kpi_id):
                ctx = cards.Ctx(cfg=_cfg(feature), now=0, **ctx_args)
                (k,) = cards.kpis(ctx, [kpi_id])
                self.assertEqual((k.value, k.unit, k.state, k.hint), ("-", "", "info", OFF_HINT))

    def test_a_feature_that_is_on_still_counts(self):
        cont = {"ts": 0, "containers": [{"name": "web", "state": "running", "status": "Up 2 hours (unhealthy)"}]}
        ctx = cards.Ctx(cfg=nuc_config.load("/nonexistent"), cont=cont, health=HEALTH_OLD, now=0)
        got = {k.id: (k.value, k.state) for k in cards.kpis(ctx, ["containers", "unhealthy", "health"])}
        self.assertEqual(got, {"containers": ("1", "err"), "unhealthy": ("1", "err"), "health": ("0", "ok")})


if __name__ == "__main__":
    unittest.main()
