import importlib.util
import sys
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "lidarr-discogs-genres-helper.py"
SPEC = importlib.util.spec_from_file_location("lidarr_discogs_genres_helper", SCRIPT)
helper = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = helper
SPEC.loader.exec_module(helper)


class LidarrDiscogsGenresHelperTests(unittest.TestCase):
    def test_scaffold_never_claims_recording_level_genres(self):
        self.assertNotIn("recording", helper.PLAN["musicbrainz_targets"])
        self.assertIn("Do not infer", helper.PLAN["recording_policy"])

    def test_identity_plan_does_not_accept_name_only_matches(self):
        automatic_rules = helper.PLAN["identity_order"][:-1]
        self.assertTrue(all("artist/title search" not in rule for rule in automatic_rules))
        self.assertIn("review-only", helper.PLAN["identity_order"][-1])

    def test_rendered_plan_makes_non_operational_status_clear(self):
        rendered = helper.render_plan(helper.PLAN)
        self.assertIn("scaffold", rendered)
        self.assertIn("cannot submit data", rendered)


if __name__ == "__main__":
    unittest.main()
