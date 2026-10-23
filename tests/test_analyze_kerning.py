import random
import unittest
from itertools import permutations
from pathlib import Path

from src.analyze_kerning import TableKerning, dependent_triplets, widen_pairs
from src.web_font import build_kerning_states
from src.font import AutoKerning, layout_line, load_glyphs


class PairOnlyAnalysisTests(unittest.TestCase):
    def test_reports_strict_violations_but_not_ties(self):
        table = {0: {0: 4, 1: 1, 2: 4},
                 1: {0: 4, 1: 4, 2: 2},
                 2: {0: 4, 1: 4, 2: 4}}
        self.assertEqual(list(dependent_triplets(table)), [(0, 1, 2, 1)])
        table[1][2] = 3
        self.assertEqual(list(dependent_triplets(table)), [])

    def test_repair_does_not_mutate_input_and_is_idempotent(self):
        table = {0: {0: 4, 1: 1, 2: 4},
                 1: {0: 4, 1: 4, 2: 2},
                 2: {0: 4, 1: 4, 2: 4}}
        nominal = dict.fromkeys(table, 4)
        repaired = widen_pairs(table, nominal)
        self.assertEqual(table[1][2], 2)
        self.assertEqual(repaired[1][2], 3)
        self.assertEqual(list(dependent_triplets(repaired)), [])
        self.assertEqual(widen_pairs(repaired, nominal), repaired)

    def test_rejects_invalid_nominal_bounds(self):
        with self.assertRaisesRegex(ValueError, "nominal bounds"):
            widen_pairs({0: {0: 5}}, {0: 4})

    def test_widening_uses_sorted_glyphs_regardless_of_insertion_order(self):
        # This table has different feasible results under forward and reverse
        # sweeps. Lock down ascending-key sweeps, not just triangle safety.
        table = {0: {0: 3, 1: 3, 2: 0, 3: 2},
                 1: {0: 4, 1: 3, 2: 3, 3: 2},
                 2: {0: 3, 1: 2, 2: 4, 3: 1},
                 3: {0: 4, 1: 1, 2: 2, 3: 1}}
        expected = {0: {0: 3, 1: 3, 2: 1, 3: 2},
                    1: {0: 4, 1: 3, 2: 3, 3: 2},
                    2: {0: 3, 1: 3, 2: 4, 3: 2},
                    3: {0: 4, 1: 1, 2: 2, 3: 1}}
        for outer in permutations(table):
            for inner in permutations(table):
                with self.subTest(outer=outer, inner=inner):
                    shuffled = {a: {b: table[a][b] for b in inner} for a in outer}
                    actual = widen_pairs(shuffled, dict.fromkeys(outer, 5))
                    self.assertEqual(actual, expected)
                    self.assertEqual(list(actual), sorted(table))
                    for row in actual.values():
                        self.assertEqual(list(row), sorted(table))

    def test_real_artwork_needs_only_base_states_and_adjacent_pairs(self):
        glyphs = load_glyphs(Path(__file__).resolve().parents[1] / "RED2 Font.aseprite", "aseprite")
        original = AutoKerning(glyphs)
        codes = sorted(glyphs)
        table = {a: {b: original.advance(a, b) for b in codes} for a in codes}
        widened = widen_pairs(table, {a: original.nominal_advance(a) for a in codes})
        self.assertEqual(list(dependent_triplets(widened)), [])
        candidate = TableKerning(original, widened)
        base, states, _ = build_kerning_states(glyphs, candidate)
        self.assertEqual(set(states), set(base.values()))
        for a in codes:
            for b in codes:
                self.assertGreaterEqual(widened[a][b], table[a][b])
                self.assertLessEqual(widened[a][b], original.nominal_advance(a))
                if a in glyphs.fixed_advances or b in glyphs.fixed_advances:
                    self.assertEqual(widened[a][b], table[a][b])
        rng = random.Random(0)
        for _ in range(2000):
            text = rng.choices(codes, k=rng.randrange(1, 80))
            adjacent = [(text[0], 0)]
            for a, b in zip(text, text[1:]):
                adjacent.append((b, adjacent[-1][1] + widened[a][b]))
            # Independent, unbounded lookbehind checks every prior constraint.
            full = []
            for code in text:
                x = max((left_x + widened[left][code] for left, left_x in full), default=0)
                full.append((code, x))
            self.assertEqual(adjacent, full)
            self.assertEqual(adjacent, layout_line(text, candidate))


if __name__ == "__main__":
    unittest.main()
