"""Compatibility entry point for the hourly routine: runs the maintenance cycle only
(identical to `python3 -m scripts.maintain`). No automatic trade decisions."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.maintain import main

if __name__ == "__main__":
    sys.exit(main())
