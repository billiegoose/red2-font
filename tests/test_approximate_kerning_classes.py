import contextlib
import io
import random
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.analyze_kerning import dependent_triplets, widen_pairs
from src.approximate_kerning_classes import (
    STRATEGIES, compress_classes, exact_classes, experiment, incompatibility_clique,
    build_parser, main, prepare_baseline, select_candidate, verify_required_history,
)


class ApproximateKerningClassTests(unittest.TestCase):
    @staticmethod
    def candidate(left, right, histogram, valid=True):
        return {"left": left, "right": right, "cells": left * right,
                "extra_pixel_histogram": {str(k): v for k, v in histogram.items()},
                "max_added_pixels": max(histogram), "history_valid": valid,
                "history_failure": "counterexample"}

    def test_prefers_29_by_29_over_30_by_27_for_the_same_padded_rom(self):
        variants = [self.candidate(30, 27, {0: 6449, 1: 603, 2: 173}),
                    self.candidate(29, 29, {0: 6560, 1: 540, 2: 125})]
        self.assertEqual(select_candidate(variants), 1)

    def test_worst_pair_counts_take_priority_over_total_widening(self):
        # 100px total with no 2px errors beats 40px total with twenty 2px errors.
        variants = [self.candidate(29, 29, {0: 0, 1: 100}),
                    self.candidate(30, 27, {0: 80, 2: 20})]
        self.assertEqual(select_candidate(variants), 0)

    def test_smaller_padded_rom_takes_priority_over_error_counts(self):
        variants = [self.candidate(29, 29, {0: 100, 2: 125}),
                    self.candidate(33, 31, {0: 225})]
        self.assertEqual(select_candidate(variants), 0)

    def test_invalid_candidate_cannot_win_and_equal_scores_are_stable(self):
        variants = [self.candidate(1, 1, {0: 100}, valid=False),
                    self.candidate(29, 29, {0: 90, 1: 10}),
                    self.candidate(29, 29, {0: 90, 1: 10})]
        self.assertEqual(select_candidate(variants), 1)

    def test_official_slack_option_defaults_to_two_and_accepts_old_alias(self):
        parser = build_parser()
        self.assertEqual(parser.parse_args([]).kerning_slack, 2)
        self.assertEqual(parser.parse_args(["--kerning-slack", "2"]).kerning_slack, 2)
        self.assertEqual(parser.parse_args(["--extra-space", "2"]).kerning_slack, 2)

    def test_negative_slack_is_rejected_before_loading_the_font(self):
        with patch("sys.argv", ["approximate_kerning_classes.py", "--kerning-slack", "-1"]):
            with patch("src.approximate_kerning_classes.load_glyphs") as load:
                with contextlib.redirect_stderr(io.StringIO()) as errors:
                    with self.assertRaises(SystemExit) as exit_status:
                        main()
                self.assertEqual(exit_status.exception.code, 2)
                self.assertIn("--kerning-slack must be nonnegative", errors.getvalue())
                load.assert_not_called()

    def test_zero_tolerance_reconstructs_exact_table(self):
        table = {0: {0: 0, 1: 1, 2: 0},
                 1: {0: 0, 1: 1, 2: 0},
                 2: {0: 2, 1: 3, 2: 2}}
        classes = compress_classes(table, 0)
        self.assertEqual(classes.expand(), table)
        self.assertEqual((len(classes.left), len(classes.right)), (2, 2))

    def test_joint_merges_do_not_accumulate_beyond_one_pixel(self):
        # Each pair of rows and each pair of columns differs by only one,
        # but merging BOTH axes into one block would span two pixels.
        table = {0: {0: 0, 1: 1}, 1: {0: 1, 1: 2}}
        for strategy in STRATEGIES:
            with self.subTest(strategy=strategy):
                classes = compress_classes(table, 1, strategy)
                self.assertGreater(len(classes.left) * len(classes.right), 1)
                expanded = classes.expand()
                for a, row in table.items():
                    for b, value in row.items():
                        self.assertIn(value - expanded[a][b], (0, 1))

    def test_compatible_neighbors_do_not_make_a_compatible_whole_group(self):
        table = {a: dict.fromkeys(range(3), a) for a in range(3)}
        expanded = compress_classes(table, 1).expand()
        self.assertGreater(len(exact_classes(expanded)[0]), 1)
        for a in table:
            for b in table:
                self.assertIn(table[a][b] - expanded[a][b], (0, 1))

    def test_protected_pairs_are_exact(self):
        table = {0: {0: 1, 1: 0}, 1: {0: 1, 1: 1}}
        classes = compress_classes(table, 1, protected={(1, 1)})
        self.assertEqual(classes.expand()[1][1], 1)

    def test_input_order_does_not_affect_classes_or_values(self):
        rng = random.Random(0)
        codes = list(range(6))
        table = {a: {b: rng.randrange(3) for b in codes} for a in codes}
        for strategy in STRATEGIES:
            expected = compress_classes(table, 1, strategy)
            for _ in range(12):
                rng.shuffle(codes)
                shuffled = {}
                for a in codes:
                    columns = list(table[a])
                    rng.shuffle(columns)
                    shuffled[a] = {b: table[a][b] for b in columns}
                self.assertEqual(compress_classes(shuffled, 1, strategy), expected)

    def test_conservative_class_aggregation_preserves_triangle_invariant(self):
        rng = random.Random(1)
        codes = list(range(5))
        matrices = [{a: {b: max(b - a, 0) for b in codes} for a in codes}]
        matrices += [{a: {b: rng.randrange(6) for b in codes} for a in codes} for _ in range(15)]
        for matrix in matrices:
            baseline = widen_pairs(matrix, dict.fromkeys(codes, 5))
            adjustments = {a: {b: 5 - baseline[a][b] for b in codes} for a in codes}
            for strategy in STRATEGIES:
                compressed = compress_classes(adjustments, 1, strategy).expand()
                proposed = {a: {b: 5 - compressed[a][b] for b in codes} for a in codes}
                self.assertEqual(list(dependent_triplets(proposed)), [])
                for a in codes:
                    for b in codes:
                        self.assertIn(proposed[a][b] - baseline[a][b], (0, 1))

    def test_incompatibility_certificate_gives_a_valid_lower_bound(self):
        table = {a: {b: b * 2 for b in range(3)} for a in range(3)}
        left = incompatibility_clique(table, 1, 0)
        right = incompatibility_clique(table, 1, 1)
        self.assertEqual(len(left), 1)
        self.assertEqual(set(right), {0, 1, 2})
        for a in right:
            for b in right:
                if a != b:
                    self.assertTrue(any(abs(table[x][a] - table[x][b]) > 1 for x in table))

    def test_negative_tolerance_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "nonnegative"):
            compress_classes({0: {0: 0}}, -1)

    def test_widening_is_opt_in_and_never_mutates_original(self):
        table = {0: {0: 4, 1: 1, 2: 4},
                 1: {0: 4, 1: 4, 2: 2},
                 2: {0: 4, 1: 4, 2: 4}}
        nominal = dict.fromkeys(table, 4)
        shuffled = {a: {b: table[a][b] for b in reversed(table)} for a in reversed(table)}
        baseline = prepare_baseline(shuffled, nominal)
        self.assertEqual(baseline, table)
        self.assertEqual(list(baseline), sorted(table))
        self.assertIsNot(baseline[0], shuffled[0])
        widened = prepare_baseline(shuffled, nominal, widen=True)
        self.assertEqual(table[1][2], 2)
        self.assertEqual(shuffled[1][2], 2)
        self.assertEqual(widened[1][2], 3)
        self.assertEqual(list(dependent_triplets(widened)), [])

    def test_two_glyph_mode_allows_triplets_that_need_the_older_glyph(self):
        table = {0: {0: 4, 1: 1, 2: 4},
                 1: {0: 4, 1: 4, 2: 2},
                 2: {0: 4, 1: 4, 2: 4}}
        with self.assertRaisesRegex(ValueError, "One-glyph history is insufficient"):
            verify_required_history(table, 1)
        self.assertEqual(verify_required_history(table, 2), 3 ** 4)
        with tempfile.TemporaryDirectory() as directory:
            with patch("src.approximate_kerning_classes.verify_required_history",
                       wraps=verify_required_history) as verifier:
                result, proposed = experiment("test", table, dict.fromkeys(table, 4),
                                               0, Path(directory))
                # All five strategies give the same table; prove it once.
                self.assertEqual(verifier.call_count, 1)
            self.assertEqual(proposed, table)
            self.assertFalse(result["widen_pairs"])
            self.assertEqual(result["required_history_glyphs"], 2)
            self.assertEqual(result["chosen"]["dependent_triplets"], 1)
            self.assertTrue(result["chosen"]["history_valid"])

    def test_bitset_history_proof_matches_exhaustive_reference(self):
        rng = random.Random(183)
        for trial in range(250):
            codes = range(rng.randrange(1, 7))
            table = {a: {b: rng.randrange(-3, 15) for b in codes} for a in codes}
            witness = next(((a, b, c, d)
                            for a in codes for b in codes for c in codes for d in codes
                            if table[a][d] > max(table[a][b] + table[b][d],
                                max(table[a][c], table[a][b] + table[b][c]) + table[c][d])), None)
            with self.subTest(trial=trial):
                if witness is None:
                    self.assertEqual(verify_required_history(table, 2), len(table) ** 4)
                else:
                    with self.assertRaises(ValueError) as error:
                        verify_required_history(table, 2)
                    self.assertIn(repr(''.join(map(chr, witness))), str(error.exception))

    def test_no_candidate_is_exported_when_two_glyph_proof_fails(self):
        # A still constrains D after two intermediate zero-advance glyphs.
        table = {a: dict.fromkeys(range(4), 0) for a in range(4)}
        table[0][3] = 2
        with self.assertRaisesRegex(ValueError, "Two-glyph history is insufficient"):
            verify_required_history(table, 2)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            with self.assertRaisesRegex(ValueError, "No approximate candidate"):
                experiment("test", table, dict.fromkeys(table, 4), 0, output)
            self.assertEqual(list(output.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
