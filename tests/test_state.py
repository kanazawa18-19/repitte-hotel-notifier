"""読み始める起点の扱い。ここが 2026-09-10 に直した不具合の本体。

旧実装は「新着が1件も無かった回はstateを書かずに戻る」作りだった。
stateファイルを失うと二度と作られず、実行のたびに「今から30分前」が
計算し直されて、実行間隔が空いたぶんの投稿が二度と読まれない。
このリポジトリの実行間隔は中央値433分・最大2925分（実測）で、30分では全く足りない。
"""
import monitor
import pytest


def test_起点が無ければ保存される(state_dir):
    assert not state_dir.exists()
    started = monitor.ensure_last_ts()
    assert state_dir.exists()
    assert state_dir.read_text().strip() == started


def test_起点は2回目以降引き直されない(state_dir, monkeypatch):
    """★ 旧挙動を再現すると落ちるテスト。

    1回目と2回目の間に45分空ける。起点が保存されていなければ、2回目のoldestは
    「今から30分前」に引き直され、その間の15分が**どの取得範囲にも入らない**。"""
    now = [1_700_000_000.0]
    monkeypatch.setattr(monitor, "now_ts", lambda: now[0])

    first = monitor.ensure_last_ts()
    now[0] += 45 * 60
    second = monitor.ensure_last_ts()

    assert first == second


def test_空振りでもstateは据え置かれる(state_dir, monkeypatch):
    """新着0件の回に、起点が書き換わったり消えたりしない。"""
    monkeypatch.setattr(monitor, "fetch_all_messages", lambda channel, oldest: [])
    started = monitor.ensure_last_ts()

    monitor.main()

    assert state_dir.read_text().strip() == started


def test_壊れたstateはSlackへ渡す前に直る(state_dir):
    """数値として読めない値をそのまま oldest に渡すと invalid_ts_oldest で毎回失敗し続ける。"""
    state_dir.write_text("こわれています")
    fixed = monitor.ensure_last_ts()
    float(fixed)  # 数値として読めること
    assert state_dir.read_text().strip() == fixed


def test_処理済みの印は時刻として最大のts(state_dir, monkeypatch):
    """文字列の辞書順ではなく、時刻として最大のtsを記録する。

    ★ 正直に書いておく: これは**実バグの修正ではなく保険**。
       Slackのtsは当面10桁で、桁数が同じなら辞書順と時刻順の答えは一致する。
       桁が変わる場合（下の値）だけ答えが割れるので、そこを固定しておく。
       "999999999" > "1000000000" は文字列としては真だが、時刻としては偽。"""
    messages = [{"ts": "999999999.000000", "text": "古い"},
                {"ts": "1000000000.000000", "text": "新しい"}]
    monkeypatch.setattr(monitor, "fetch_all_messages", lambda channel, oldest: messages)
    monkeypatch.setattr(monitor, "slack_post", lambda *a, **k: {"ts": "1.0"})

    monitor.main()

    assert state_dir.read_text().strip() == "1000000000.000000"


def test_ページングして全件取る(monkeypatch):
    """1ページ目しか読まないと、取得窓に51件以上あるとき古い方が二度と読まれない。"""
    pages = [
        {"messages": [{"ts": "3.0"}], "has_more": True,
         "response_metadata": {"next_cursor": "c1"}},
        {"messages": [{"ts": "2.0"}], "has_more": True,
         "response_metadata": {"next_cursor": "c2"}},
        {"messages": [{"ts": "1.0"}], "has_more": False},
    ]
    seen = []

    def fake_get(method, **params):
        seen.append(params.get("cursor"))
        return pages[len(seen) - 1]

    monkeypatch.setattr(monitor, "slack_get", fake_get)
    got = monitor.fetch_all_messages("C1", "0")

    assert [m["ts"] for m in got] == ["3.0", "2.0", "1.0"]
    assert seen == [None, "c1", "c2"]


def test_next_cursorが空なら止まる(monkeypatch):
    """has_more は真なのに cursor が返ってこない異常応答で、無限ループにしない。"""
    monkeypatch.setattr(monitor, "slack_get",
                        lambda method, **p: {"messages": [{"ts": "1.0"}], "has_more": True})
    assert monitor.fetch_all_messages("C1", "0") == [{"ts": "1.0"}]
