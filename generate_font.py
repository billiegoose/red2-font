#!./.venv/bin/python3
"""Generate RED2 assets; --export v2 writes a flashable header + BRAM32 binary."""

from src.generate import main

if __name__ == "__main__":
    main()
