import os
import tempfile
import unittest

from checker.generator import CHARSETS, classify, generate, is_valid, load_wordlist


class GeneratorTests(unittest.TestCase):
    def test_letters_3(self):
        names = list(generate([3], CHARSETS["letters"]))
        self.assertEqual(len(names), 26 ** 3)
        self.assertEqual(names[0], "aaa")

    def test_numbers_3_and_4(self):
        self.assertEqual(len(list(generate([3], CHARSETS["numbers"]))), 10 ** 3)
        self.assertEqual(len(list(generate([4], CHARSETS["numbers"]))), 10 ** 4)

    def test_chars_3_excludes_double_period(self):
        names = list(generate([3], CHARSETS["chars"]))
        # 38^3 minus names containing ".." (".." + x, x + "..", "..." counted once)
        self.assertEqual(len(names), 38 ** 3 - (38 + 38 - 1))
        self.assertNotIn("a..", names)
        self.assertIn("a.b", names)

    def test_multiple_lengths_and_shuffle(self):
        names = list(generate([3, 4], CHARSETS["numbers"], shuffle=True))
        self.assertEqual(len(names), 1000 + 10000)
        self.assertEqual(len(set(names)), len(names))

    def test_is_valid(self):
        for good in ["abc", "a_b", "a.b", "1234", "ab"]:
            self.assertTrue(is_valid(good), good)
        for bad in ["a", "ABC", "a..b", "a-b", "a b", "x" * 33]:
            self.assertFalse(is_valid(bad), bad)

    def test_classify(self):
        self.assertEqual(classify("abc"), "3L")
        self.assertEqual(classify("1234"), "4N")
        self.assertEqual(classify("a1_"), "3C")

    def test_load_wordlist(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write("ABC\nabc\n\nbad name\nx..y\n123\n")
        try:
            self.assertEqual(load_wordlist(f.name), ["abc", "123"])
        finally:
            os.unlink(f.name)


if __name__ == "__main__":
    unittest.main()
