import unittest

from checker.menu import build_argv


def run(answers):
    answers = iter(answers)
    output = []
    argv = build_argv(ask=lambda prompt: next(answers), out=output.append)
    return argv, output


class MenuTests(unittest.TestCase):
    def test_3_letters_defaults(self):
        argv, _ = run(["1", "1", "", ""])
        self.assertEqual(argv, ["--length", "3", "--mode", "letters", "--shuffle", "--resume"])

    def test_4_numbers_no_extras(self):
        argv, _ = run(["2", "2", "n", "n"])
        self.assertEqual(argv, ["--length", "4", "--mode", "numbers"])

    def test_both_lengths_chars(self):
        argv, _ = run(["3", "3", "n", "y"])
        self.assertEqual(argv, ["--length", "3", "4", "--mode", "chars", "--resume"])

    def test_custom_charset(self):
        argv, _ = run(["1", "4", "xyz", "n", "n"])
        self.assertEqual(argv, ["--length", "3", "--mode", "custom", "--charset", "xyz"])

    def test_wordlist(self):
        argv, _ = run(["1", "5", "names.txt", "n", "n"])
        self.assertEqual(argv, ["--wordlist", "names.txt"])

    def test_reprompts_on_bad_choice(self):
        argv, output = run(["9", "1", "2", "n", "n"])
        self.assertEqual(argv, ["--length", "3", "--mode", "numbers"])
        self.assertTrue(any("Pick one of" in line for line in output))


if __name__ == "__main__":
    unittest.main()
