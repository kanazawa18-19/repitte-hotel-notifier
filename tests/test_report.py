"""完了報告（#job_sales の契約報告の元スレッドへ出す「どこまでやったか」）のテスト。

★ 2026-09-10 本人指示
  「完了してもしなくても、完了したものと残っているタスクをスレッドで報告する」
  「完了報告の場所は常に、job_sales の契約報告の元スレッド」
"""
import pytest

import monitor

CONTRACT = ("【契約獲得】リピッテホテル\n"
            "契約サービス：リピッテホテル\n"
            "課金開始月：10月\n請求方法：口座振替\n予約番契約有無：無\n")

# 必須項目のうち「請求方法」と「予約番契約有無」が抜けている契約報告
CONTRACT_欠けあり = "【契約獲得】リピッテホテル\n契約サービス：リピッテホテル\n課金開始月：10月\n"


@pytest.fixture
def 投稿(monkeypatch):
    """slack_post の呼び出しを全部ためる。"""
    たまり = []

    def fake_post(method, **body):
        たまり.append(body)
        return {"ts": "999.0"}

    monkeypatch.setattr(monitor, "slack_post", fake_post)
    return たまり


def 完了報告(投稿):
    return [b for b in 投稿 if "できたこと" in b.get("text", "")
            or "できなかったこと" in b.get("text", "")]


# ── 場所 ──────────────────────────────────────────────────────────────

def test_報告先は常にjob_salesの元スレッド(state_dir, monkeypatch, 投稿):
    """★ 転記先（#repitte-hotel）のスレッドに出さない。

    出し先を間違えると、報告した本人と営業部には一生見えない。
    「元投稿のts」であって「転記した投稿のts（999.0）」ではない。"""
    state_dir.write_text("50.0")
    monkeypatch.setattr(monitor, "fetch_all_messages",
                        lambda channel, oldest: [{"ts": "100.0", "text": CONTRACT, "user": "U報告者"}])
    monitor.main()

    報告 = 完了報告(投稿)
    assert len(報告) == 1
    assert 報告[0]["channel"] == monitor.JOB_SALES_CHANNEL_ID
    assert 報告[0]["thread_ts"] == "100.0"


def test_転記先のチャンネルには完了報告を出さない(state_dir, monkeypatch, 投稿):
    """弾く側。#repitte-hotel に出ているのは転記本体だけであること。"""
    state_dir.write_text("50.0")
    monkeypatch.setattr(monitor, "fetch_all_messages",
                        lambda channel, oldest: [{"ts": "100.0", "text": CONTRACT, "user": "U報告者"}])
    monitor.main()

    repitte = [b for b in 投稿 if b.get("channel") == monitor.REPITTE_HOTEL_CHANNEL_ID]
    assert len(repitte) == 1
    assert "できたこと" not in repitte[0]["text"]


# ── 完了してもしなくても出す ──────────────────────────────────────────

def test_全部うまくいった回も必ず報告する(state_dir, monkeypatch, 投稿):
    """★ 成功した回に黙ると、「まだ誰も見ていない」のと区別が付かない。"""
    state_dir.write_text("50.0")
    monkeypatch.setattr(monitor, "fetch_all_messages",
                        lambda channel, oldest: [{"ts": "100.0", "text": CONTRACT, "user": "U報告者"}])
    monitor.main()

    報告 = 完了報告(投稿)
    assert len(報告) == 1
    assert "✅" in 報告[0]["text"]
    assert "#repitte-hotel への転記" in 報告[0]["text"]


def test_転記そのものに失敗した回も報告する(state_dir, monkeypatch, 投稿):
    """★ できなかった回こそ黙らない。ログ1行では誰も見ない。"""
    state_dir.write_text("50.0")
    monkeypatch.setattr(monitor, "fetch_all_messages",
                        lambda channel, oldest: [{"ts": "100.0", "text": CONTRACT, "user": "U報告者"}])

    def fake_post(method, **body):
        if body.get("channel") == monitor.REPITTE_HOTEL_CHANNEL_ID:
            raise Exception("チャンネルが見つかりません")
        投稿.append(body)
        return {"ts": "999.0"}

    monkeypatch.setattr(monitor, "slack_post", fake_post)
    monitor.main()

    報告 = 完了報告(投稿)
    assert len(報告) == 1
    assert "⚠️" in 報告[0]["text"]
    assert "できなかったこと" in 報告[0]["text"]
    assert 報告[0]["thread_ts"] == "100.0"


def test_報告が出せなくても監視は止まらない(state_dir, monkeypatch, capsys):
    """★ 報告はあくまで「知らせる」処理。ここで例外を上げ直すと後続まで止まる。"""
    state_dir.write_text("50.0")
    monkeypatch.setattr(monitor, "fetch_all_messages",
                        lambda channel, oldest: [{"ts": "100.0", "text": CONTRACT, "user": "U報告者"}])

    def fake_post(method, **body):
        if body.get("thread_ts"):
            raise Exception("スレッドが見つかりません")
        return {"ts": "999.0"}

    monkeypatch.setattr(monitor, "slack_post", fake_post)
    monitor.main()   # 例外が漏れないこと

    assert state_dir.read_text().strip() == "100.0"   # 転記は済んだので進む
    assert "完了報告そのものに失敗" in capsys.readouterr().out


# ── 文面 ──────────────────────────────────────────────────────────────

def test_残タスクの語尾は必ず感嘆符():
    """★ 語尾は「!」（2026-09-10 本人指示）。「お願いします。」に戻さない。"""
    text = monitor.completion_report_text(
        {"ts": "100.0", "user": "U報告者"}, CONTRACT_欠けあり, done=["#repitte-hotel への転記"])

    assert "・請求方法、予約番契約有無のご共有については、<@U報告者>さんよろしくお願いします！" in text


def test_足りない項目は報告した本人に聞く():
    """不足項目を知っているのは報告した本人だけ。#job_sales の元スレッドで聞く。"""
    text = monitor.completion_report_text(
        {"ts": "100.0", "user": "U報告者"}, CONTRACT_欠けあり, done=["#repitte-hotel への転記"])

    assert "・請求方法、予約番契約有無のご共有については、<@U報告者>さんよろしくお願いします！" in text


def test_足りない項目が無ければその行は出さない():
    """弾く側。埋まっているのに「ご共有ください」と言わない。"""
    text = monitor.completion_report_text(
        {"ts": "100.0", "user": "U報告者"}, CONTRACT, done=["#repitte-hotel への転記"])

    assert "ご共有" not in text
    assert "*残っているタスク*" not in text


# ── 事業計画の反映は、こちらでは出さない ──────────────────────────────

def test_事業計画の反映はこちらでは出さない():
    """★ 2026-09-10 のレビュー指摘。リピッテホテルは転記のあとに
    cnctor-onboarding が同じ元スレッドへ完了報告を出す。両方が同じお願いを
    積むと、同じ人に同じ依頼が2回届く。締めの依頼は初期構築側に一本化した。"""
    for text in (CONTRACT, CONTRACT_欠けあり):
        out = monitor.completion_report_text(
            {"ts": "100.0", "user": "U報告者"}, text, done=["#repitte-hotel への転記"])
        assert "事業計画" not in out


# ── 相手が決まらないものは並べない ────────────────────────────────────

def test_報告者が分からなければ聞く相手を書かない():
    """★ `<@>` はSlack上でただの文字列になり、**通知が飛ばない**。

    誰宛か分からない残タスクは、全員が自分以外の仕事だと思って誰もやらない。"""
    text = monitor.completion_report_text(
        {"ts": "100.0"}, CONTRACT_欠けあり, done=["#repitte-hotel への転記"])

    assert "<@>" not in text
    assert "*残っているタスク*" not in text


def test_assignees_textは未設定を落として繋ぐ():
    """値そのものを固定する（区切りを「、」から変えたら落ちる）。"""
    assert monitor.assignees_text("U1", "U2") == "<@U1>さん、<@U2>さん"
    assert monitor.assignees_text("U1", "") == "<@U1>さん"
    assert monitor.assignees_text("", "") == ""


# ── できたことの中身 ──────────────────────────────────────────────────

def test_添付を転送したら件数まで報告する(state_dir, monkeypatch, 投稿):
    state_dir.write_text("50.0")
    monkeypatch.setattr(monitor, "fetch_all_messages", lambda channel, oldest: [
        {"ts": "100.0", "text": CONTRACT, "user": "U報告者",
         "files": [{"name": "契約書.pdf"}, {"name": "申込書.pdf"}]}])
    monkeypatch.setattr(monitor, "upload_file", lambda f, channel: None)
    monitor.main()

    assert "・添付ファイル2件の転送" in 完了報告(投稿)[0]["text"]


def test_添付が無ければ添付の行は出さない(state_dir, monkeypatch, 投稿):
    """弾く側。0件を「0件の転送」と書かない。"""
    state_dir.write_text("50.0")
    monkeypatch.setattr(monitor, "fetch_all_messages",
                        lambda channel, oldest: [{"ts": "100.0", "text": CONTRACT, "user": "U報告者"}])
    monitor.main()

    assert "添付ファイル" not in 完了報告(投稿)[0]["text"]


# ── 添付の部分成功（2026-09-10 の GPT-5.6 Sol 指摘） ──────────────────

def test_添付が途中で失敗しても転送できた分は報告する(state_dir, monkeypatch, 投稿):
    """★ 「2件中1件は転送済み」を伝えないと、読んだ人が現物と突き合わせられない。

    以前は例外を投げ直していたので、転送できた件数が呼び出し元に戻らず、
    添付が全部失敗したかのような報告になっていた。"""
    state_dir.write_text("50.0")
    monkeypatch.setattr(monitor, "fetch_all_messages", lambda channel, oldest: [
        {"ts": "100.0", "text": CONTRACT, "user": "U報告者",
         "files": [{"name": "契約書.pdf"}, {"name": "申込書.pdf"}]}])

    転送 = []

    def 申込書で落ちる(f, channel):
        if f["name"] == "申込書.pdf":
            raise Exception("ダウンロードに失敗")
        転送.append(f["name"])

    monkeypatch.setattr(monitor, "upload_file", 申込書で落ちる)
    monitor.main()

    報告 = 完了報告(投稿)
    assert len(報告) == 1, "報告が二重に出ている"
    text = 報告[0]["text"]
    assert "・添付ファイル1件の転送" in text        # できたこと
    assert "・添付ファイル1件の転送" in text        # できなかったこと（残り1件）
    assert "*できたこと*" in text and "*できなかったこと*" in text
    assert 報告[0]["channel"] == monitor.JOB_SALES_CHANNEL_ID
    assert state_dir.read_text().strip() == "100.0"   # 本体の転記は済んだので進める


def test_添付が全部失敗したらできたことに添付を書かない(state_dir, monkeypatch, 投稿):
    """弾く側。0件しか転送できていないのに「転送しました」と書かない。"""
    state_dir.write_text("50.0")
    monkeypatch.setattr(monitor, "fetch_all_messages", lambda channel, oldest: [
        {"ts": "100.0", "text": CONTRACT, "user": "U報告者", "files": [{"name": "契約書.pdf"}]}])
    monkeypatch.setattr(monitor, "upload_file",
                        lambda f, channel: (_ for _ in ()).throw(Exception("失敗")))
    monitor.main()

    text = 完了報告(投稿)[0]["text"]
    assert "*できたこと*\n・#repitte-hotel への転記\n" in text
    assert "・添付ファイル1件の転送" in text.split("*できなかったこと*")[1]
    assert "手で貼って" in text          # 次にやることが書いてある


def test_完了報告そのものが失敗しても添付の失敗と混同しない(state_dir, monkeypatch, capsys):
    """★ 添付は全部転送できているのに「添付が失敗」と伝えると、
    読んだ人が同じファイルをもう一度貼ってしまう。"""
    state_dir.write_text("50.0")
    monkeypatch.setattr(monitor, "fetch_all_messages", lambda channel, oldest: [
        {"ts": "100.0", "text": CONTRACT, "user": "U報告者", "files": [{"name": "契約書.pdf"}]}])
    monkeypatch.setattr(monitor, "upload_file", lambda f, channel: None)

    出た = []

    def fake_post(method, **body):
        if body.get("channel") == monitor.JOB_SALES_CHANNEL_ID:
            出た.append(body)
            raise Exception("スレッドが見つかりません")
        return {"ts": "999.0"}

    monkeypatch.setattr(monitor, "slack_post", fake_post)
    monitor.main()

    assert all("添付ファイル1件の転送" not in b["text"].split("*できなかったこと*")[-1]
               or "*できなかったこと*" not in b["text"] for b in 出た), \
        "添付は成功しているのに失敗として報告している"
    assert "完了報告に失敗" in capsys.readouterr().out
