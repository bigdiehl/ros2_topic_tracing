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


def _gh_escape(s):
    return s.replace("%", "%25").replace("\r", "").replace("\n", "%0A")


def pytest_runtest_logreport(report):
    """In GitHub Actions, failures become annotations (readable without log access)."""
    if not os.environ.get("GITHUB_ACTIONS") or not report.failed:
        return
    msg = str(report.longrepr)[-2500:]
    log = ts.log_path()
    if os.path.isfile(log):
        with open(log, errors="replace") as f:
            msg += "\n--- trace_daemon log ---\n" + f.read()[-1500:]
    print(f"\n::error title={report.nodeid} ({report.when})::{_gh_escape(msg)}", flush=True)
