import sys

from checker.cli import main
from checker.menu import build_argv

if __name__ == "__main__":
    # No arguments: show the numbered menu. Arguments: run as a normal CLI.
    if len(sys.argv) > 1:
        sys.exit(main())
    try:
        argv = build_argv()
    except (KeyboardInterrupt, EOFError):
        sys.exit(0)
    sys.exit(main(argv))
