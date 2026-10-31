"""zenobfus — obfus file browser (zenteru plugin entry point)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import zenteru  # noqa: E402

from zenobfus.plugin import main  # noqa: E402

if __name__ == "__main__":
    zenteru.run_forever(main)
