"""Allow running pihello as ``python -m pihello``."""

from pihello.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
