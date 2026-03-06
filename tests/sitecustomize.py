"""Site customization to enable coverage in subprocesses.

This file is automatically imported by Python when tests/sitecustomize.py
is in PYTHONPATH. It starts coverage measurement in subprocesses spawned
during integration tests.
"""

import os

# Only start coverage if COVERAGE_PROCESS_START is set
if "COVERAGE_PROCESS_START" in os.environ:
    try:
        import coverage

        coverage.process_startup()
    except ImportError:
        # Coverage not installed, skip
        pass
