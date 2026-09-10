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


def test_壊れたstateはSlackへ渡す前に直る(state_dir, monkeypatch):
    """数値として読めない値をそのまま oldest に渡すと invalid_ts_oldest で毎回失敗し続ける。"""
    state_dir.write_text("こわれています")
    知らせた = []
    monkeypatch.setattr(monitor, "slack_post",
                        lambda method, **body: (知らせた.append(body), {"ts": "1.0"})[1])

    fixed = monitor.ensure_last_ts()

    float(fixed)  # 数値として読めること
    assert state_dir.read_text().strip() == fixed
    assert 知らせた, "直したことを誰にも知らせていない"


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
    # ★ 桁が変わる境目の値を使うテストなので、現在時刻もその近くに置く
    #   （そうしないと「未来」「古すぎる」の検査に引っかかって別の理由で落ちる）。
    monkeypatch.setattr(monitor, "now_ts", lambda: 1_000_000_100.0)
    state_dir.write_text("999999990.0")
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


# ─────────────── 未来を指すstateの検知（2026-09-10 に移植） ───────────────
#
# [[cnctor-onboarding]] の `7a5d737` で先に塞いだ穴を、こちらへ持ってきたもの。
# stateに未来の時刻が入ると conversations.history の oldest に渡り、Slackは毎回
# 0件を返す。例外も出ず、Actionsは緑のまま、ログには [EMPTY] が並ぶだけで、
# #job_sales が静かなだけの回と見分けがつかない。しかも advance_last_ts が
# 巻き戻しを禁じているので、放っておいても自力では戻らない。


def test_未来を指すstateは読めない値と同じ扱い(state_dir, monkeypatch):
    """★ 数値としては読めるので黙って通ってしまう。通すと転記が全部止まる。"""
    monkeypatch.setattr(monitor, "now_ts", lambda: 5000.0)
    # ミリ秒のepochを書いてしまったときの形。float としては読めてしまう
    state_dir.write_text("5000000.0")

    assert monitor.read_last_ts() == str(5000.0 - monitor.INITIAL_LOOKBACK_SEC)


def test_わずかな時計のズレは通す(state_dir, monkeypatch):
    """許容幅の中の先行は正常として通す。

    stateに入るのはSlackが発行したtsで、読むのはGitHubのランナー。時計は完全には
    一致しないので、ここを狭くしすぎると正常なstateを毎回「壊れている」と誤判定して
    読み直しを繰り返す（誤報を鳴らし続けると、本物の通知まで無視されるようになる）。"""
    monkeypatch.setattr(monitor, "now_ts", lambda: 5000.0)
    ぎりぎり = str(5000.0 + monitor.FUTURE_TS_TOLERANCE_SEC - 1)
    state_dir.write_text(ぎりぎり)

    assert monitor.read_last_ts() == ぎりぎり


def test_許容幅を1秒でも超えたら引き直す(state_dir, monkeypatch):
    """境界の反対側。ここを固定しないと、幅を広げたときに誰も気づけない。"""
    monkeypatch.setattr(monitor, "now_ts", lambda: 5000.0)
    state_dir.write_text(str(5000.0 + monitor.FUTURE_TS_TOLERANCE_SEC + 1))

    assert monitor.read_last_ts() == str(5000.0 - monitor.INITIAL_LOOKBACK_SEC)


def test_時刻として有り得ない値は通さない(state_dir, monkeypatch):
    """floatになるだけの値（負数・無限大・0・nan）を通さない。

    Slackのtsは1970年からの経過秒なので、有限で正の値のはず。
    「floatになる」だけを条件にすると -1 や -inf が正常として通る。"""
    monkeypatch.setattr(monitor, "now_ts", lambda: 5000.0)
    既定 = str(5000.0 - monitor.INITIAL_LOOKBACK_SEC)

    for 壊れた値 in ("-1", "-inf", "0", "nan"):
        state_dir.write_text(壊れた値)
        assert monitor.read_last_ts() == 既定, f"{壊れた値!r} を通してしまった"


def test_未来のstateは直したうえで人に知らせる(state_dir, monkeypatch, repair_notice_file):
    """直せば動き出すが、直した時点で「前回の続き」は失われている。

    壊れていた間に届いた契約報告は転記できていないので、人が現物を確認できないと困る。"""
    monkeypatch.setattr(monitor, "now_ts", lambda: 5000.0)
    state_dir.write_text("5000000.0")
    知らせた = []
    monkeypatch.setattr(monitor, "slack_post",
                        lambda method, **body: (知らせた.append(body), {"ts": "1.0"})[1])

    started = monitor.ensure_last_ts()

    assert started == str(5000.0 - monitor.INITIAL_LOOKBACK_SEC)
    assert state_dir.read_text().strip() == started      # ファイルも直っている
    assert len(知らせた) == 1
    assert 知らせた[0]["channel"] == monitor.REPITTE_HOTEL_CHANNEL_ID
    assert "thread_ts" not in 知らせた[0], "スレッドではなくチャンネル本体へ出すこと"
    assert "読み取り位置を30分前まで戻して" in 知らせた[0]["text"]
    assert not repair_notice_file.exists(), "伝わったのに未送として残っている"


def test_正常なstateでは何も知らせない(state_dir, monkeypatch):
    """誤報を鳴らすと本物の通知が無視される。

    ★ Slackを差し替えていない。conftest の no_real_slack が入っているので、
      ここで投稿しようとすればテストは落ちる。"""
    monkeypatch.setattr(monitor, "now_ts", lambda: 5000.0)
    state_dir.write_text("4000.0")

    assert monitor.ensure_last_ts() == "4000.0"


def test_知らせが届かなくてもすぐ直す(state_dir, monkeypatch, repair_notice_file):
    """★ 直すのは待たない。引き直す先は「そのとき」から30分前なので、
    直すのを遅らせた分だけ欠落がまるごと広がる。

      10:00 破損を検知。Slack投稿だけが失敗している
      10:10 / 10:40 / 11:20 に契約報告が届く
      12:00 Slackが復旧 → 11:30 から読み直す
            → 10:10・10:40・11:20 は永久に転記対象外

    すぐ直せば09:30から読み直すので、10:10以降は全部拾える。"""
    monkeypatch.setattr(monitor, "now_ts", lambda: 5000.0)
    state_dir.write_text("5000000.0")

    def 落ちる(method, **body):
        raise Exception("slack down")

    monkeypatch.setattr(monitor, "slack_post", 落ちる)

    started = monitor.ensure_last_ts()

    # すぐ直っている（止めない）
    assert started == str(5000.0 - monitor.INITIAL_LOOKBACK_SEC)
    assert state_dir.read_text().strip() == started
    # 伝えられなかったことは残っている＝次の実行で言い直す
    assert repair_notice_file.exists()
    assert monitor.read_repair_notice()["broken"] == "5000000.0"


def test_伝わるまで毎回言い直す(monkeypatch, repair_notice_file):
    """★ 言い直せる形で残しておかないと、直した次の回からは「正常なstate」に見えるので、
    壊れていた間の取りこぼしを人が知る機会が永久に失われる。"""
    monitor.write_repair_notice({"broken": "5000000.0", "repaired_to": "3200.0",
                                 "detected_at": "2026-09-10T01:00:00+00:00"})

    monkeypatch.setattr(monitor, "slack_post",
                        lambda method, **body: (_ for _ in ()).throw(Exception("slack down")))
    assert monitor.report_pending_repair_notice() is True
    assert repair_notice_file.exists()                      # まだ残る

    言い直した = []
    monkeypatch.setattr(monitor, "slack_post",
                        lambda method, **body: (言い直した.append(body), {"ts": "1.0"})[1])
    assert monitor.report_pending_repair_notice() is False
    assert not repair_notice_file.exists()                  # 伝わったので消える
    # 遅れて言い直すので、いつ気づいたかを必ず添える
    assert "09/10 10:00 に検知" in 言い直した[0]["text"]


def test_未来のstateから自力で立ち直って転記まで進む(state_dir, monkeypatch, repair_notice_file):
    """★ この移植の存在意義そのもの。

    以前は未来の値が入った時点で [EMPTY] が並ぶだけになり、契約報告が届いても
    永久に転記されなかった。直したうえで、同じ実行の中で転記まで到達すること。"""
    monkeypatch.setattr(monitor, "now_ts", lambda: 5000.0)
    state_dir.write_text("5000000.0")
    monkeypatch.setattr(monitor, "fetch_all_messages",
                        lambda channel, oldest: [{"ts": "4000.0", "text": CONTRACT}])

    投稿した = []
    monkeypatch.setattr(monitor, "slack_post",
                        lambda method, **body: (投稿した.append(body), {"ts": "999.0"})[1])

    monitor.main()   # 赤くならない（お知らせは伝わっている）

    assert any("読み取り位置を30分前まで戻して" in b["text"] for b in 投稿した)   # 直したことを知らせた
    assert any("【契約獲得】" in b["text"] for b in 投稿した)        # そのうえで転記した
    assert state_dir.read_text().strip() == "4000.0"
    assert not repair_notice_file.exists()


def test_伝えられていないお知らせが残ったら実行を赤くする(state_dir, monkeypatch, repair_notice_file):
    """★ 黙って抱えたままにすると、結局誰も知らないまま終わる。

    ただし赤くするのは**全部やり切ったあと**。お知らせが出せないことを理由に
    監視まで止めると、伝えたかった取りこぼしがその間さらに増える。"""
    monkeypatch.setattr(monitor, "now_ts", lambda: 5000.0)
    state_dir.write_text("4000.0")
    monitor.write_repair_notice({"broken": "5000000.0", "repaired_to": "3200.0",
                                 "detected_at": "2026-09-10T01:00:00+00:00"})
    監視した = []
    monkeypatch.setattr(monitor, "fetch_all_messages",
                        lambda channel, oldest: (監視した.append(oldest), [])[1])
    monkeypatch.setattr(monitor, "slack_post",
                        lambda method, **body: (_ for _ in ()).throw(Exception("slack down")))

    with pytest.raises(RuntimeError, match="知らせられていません"):
        monitor.main()

    assert 監視した == ["4000.0"], "お知らせが出せないことを理由に監視を止めている"
    assert repair_notice_file.exists()   # 次回また言い直せるように残っている


def test_お知らせが壊れていても捨てない(monkeypatch, repair_notice_file):
    """★ 読めないJSONを黙って捨てると、「伝えるべきことがあった」ことすら消える。

    中身不明のお知らせとして言い直す（人が確認範囲を判断できないことは文面に出る）。"""
    repair_notice_file.write_text("{壊れている")

    言い直した = []
    monkeypatch.setattr(monitor, "slack_post",
                        lambda method, **body: (言い直した.append(body), {"ts": "1.0"})[1])

    assert monitor.report_pending_repair_notice() is False
    assert 言い直した, "読めないお知らせを黙って捨てている"
    assert "に検知" not in 言い直した[0]["text"]   # いつ気づいたかは分からないので書かない


# ──────── レビュー（2026-09-10・動物チーム3体）で足したもの ────────


def test_許容幅は60秒から広げない():
    """★ 定数の値そのものを固定する（kuma-qa の mutation testing で見つかった穴）。

    境界のテストは `monitor.FUTURE_TS_TOLERANCE_SEC` を**その場で参照**しているので、
    定数を 60 → 300 に広げてもテストが一緒に動いてしまい、落ちない。
    この幅はそのまま「見逃す時間」になる（許容した未来の値は引き直されないため、
    現在時刻が追いつくまでSlackは0件を返し、その間の投稿は二度と取れない）。
    広げるときは、なぜ広げてよいのかを AGENTS.md に書いてからここを直すこと。"""
    assert monitor.FUTURE_TS_TOLERANCE_SEC == 60


def test_許容幅ちょうどは通す(state_dir, monkeypatch):
    """境界そのもの。判定が `>` か `>=` かをここで固定する。"""
    monkeypatch.setattr(monitor, "now_ts", lambda: 5000.0)
    ちょうど = str(5000.0 + monitor.FUTURE_TS_TOLERANCE_SEC)
    state_dir.write_text(ちょうど)

    assert monitor.read_last_ts() == ちょうど


def test_書き込み側は未来のtsでも記録する(state_dir, monkeypatch):
    """★ 読み取り側と書き込み側の非対称は**意図的**。揃えてはいけない。

    ここへ渡ってくるのは「いま転記し終えたメッセージのts」。未来に見えるからと
    記録を拒むと、次回その契約報告をもう一度転記する（＝二重転記）。
    記録側は実際に処理したところを正直に残し、直すのは読み取り側の仕事にする。"""
    monkeypatch.setattr(monitor, "now_ts", lambda: 5000.0)
    state_dir.write_text("4000.0")

    monitor.advance_last_ts("5000000.0")     # 遥か未来のts

    assert state_dir.read_text().strip() == "5000000.0", "記録を拒むと次回二重に転記する"
    # そのうえで、読み取り側が引き直して自力で立ち直る
    assert monitor.read_last_ts() == str(5000.0 - monitor.INITIAL_LOOKBACK_SEC)


def test_二重に壊れても先に気づいた時刻を残す(monkeypatch, repair_notice_file):
    """★ 人がこのお知らせで決めるのは「どこまでさかのぼって確認するか」。

    Slackが落ち続けている間に2回壊れたとき、遅い方の時刻で上書きすると、
    1回目から2回目までの分を確認範囲から丸ごと落としてしまう。"""
    monitor.write_repair_notice({"broken": "1回目", "repaired_to": "3200.0",
                                 "detected_at": "2026-09-10T01:00:00+00:00"})
    monitor.write_repair_notice({"broken": "2回目", "repaired_to": "3300.0",
                                 "detected_at": "2026-09-10T05:00:00+00:00"})

    残った = monitor.read_repair_notice()
    assert 残った["detected_at"] == "2026-09-10T01:00:00+00:00", "確認範囲を狭めている"
    assert "1回目" in 残った["also_broken"], "消えた値を追えなくしている"

    言い直した = []
    monkeypatch.setattr(monitor, "slack_post",
                        lambda method, **body: (言い直した.append(body), {"ts": "1.0"})[1])
    monitor.report_pending_repair_notice()
    assert "09/10 10:00 に検知" in 言い直した[0]["text"]   # 早い方の時刻で伝える


def test_今回壊れて今回伝え損ねた回も赤くする(state_dir, monkeypatch, repair_notice_file):
    """★ いちばん漏れやすいのがこの経路（obasan-quality 指摘）。

    `main()` 冒頭の言い直しは「前回まで」の分しか見ない。今回 ensure_last_ts が
    新しく破損を見つけ、そのお知らせも出せなかった場合は、末尾の再確認だけが拾う。
    片方の判定を消すと、この回だけ緑のまま終わる。"""
    monkeypatch.setattr(monitor, "now_ts", lambda: 5000.0)
    state_dir.write_text("5000000.0")        # 今回はじめて見つかる破損
    assert not repair_notice_file.exists()   # 前回までの未送は無い

    転記した = []
    監視した = []
    monkeypatch.setattr(monitor, "fetch_all_messages",
                        lambda channel, oldest: (監視した.append(oldest),
                                                 [{"ts": "4000.0", "text": CONTRACT}])[1])

    def slackは落ちている(method, **body):
        転記した.append(body)
        raise Exception("slack down")

    monkeypatch.setattr(monitor, "slack_post", slackは落ちている)

    with pytest.raises(Exception):
        monitor.main()

    assert 監視した, "お知らせが出せないことを理由に監視まで止めている"
    assert repair_notice_file.exists(), "次回言い直せる形で残っていない"


def test_stateの書き込みが途中で落ちても前の値が残る(state_dir, monkeypatch):
    """★ 0バイトのstateを作らない（shirokuma-sec 指摘）。

    `open(..., "w")` は先にファイルを空にしてから書くので、書き込み中に
    ジョブが落ちると0バイトのstateが残る。今回の変更で「壊れたstate」は
    リピッテチームへの確認依頼＋実行を赤くする扱いになったため、
    突然死しただけで重い通知が飛ぶことになる。"""
    state_dir.write_text("4000.0")

    def 置き換えの直前で落ちる(src, dst):
        raise OSError("ランナーが落ちた")

    monkeypatch.setattr(monitor.os, "replace", 置き換えの直前で落ちる)

    with pytest.raises(OSError):
        monitor.write_last_ts("5000.0")

    assert state_dir.read_text().strip() == "4000.0", "前の値が消えている"


def test_お知らせの文面は転記漏れと二重の両方に触れる():
    """★ 読むのはエンジニアではない。何を確認すればよいかが文面だけで分かること。

    引き直しは30分巻き戻すので、転記漏れ（壊れていた間の分）と
    二重転記（読み直した分）の**どちらにも振れうる**。こちらからは区別できないので、
    両方を正直に書く。片方しか書かないと、もう片方を誰も見に行かない。"""
    text = monitor._repair_notice_text("2026-09-10T01:00:00+00:00")

    assert "転記されていないおそれ" in text
    assert "二重に並んでいる可能性" in text
    assert "09/10 10:00 に検知" in text
    assert "読み取り位置を30分前まで戻して" in text        # 検知時点でもう直っている（現在進行形にしない）
    # ★ 「何をすればよいか」まで書く（2026-09-10 の Gemini Pro 指摘）。
    #   「心当たりのある契約報告をご確認ください」だけでは、読んだ人は自分の分しか見ないか、
    #   分からず放置する。見比べる対象を名指しする。
    assert "【お願い】" in text
    assert "#job_sales" in text


# ──────── 他モデルレビュー（2026-09-10・Gemini Pro / GPT-5.6 Sol）で足したもの ────────


def test_古すぎるstateも引き直す(state_dir, monkeypatch):
    """★ 未来だけ見ても足りない（GPT-5.6 Sol 指摘）。

    `1` や `123456` は有限で正なので、未来の検査を全部すり抜ける。これが oldest に
    渡ると #job_sales の一番古い履歴からページングが始まり、MAX_PAGES で例外になる。
    stateは進まないので、次回も同じところで落ち続ける。未来のときと違って赤くはなるが、
    **止まっていることに変わりはない**。"""
    monkeypatch.setattr(monitor, "now_ts", lambda: 1_800_000_000.0)
    既定 = str(1_800_000_000.0 - monitor.INITIAL_LOOKBACK_SEC)

    for 古すぎる値 in ("1", "100", "123456"):
        state_dir.write_text(古すぎる値)
        assert monitor.read_last_ts() == 既定, f"{古すぎる値!r} を通してしまった"


def test_許される古さの境目(state_dir, monkeypatch):
    """境界を固定する。ここを狭めると、長く止まったあとの復帰で毎回引き直しになる。"""
    now = 1_800_000_000.0
    monkeypatch.setattr(monitor, "now_ts", lambda: now)

    ぎりぎり通る = str(now - monitor.MAX_CATCHUP_SEC)
    state_dir.write_text(ぎりぎり通る)
    assert monitor.read_last_ts() == ぎりぎり通る

    state_dir.write_text(str(now - monitor.MAX_CATCHUP_SEC - 1))
    assert monitor.read_last_ts() == str(now - monitor.INITIAL_LOOKBACK_SEC)


def test_未来のtsを記録したらその回のうちに赤くする(state_dir, monkeypatch, repair_notice_file):
    """★ 「保存はする。ただし保存した事実は即座に知らせる」（GPT-5.6 Sol 指摘）。

    未来のtsでも記録は拒まない（拒むと次回その契約報告をもう一度転記する）。
    だが黙って緑で終わると、次の実行（30分後）まで誰も気づけない。
    転記そのものは最後まで済ませたうえで、実行だけ赤くする。"""
    monkeypatch.setattr(monitor, "now_ts", lambda: 5000.0)
    state_dir.write_text("4000.0")
    未来のts = str(5000.0 + monitor.FUTURE_TS_TOLERANCE_SEC + 1)
    monkeypatch.setattr(monitor, "fetch_all_messages",
                        lambda channel, oldest: [{"ts": 未来のts, "text": CONTRACT}])

    投稿した = []
    monkeypatch.setattr(monitor, "slack_post",
                        lambda method, **body: (投稿した.append(body), {"ts": "999.0"})[1])

    with pytest.raises(RuntimeError, match="未来を指すts"):
        monitor.main()

    assert 投稿した, "転記まで済ませずに打ち切っている"
    assert state_dir.read_text().strip() == 未来のts, "記録を拒むと次回二重に転記する"


def test_正常なtsでは赤くしない(state_dir, monkeypatch, repair_notice_file):
    """誤報を鳴らさない。ここが緩いと、本物の通知まで無視されるようになる。"""
    monkeypatch.setattr(monitor, "now_ts", lambda: 5000.0)
    state_dir.write_text("4000.0")
    monkeypatch.setattr(monitor, "fetch_all_messages",
                        lambda channel, oldest: [{"ts": "4500.0", "text": CONTRACT}])
    monkeypatch.setattr(monitor, "slack_post", lambda method, **body: {"ts": "999.0"})

    monitor.main()   # 例外にならないこと

    assert state_dir.read_text().strip() == "4500.0"


def test_お知らせは何と何を照合するか名指しする():
    """★ 「心当たりのある契約報告をご確認ください」では動けない。

    Gemini Pro と GPT-5.6 Sol が独立に指摘した。読んだ人は自分の分しか見ないか、
    分からず放置する。転記漏れを探すのは本来チーム全体の仕事なので、
    どこを見ればよいかまで書く。"""
    text = monitor._repair_notice_text("2026-09-10T01:00:00+00:00")

    assert "【契約獲得】" in text      # 照合する投稿の目印
    assert "リピッテホテル" in text    # 探す対象
    assert "照合してください" in text
    # ★ 通知の時点で終わっているのは「位置を戻した」ところまで。読み直しはこの後。
    assert "読み直しました" not in text


# ─── mutation testing で「守れていなかった」と分かった分（2026-09-10 kuma-qa 3周目）───
#
# どれも「テストは通っているが、守りたい対象そのものには触れていない」形だった。
# 定数やフォールバック値は、その場で参照すると変更に追従してしまうので、値を直接書く。


def test_受け入れる古さは90日から変えない():
    """★ 定数の値そのものを固定する。

    境界テスト（test_許される古さの境目）は `monitor.MAX_CATCHUP_SEC` をその場で
    参照して期待値を作るので、90日を1日にも10年にも変えられて落ちない。
    - 短くすると、少し止まっただけで毎回引き直し＋通知になる（誤報で本物が埋もれる）
    - 長くすると、ページングが追いつかず MAX_PAGES で落ち続ける（別の永久停止）
    根拠は AGENTS.md にある。変えるならそちらを直してからここを直すこと。"""
    assert monitor.MAX_CATCHUP_SEC == 90 * 24 * 60 * 60


def test_brokenが無い古い形式でもNoneを積まない(monkeypatch, repair_notice_file):
    """★ 積まれた `None` は人が読む記録に出てくる。

    `also_broken` は「消えた値をあとから追うため」の欄なので、欠けているなら
    そう書いてある方がよい。既存テストは `broken` が必ず入っている形しか通しておらず、
    このフォールバックを消しても誰も気づかなかった。"""
    monitor.write_repair_notice({"repaired_to": "3200.0",
                                 "detected_at": "2026-09-10T01:00:00+00:00"})  # broken 無し
    monitor.write_repair_notice({"broken": "2回目", "repaired_to": "3300.0",
                                 "detected_at": "2026-09-10T05:00:00+00:00"})

    assert monitor.read_repair_notice()["also_broken"] == ["(不明)"]


def test_読めないお知らせの中身は不明と書く(repair_notice_file):
    """★ JSONが壊れていたときの中身。

    既存テストは「言い直すこと」しか見ておらず、返す中身が None でも通っていた。
    None が記録に残ると、あとから読んだ人が「値が無かった」のか
    「読めなかった」のか区別できない。"""
    repair_notice_file.write_text("{壊れている")

    notice = monitor.read_repair_notice()

    assert notice["broken"] == "(不明)"
    assert notice["repaired_to"] == "(不明)"
    assert notice["detected_at"] == ""     # いつ気づいたかは分からない


def test_先に気づいた時刻は向きに関わらず残る(repair_notice_file):
    """★ 「既存の方が新しければ素直に上書き」にすると、早い方を無条件に捨てる。

    時計が巻き戻ったときや、将来ここを触る人が順序を誤ったときに効く歯止め。
    残したいのは常に**いちばん早く気づいた時刻**（確認範囲の起点になるため）。"""
    monitor.write_repair_notice({"broken": "あとから気づいた分", "repaired_to": "3300.0",
                                 "detected_at": "2026-09-10T05:00:00+00:00"})
    # 既存より**古い** detected_at を書きに行く（時計が巻き戻った形）
    monitor.write_repair_notice({"broken": "先に気づいた分", "repaired_to": "3200.0",
                                 "detected_at": "2026-09-10T01:00:00+00:00"})

    残った = monitor.read_repair_notice()
    assert 残った["detected_at"] == "2026-09-10T01:00:00+00:00"
    assert "あとから気づいた分" in 残った["also_broken"], "早い方を捨てている"


def test_異常が2つ重なってもエラー文に両方出る(state_dir, monkeypatch, repair_notice_file):
    """★ raise を2本並べると、両方成立したときに2つ目がエラー文に出ない。

    ログには残るし次回自己検知もするが、Actionsのエラー文だけを見た人は
    片方（時計のずれ）に気づけず、対処が1回ぶん遅れる。"""
    monkeypatch.setattr(monitor, "now_ts", lambda: 5000.0)
    state_dir.write_text("4000.0")
    monitor.write_repair_notice({"broken": "5000000.0", "repaired_to": "3200.0",
                                 "detected_at": "2026-09-10T01:00:00+00:00"})
    未来のts = str(5000.0 + monitor.FUTURE_TS_TOLERANCE_SEC + 1)
    monkeypatch.setattr(monitor, "fetch_all_messages",
                        lambda channel, oldest: [{"ts": 未来のts, "text": CONTRACT}])

    投稿できたか = {"本体": True}

    def slack_post(method, **body):
        if "記録が壊れていました" in body.get("text", "") or "止まっていました" in body.get("text", ""):
            raise Exception("slack down")   # お知らせだけ届かない
        return {"ts": "999.0"}

    monkeypatch.setattr(monitor, "slack_post", slack_post)

    with pytest.raises(RuntimeError) as e:
        monitor.main()

    assert "知らせられていません" in str(e.value)   # ①お知らせが伝わっていない
    assert "未来を指すts" in str(e.value)           # ②時計がずれている


def test_3回以上壊れても履歴が全部残る(repair_notice_file):
    """★ `also_broken` は「消えた値をあとから追うため」の欄。

    2回目までしか確かめていないと、蓄積（既存の also_broken を引き継ぐ側）を
    消しても誰も気づかない。実際 kuma-qa の mutation で素通りした。
    Slackが落ち続けている間に3回壊れる形を通す。"""
    for n, t in [("1回目", "01"), ("2回目", "05"), ("3回目", "09")]:
        monitor.write_repair_notice({"broken": n, "repaired_to": "3200.0",
                                     "detected_at": f"2026-09-10T{t}:00:00+00:00"})

    残った = monitor.read_repair_notice()
    assert 残った["detected_at"] == "2026-09-10T01:00:00+00:00"   # 最初に気づいた時刻
    assert 残った["also_broken"] == ["1回目", "2回目"], "途中の履歴が消えている"
    assert 残った["broken"] == "3回目"                            # 最新は本体に残る


# ──────── 転記の対象を絞る条件（もともとテストが無かった） ────────
#
# ★ 正常系（CONTRACT）しか通していなかったので、条件を `and` から片方だけに
#   変えても全テストが通ってしまう状態だった（2026-09-10 kuma-qa 4周目）。
#   #job_sales には他商品の契約報告も雑談も流れるので、**弾く側**を固定する。


def _転記された本文(monkeypatch, state_dir, text):
    """1件だけ流して、#repitte-hotel へ本体が転記されたかを返す。"""
    monkeypatch.setattr(monitor, "now_ts", lambda: 5000.0)
    state_dir.write_text("4000.0")
    monkeypatch.setattr(monitor, "fetch_all_messages",
                        lambda channel, oldest: [{"ts": "4500.0", "text": text}])
    投稿した = []
    monkeypatch.setattr(monitor, "slack_post",
                        lambda method, **body: (投稿した.append(body), {"ts": "999.0"})[1])
    monitor.main()
    return 投稿した


def test_契約獲得の見出しが無ければ転記しない(state_dir, monkeypatch, repair_notice_file):
    """「リピッテホテル」という語を含むだけの投稿を転記しない。

    ★ 弾いたうえで state は進める。読み終えているので、進めないと次回もう一度読む。"""
    投稿した = _転記された本文(monkeypatch, state_dir, "リピッテホテルについて雑談しています")

    assert 投稿した == [], "契約報告でない投稿を転記している"
    assert state_dir.read_text().strip() == "4500.0", "読み終えたのにstateを進めていない"


def test_リピッテホテル以外の契約報告は転記しない(state_dir, monkeypatch, repair_notice_file):
    """他商品の契約報告は #repitte-hotel の対象外。

    ★ ここが緩むと、リピッテチームに無関係な契約報告が流れ込む。
      さらに下流の [[cnctor-onboarding]] が転記bot経由の投稿として拾いうる。"""
    投稿した = _転記された本文(
        monkeypatch, state_dir,
        "【契約獲得】ホテルラボ\n契約サービス：ホテルラボ\n課金開始月：10月\n")

    assert 投稿した == [], "他商品の契約報告を転記している"
    assert state_dir.read_text().strip() == "4500.0"


def test_両方そろっていれば転記する(state_dir, monkeypatch, repair_notice_file):
    """否定テストの対になる肯定側。条件を「常に弾く」に変えたら落ちること。"""
    投稿した = _転記された本文(monkeypatch, state_dir, CONTRACT)

    assert any("【契約獲得】" in b.get("text", "") for b in 投稿した)
