"""Run the end-to-end test of the bridge on its own (about 40 seconds).

    <the Python AstrBot runs on> tests/run_e2e.py

It starts a real core process (``python -m st_bridge_core``) in a temporary data directory on free ports and
drives it with a fake ST extension, a fake chat connector and a fake TTS server; nothing outside this machine's
loopback and the temporary directory is touched, and everything is removed at the end. Needs aiohttp, numpy and
httpx (the AstrBot instance's Python has them).
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def main() -> int:
    suite = unittest.defaultTestLoader.loadTestsFromName("test_e2e")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
