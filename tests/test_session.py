from __future__ import annotations

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from clipssh.session import SequenceCounter, SeenMessageCache


class SessionTests(unittest.TestCase):
    def test_sequence_counter_increments(self) -> None:
        seq = SequenceCounter()
        self.assertEqual(seq.next(), 1)
        self.assertEqual(seq.next(), 2)
        self.assertEqual(seq.next(), 3)

    def test_seen_message_cache_deduplicates(self) -> None:
        cache = SeenMessageCache(max_size=3)

        self.assertTrue(cache.mark("a"))
        self.assertTrue(cache.mark("b"))
        self.assertFalse(cache.mark("a"))

    def test_seen_message_cache_eviction(self) -> None:
        cache = SeenMessageCache(max_size=2)

        self.assertTrue(cache.mark("a"))
        self.assertTrue(cache.mark("b"))
        self.assertTrue(cache.mark("c"))  # "a" evicted
        self.assertTrue(cache.mark("a"))


if __name__ == "__main__":
    unittest.main()
