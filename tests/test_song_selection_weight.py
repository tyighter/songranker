import unittest

from app.main import POPULARITY_RATING_COUNT_CAP, _song_selection_weight
from app.models import Song


class SongSelectionWeightTests(unittest.TestCase):
    def _song(self, rating_count: int) -> Song:
        return Song(id=1, title="t", artist="a", plex_rating_count=rating_count)

    def test_representative_rating_counts_have_meaningful_separation(self):
        popularity_weight = 1.0
        rating_counts = [10, 100, 1_000, 10_000]

        weights = [_song_selection_weight(self._song(count), popularity_weight) for count in rating_counts]

        for previous, current in zip(weights, weights[1:]):
            self.assertLess(previous, current)

        self.assertGreater(weights[-1] - weights[0], 0.4)

    def test_representative_rating_counts_increase_monotonically_to_cap(self):
        popularity_weight = 1.0
        rating_counts = [
            0,
            10,
            100,
            1_000,
            10_000,
            100_000,
            POPULARITY_RATING_COUNT_CAP,
        ]

        weights = [_song_selection_weight(self._song(count), popularity_weight) for count in rating_counts]

        self.assertEqual(weights[0], 1.0)
        for previous, current in zip(weights, weights[1:]):
            self.assertLess(previous, current)

    def test_values_above_cap_saturate_at_cap_weight(self):
        popularity_weight = 1.0
        at_cap_weight = _song_selection_weight(self._song(POPULARITY_RATING_COUNT_CAP), popularity_weight)
        above_cap_weight = _song_selection_weight(self._song(POPULARITY_RATING_COUNT_CAP + 1), popularity_weight)
        far_above_cap_weight = _song_selection_weight(self._song(POPULARITY_RATING_COUNT_CAP + 10_000_000), popularity_weight)

        self.assertEqual(at_cap_weight, above_cap_weight)
        self.assertEqual(at_cap_weight, far_above_cap_weight)


if __name__ == "__main__":
    unittest.main()
