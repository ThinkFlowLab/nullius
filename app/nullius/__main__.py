"""``python -m nullius`` has the same commands as the installed executable."""
from nullius.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
