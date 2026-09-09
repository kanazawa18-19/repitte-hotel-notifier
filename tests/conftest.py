"""monitor.py は読み込んだ時点で環境変数を要求するので、import より先に埋めておく。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test-dummy")
os.environ.setdefault("JOB_SALES_CHANNEL_ID", "CTEST_JOBSALES")
os.environ.setdefault("REPITTE_HOTEL_CHANNEL_ID", "CTEST_REPITTE")
os.environ.setdefault("HIRAMOTO_USER_ID", "UTEST_HIRAMOTO")
os.environ.setdefault("TAKESUE_USER_ID", "UTEST_TAKESUE")
os.environ.setdefault("REPITTE_TEAM_GROUP_ID", "STEST_TEAM")

import pytest  # noqa: E402
import monitor  # noqa: E402


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    """stateファイルを一時ディレクトリに逃がす。本物の last_processed.txt を壊さない。"""
    path = tmp_path / "last_processed.txt"
    monkeypatch.setattr(monitor, "STATE_FILE", str(path))
    return path
