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


@pytest.fixture(autouse=True)
def repair_notice_file(tmp_path, monkeypatch):
    """「stateを直した」お知らせの置き場も一時ディレクトリへ逃がす。

    ★ autouse にしてある。main() は必ずこのファイルを読むので、逃がし忘れると
      テストがリポジトリ直下の本物を読み書きしてしまう（CIで書かれると
      未追跡ファイルとして残り、次の実行が誤ってお知らせを言い直す）。"""
    path = tmp_path / "pending_repair_notice.json"
    monkeypatch.setattr(monitor, "REPAIR_NOTICE_FILE", str(path))
    return path


class SlackWasCalled(BaseException):
    """テストが本物のSlackへ飛ばそうとしたときに出す。

    ★ Exception ではなく BaseException から派生させている。safe_slack_post は
      `except Exception` で投稿失敗を握りつぶす作りなので、普通の例外にすると
      「差し替え忘れ」が「投稿に失敗しただけ」に化けて素通りしてしまう。"""


@pytest.fixture(autouse=True)
def no_real_slack(monkeypatch):
    """テストからSlackへ実際に飛ばさない。

    ★ 差し替え忘れを事故ではなく失敗にする。ensure_last_ts が投稿するようになり、
      stateの修復を扱うテストは黙って本物のAPIを叩きうる（ダミートークンなので
      失敗はするが、ネットワークに依存したテストになる）。"""
    def 呼ばれてはいけない(method, **body):
        raise SlackWasCalled(f"テストからSlackを呼ぼうとしました: {method} {body}")

    monkeypatch.setattr(monitor, "slack_post", 呼ばれてはいけない)


@pytest.fixture(autouse=True)
def fixed_now(monkeypatch):
    """現在時刻を固定する。

    ★ テストが実時計に依存しないようにするためと、テスト中の `100.0` のような
      短いtsを「古すぎる」と弾かれないようにするため（MAX_CATCHUP_SEC）。
      ここを固定しておくと、現在時刻からの相対で判定する検査（未来・古すぎ）を
      テスト側から自由に組める。個別に別の時刻が要るテストは、自分で上書きすればよい
      （あとから setattr した方が勝つ）。"""
    monkeypatch.setattr(monitor, "now_ts", lambda: 5000.0)


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    """stateファイルを一時ディレクトリに逃がす。本物の last_processed.txt を壊さない。"""
    path = tmp_path / "last_processed.txt"
    monkeypatch.setattr(monitor, "STATE_FILE", str(path))
    return path
