import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import trace_stats as ts  # noqa: E402

FIXTURE = os.path.join(ROOT, "test", "data", "talker_listener")


@pytest.fixture(scope="session")
def daemon():
    exe = ts.daemon_path()
    if not os.access(exe, os.X_OK):
        if os.environ.get("CI"):
            pytest.fail(f"{exe} not built")
        pytest.skip(f"{exe} not built")
    return exe
