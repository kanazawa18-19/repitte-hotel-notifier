"""読み始める起点の扱いと、途中で失敗したときの後始末。

ここが 2026-09-10 に直した不具合の本体。
旧実装は「新着が1件も無かった回はstateを書かずに戻る」作りだった。
stateファイルを失うと二度と作られず、実行のたびに「今から30分前」が
計算し直されて、実行間隔が空いたぶんの投稿が二度と読まれない。
実行間隔が実際どれだけ広いか（cronの見た目と全く違う）は AGENTS.md にある。
"""
import monitor
import pytest


# 必須項目が全部そろった契約報告。これを渡すとリマインドのスレッドは出ない。
CONTRACT = ("【契約獲得】リピッテホテル\n"
            "契約サービス：リピッテホテル\n"
            "課金開始月：10月\n請求方法：口座振替\n予約番契約有無：無\n")


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


def test_取得が失敗しても起点は残る(state_dir, monkeypatch):
    """★ この修正の存在意義そのもの。

    起点の保存は Slack の取得より**前**に行う。取得が落ちた回でも起点が残るので、
    次回は同じ場所から読み直せる。順序を入れ替えるとここが落ちる。"""
    def 落ちる(channel, oldest):
        raise Exception("slack down")

    monkeypatch.setattr(monitor, "fetch_all_messages", 落ちる)

    with pytest.raises(Exception):
        monitor.main()

    assert state_dir.exists()
    float(state_dir.read_text().strip())


def test_転記に失敗したら打ち切って成功分までstateを進める(state_dir, monkeypatch):
    """★ 二重転記を防ぐ本体。

    2件目の転記が落ちたとき、stateを一切進めないと1件目が次回また投稿される
    （#repitte-hotel に同じ契約報告が二重に並ぶ）。逆に窓の端まで進めると
    2件目が誰にも転記されないまま埋もれる。1件目の分だけ進めるのが正解。"""
    state_dir.write_text("50.0")
    messages = [{"ts": "100.0", "text": CONTRACT + "1件目"},
                {"ts": "200.0", "text": CONTRACT + "2件目"}]
    monkeypatch.setattr(monitor, "fetch_all_messages", lambda channel, oldest: messages)

    投稿した = []

    def fake_post(method, **body):
        if "2件目" in body.get("text", ""):
            raise Exception("チャンネルが見つかりません")
        投稿した.append(body["text"])
        return {"ts": "999.0"}

    monkeypatch.setattr(monitor, "slack_post", fake_post)
    monitor.main()

    assert len(投稿した) == 1                          # 1件目だけ転記された
    assert state_dir.read_text().strip() == "100.0"    # 1件目の分まで進んだ


def test_添付に失敗しても本体は再投稿されない(state_dir, monkeypatch):
    """本体の転記が済んだあとに添付で落ちても、stateは進める（重複より欠落を選ぶ）。

    進めないと、次回この契約報告を本体から転記し直して二重に並ぶ。
    黙って進めると誰も気づかないので、転記したスレッドに警告を残すところまで確かめる。"""
    state_dir.write_text("50.0")
    messages = [{"ts": "100.0", "text": CONTRACT, "files": [{"name": "契約書.pdf"}]}]
    monkeypatch.setattr(monitor, "fetch_all_messages", lambda channel, oldest: messages)

    def 落ちる(f, channel):
        raise Exception("ダウンロードに失敗")

    monkeypatch.setattr(monitor, "upload_file", 落ちる)

    投稿した = []
    monkeypatch.setattr(monitor, "slack_post",
                        lambda method, **body: (投稿した.append(body), {"ts": "999.0"})[1])
    monitor.main()

    assert state_dir.read_text().strip() == "100.0"
    assert any("⚠️" in b.get("text", "") for b in 投稿した)   # 人が拾えるよう知らせている
    assert any(b.get("thread_ts") == "999.0" for b in 投稿した)  # 転記したスレッドの中に


def test_stateは巻き戻らない(state_dir, monkeypatch):
    """★ 巻き戻ると、そこから全部読み直して転記済みの報告を丸ごと再投稿する。

    Slackが想定外の応答を返したときや、将来ここを触った人が順序を壊したときの歯止め。"""
    state_dir.write_text("5000.0")
    monkeypatch.setattr(monitor, "fetch_all_messages",
                        lambda channel, oldest: [{"ts": "100.0", "text": "無関係な投稿"}])

    monitor.main()

    assert state_dir.read_text().strip() == "5000.0"


def test_処理済みの印は時刻として最大のts(state_dir, monkeypatch):
    """文字列の辞書順ではなく、時刻として扱う（並べ替えと記録の両方）。

    ★ 正直に書いておく: これは**実バグの修正ではなく保険**。
       Slackのtsは当面10桁で、桁数が同じなら辞書順と時刻順の答えは一致する。
       桁が変わる場合（下の値）だけ答えが割れるので、そこを固定しておく。
       "999999999" > "1000000000" は文字列としては真だが、時刻としては偽。"""
    state_dir.write_text("1.0")
    messages = [{"ts": "1000000000.000000", "text": CONTRACT + "新しい"},
                {"ts": "999999999.000000", "text": CONTRACT + "古い"}]
    monkeypatch.setattr(monitor, "fetch_all_messages", lambda channel, oldest: messages)

    投稿順 = []

    def fake_post(method, **body):
        投稿順.append(body["text"])
        return {"ts": "999.0"}

    monkeypatch.setattr(monitor, "slack_post", fake_post)
    monitor.main()

    assert state_dir.read_text().strip() == "1000000000.000000"
    assert "古い" in 投稿順[0] and "新しい" in 投稿順[1]   # 古い方から転記する


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


def test_next_cursorが空なら例外にする(monkeypatch):
    """★ has_more は真なのに cursor が返ってこない異常応答。

    ここで「取れた分だけ」返すと、呼び出し側はそれを全部だと思って state を進め、
    **取れなかった古い方を永久に落とす**。Slackは新しい順に返すので、
    落ちるのは必ず古い方＝まだ転記していない契約報告になる。
    黙って部分取得を返すくらいなら、その回を丸ごと失敗させる方が安全。"""
    monkeypatch.setattr(monitor, "slack_get",
                        lambda method, **p: {"messages": [{"ts": "1.0"}], "has_more": True})
    with pytest.raises(Exception, match="カーソル"):
        monitor.fetch_all_messages("C1", "0")


def test_ページングが際限なく続いたら例外にする(monkeypatch):
    """has_more と cursor を延々返し続ける異常応答で、6時間走り続けない。

    ★ 黙って抜けてはいけない。「取り切った」と誤解してstateを進めると、残りを永久に落とす。"""
    monkeypatch.setattr(monitor, "slack_get",
                        lambda method, **p: {"messages": [{"ts": "1.0"}], "has_more": True,
                                             "response_metadata": {"next_cursor": "ずっと続く"}})
    with pytest.raises(Exception, match="ページ"):
        monitor.fetch_all_messages("C1", "0")

def test_転記の直後にその場でstateを書く(state_dir, monkeypatch):
    """★ 「1件ずつ進める」は**その場でファイルに書く**という意味。

    ループの外でまとめて書く作りだと、例外ではなく突然死したとき
    （ジョブのタイムアウト・SIGKILL・ランナー障害）に書き込み自体が起きず、
    転記済みの契約報告を次回また投稿する。
    ここでは「本体を投稿した瞬間に、もうファイルへ書けているか」を見る。"""
    state_dir.write_text("50.0")
    messages = [{"ts": "100.0", "text": CONTRACT}, {"ts": "200.0", "text": CONTRACT}]
    monkeypatch.setattr(monitor, "fetch_all_messages", lambda channel, oldest: messages)

    書けていた = []

    def fake_post(method, **body):
        # 投稿した「直後」ではなく、次の投稿の時点でファイルを覗く。
        書けていた.append(state_dir.read_text().strip())
        return {"ts": "999.0"}

    monkeypatch.setattr(monitor, "slack_post", fake_post)
    monitor.main()

    # 2件目を投稿する時点で、1件目のtsが既にファイルに載っていること
    assert 書けていた == ["50.0", "100.0"]
    assert state_dir.read_text().strip() == "200.0"


def test_添付の失敗は黙って握りつぶさない(monkeypatch):
    """★ 以前はログを1行出して戻るだけだった。

    呼び出し側の「失敗したらスレッドに警告を残す」経路をすり抜け、
    **添付が付かないまま誰にも気づかれず終わっていた**。契約書が落ちるのは困る。"""
    class 応答:
        ok = True
        content = b"x"

        @staticmethod
        def json():
            return {"ok": False, "error": "upload_failed"}

    monkeypatch.setattr(monitor.requests, "get", lambda *a, **k: 応答())
    monkeypatch.setattr(monitor.requests, "post", lambda *a, **k: 応答())

    with pytest.raises(Exception, match="getUploadURLExternal"):
        monitor.upload_file({"name": "契約書.pdf", "url_private": "https://example.test/f"}, "C1")
