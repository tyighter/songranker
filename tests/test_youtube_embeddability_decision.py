import unittest
from unittest.mock import Mock, patch

from app.main import (
    YOUTUBE_CONFIDENCE_KNOWN_NON_EMBEDDABLE,
    YOUTUBE_CONFIDENCE_UNVERIFIED,
    YOUTUBE_CONFIDENCE_VERIFIED,
    YouTubeDataApiSearchProvider,
    YouTubeLookupError,
    _fetch_first_youtube_video,
)


class _StaticProvider:
    name = "static_provider"

    def __init__(self, broad_results):
        self._broad_results = broad_results

    def search(self, query: str, *, embeddable_only: bool):
        if embeddable_only:
            return []
        return self._broad_results


class YouTubeEmbeddabilityDecisionTests(unittest.TestCase):
    def _run_lookup_with_broad_results(self, broad_results):
        with (
            patch("app.main._get_cached_youtube_lookup", return_value=None),
            patch("app.main._get_persistent_youtube_lookup_cache", return_value=None),
            patch("app.main._set_cached_youtube_lookup"),
            patch("app.main._set_persistent_youtube_lookup_cache"),
            patch("app.main._make_youtube_provider_chain", return_value=[_StaticProvider(broad_results)]),
        ):
            return _fetch_first_youtube_video(Mock(), "Song", "Artist")

    def test_unverified_candidate_raises_embeddability_unverified(self):
        broad_results = [
            {
                "video_id": "aaaaaaaaaaa",
                "embeddability_confidence": YOUTUBE_CONFIDENCE_UNVERIFIED,
                "provider": "static_provider",
            }
        ]

        with self.assertRaises(YouTubeLookupError) as ctx:
            self._run_lookup_with_broad_results(broad_results)

        self.assertEqual(ctx.exception.code, "video_embeddability_unverified")

    def test_known_non_embeddable_candidate_raises_video_not_embeddable(self):
        broad_results = [
            {
                "video_id": "bbbbbbbbbbb",
                "embeddability_confidence": YOUTUBE_CONFIDENCE_KNOWN_NON_EMBEDDABLE,
                "provider": "static_provider",
            }
        ]

        with self.assertRaises(YouTubeLookupError) as ctx:
            self._run_lookup_with_broad_results(broad_results)

        self.assertEqual(ctx.exception.code, "video_not_embeddable")

    def test_known_non_embeddable_takes_precedence_over_unverified(self):
        broad_results = [
            {
                "video_id": "ccccccccccc",
                "embeddability_confidence": YOUTUBE_CONFIDENCE_UNVERIFIED,
                "provider": "static_provider",
            },
            {
                "video_id": "ddddddddddd",
                "embeddability_confidence": YOUTUBE_CONFIDENCE_KNOWN_NON_EMBEDDABLE,
                "provider": "static_provider",
            },
        ]

        with self.assertRaises(YouTubeLookupError) as ctx:
            self._run_lookup_with_broad_results(broad_results)

        self.assertEqual(ctx.exception.code, "video_not_embeddable")


class YouTubeDataApiSearchProviderConfidenceTests(unittest.TestCase):
    def test_search_marks_known_non_embeddable_when_status_false(self):
        provider = YouTubeDataApiSearchProvider("fake-key")
        with patch(
            "app.main._read_json_from_url",
            side_effect=[
                {
                    "items": [
                        {"id": {"videoId": "eeeeeeeeeee"}},
                        {"id": {"videoId": "ffffffffffF"}},
                    ]
                },
                {
                    "items": [
                        {"id": "eeeeeeeeeee", "status": {"embeddable": False}},
                        {"id": "ffffffffffF", "status": {"embeddable": True}},
                    ]
                },
            ],
        ):
            candidates = provider.search("song artist", embeddable_only=False)

        confidence_by_id = {c["video_id"]: c["embeddability_confidence"] for c in candidates}
        self.assertEqual(confidence_by_id["eeeeeeeeeee"], YOUTUBE_CONFIDENCE_KNOWN_NON_EMBEDDABLE)
        self.assertEqual(confidence_by_id["ffffffffffF"], YOUTUBE_CONFIDENCE_VERIFIED)


if __name__ == "__main__":
    unittest.main()
