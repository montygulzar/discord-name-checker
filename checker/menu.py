"""Interactive numbered menu, shown when main.py runs with no arguments."""

from typing import Callable, Optional

LENGTHS = {
    "1": ("3", ["3"]),
    "2": ("4", ["4"]),
    "3": ("3 + 4", ["3", "4"]),
}

TYPES = {
    "1": ("Letters  (a-z)          -> L", "letters"),
    "2": ("Numbers  (0-9)          -> N", "numbers"),
    "3": ("Chars    (a-z 0-9 _ .)  -> C", "chars"),
    "4": ("Custom characters", "custom"),
    "5": ("Wordlist file (ignores length)", "wordlist"),
}


def choose(title: str, options: dict, ask: Callable[[str], str], out: Callable[[str], None]) -> str:
    out(f"\n{title}")
    for key, (label, _) in options.items():
        out(f"  {key}: {label}")
    while True:
        answer = ask("> ").strip()
        if answer in options:
            return answer
        out(f"Pick one of {', '.join(options)}.")


def yes_no(question: str, default: bool, ask: Callable[[str], str]) -> bool:
    hint = "Y/n" if default else "y/N"
    answer = ask(f"{question} [{hint}] ").strip().lower()
    return default if not answer else answer.startswith("y")


def build_argv(ask: Callable[[str], str] = input, out: Callable[[str], None] = print) -> Optional[list[str]]:
    """Ask the user what to check and return the equivalent CLI arguments."""
    out("=== Discord Name Checker ===")
    lengths = LENGTHS[choose("Length?", LENGTHS, ask, out)][1]
    kind = TYPES[choose("What type?", TYPES, ask, out)][1]

    argv: list[str] = []
    if kind == "wordlist":
        argv += ["--wordlist", ask("Wordlist path: ").strip()]
    else:
        argv += ["--length", *lengths, "--mode", kind]
        if kind == "custom":
            argv += ["--charset", ask("Characters to use: ").strip()]

    if yes_no("Random order?", True, ask):
        argv.append("--shuffle")
    if yes_no("Skip names checked in earlier runs?", True, ask):
        argv.append("--resume")
    return argv
