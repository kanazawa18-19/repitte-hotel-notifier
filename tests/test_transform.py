"""#job_sales の契約報告を #repitte-hotel 向けに書き換える部分。

★ ここは「社外に出す前の最後の防波堤」でありながら、2026-09-10 まで一度も
  テストされていなかった。壊れたときの実害が2つあるので固定しておく。

   1. 粗利のマスクが外れる      → 社外秘の粗利がリピッテチームへ流れる
   2. 契約サービス行が消える    → 下流の cnctor-onboarding が
                                  オプション契約を本体契約として登録してしまう
"""
import monitor


def test_粗利は伏せられる():
    """月額費用の行から「（…粗利…）」を取り除く。ここが外れると社外秘が漏れる。"""
    out = monitor.transform("月額費用：50,000円（粗利 30,000円）")
    assert "粗利" not in out
    assert "30,000" not in out
    assert "50,000円" in out          # 金額そのものは残す


def test_粗利以外の括弧は消さない():
    """括弧を無条件で消す作りに変えると、必要な補足まで落ちる。"""
    out = monitor.transform("月額費用：50,000円（税込）")
    assert "（税込）" in out


def test_月額費用以外の行の括弧は触らない():
    out = monitor.transform("備考：粗利について確認済み（担当者了承）")
    assert "（担当者了承）" in out


def test_契約サービス行は消さない():
    """★ 2890beb で直した不具合。この行を消すと、下流の cnctor-onboarding が
       「契約サービス行が無い＝リピッテホテル本体の契約」と誤判定し、
       オプション契約が契約・解約リストに1行足されてしまう。"""
    out = monitor.transform("契約サービス：リピッテホテル連携WebHook Hub")
    assert "契約サービス：リピッテホテル連携WebHook Hub" in out


def test_宛先が平本さんからリピッテチームへ差し替わる():
    src = f"<@{monitor.HIRAMOTO_USER_ID}> 契約が取れました"
    out = monitor.transform(src)
    assert monitor.HIRAMOTO_USER_ID not in out
    assert f"<@{monitor.TAKESUE_USER_ID}>" in out
    assert f"<!subteam^{monitor.REPITTE_TEAM_GROUP_ID}>" in out


def test_kintone更新の依頼が末尾に付く():
    out = monitor.transform("【契約獲得】リピッテホテル")
    assert out.rstrip().endswith("Kintoneの更新をお願いします！")


def test_必須項目が揃っていれば何も返らない():
    text = "課金開始月：10月\n請求方法：口座振替\n予約番契約有無：無"
    assert monitor.missing_fields(text) == []


def test_足りない項目だけを拾う():
    assert monitor.missing_fields("課金開始月：10月") == ["請求方法", "予約番契約有無"]


def test_言い方が揺れても拾える():
    """「課金月」「予約番」という短い書き方でも、書かれている扱いにする。
    ここが厳しすぎると、書いてあるのにリマインドが飛んで営業を煩わせる。"""
    text = "課金月：10月\n請求方法：請求書\n予約番：あり"
    assert monitor.missing_fields(text) == []


def test_何も書かれていなければ全部返る():
    assert monitor.missing_fields("【契約獲得】リピッテホテル") == \
        ["課金開始月", "請求方法", "予約番契約有無"]


# ── 元投稿へのリンク（下流の cnctor-onboarding が元スレッドを辿る手がかり） ──

def test_ワークスペース名が無くても目印は必ず出す(monkeypatch):
    """★ 目印（元投稿ID）は下流が元スレッドへ戻るための唯一の手がかり。

    リンクは人が飛ぶための表示物なので、ワークスペース名が無ければ出さなくてよい。
    だが目印まで消すと、初期構築botの完了報告が #job_sales へ戻れなくなる。"""
    monkeypatch.setattr(monitor, "SLACK_WORKSPACE", "")
    out = monitor.transform("【契約獲得】リピッテホテル\n", "1757000000.123456")

    assert "slack.com" not in out
    assert out.endswith(f"（元投稿ID: {monitor.JOB_SALES_CHANNEL_ID}/1757000000.123456）")


def test_リンクと目印を両方添える(monkeypatch):
    monkeypatch.setattr(monitor, "SLACK_WORKSPACE", "cnctor")
    out = monitor.transform("【契約獲得】リピッテホテル\n", "1757000000.123456")

    assert out.endswith(
        f"（元の投稿: https://cnctor.slack.com/archives/{monitor.JOB_SALES_CHANNEL_ID}"
        f"/p1757000000123456 ／ 元投稿ID: {monitor.JOB_SALES_CHANNEL_ID}/1757000000.123456）")


def test_目印はURLの形にしない():
    """★ 2026-09-10 の GPT-5.6 Sol 指摘。

    Slack は本文をAPIで返すとき、自動リンク化したURLを <https://...> に変える。
    目印がURLだと、下流の正規表現が**本番でだけ**外れる。
    山括弧もスラッシュ2つも含まない形に固定しておく。"""
    marker = monitor.source_marker("1757000000.123456")
    assert marker == f"元投稿ID: {monitor.JOB_SALES_CHANNEL_ID}/1757000000.123456"
    assert "://" not in marker and "<" not in marker


def test_リンクのtsは小数点を抜く(monkeypatch):
    """Slackのパーマリンクは `p` のあとに小数点を含めない形式。値そのものを固定する。"""
    monkeypatch.setattr(monitor, "SLACK_WORKSPACE", "cnctor")
    assert monitor.source_permalink("1757000000.123456") == \
        f"https://cnctor.slack.com/archives/{monitor.JOB_SALES_CHANNEL_ID}/p1757000000123456"


def test_ワークスペース名が無ければリンクは空(monkeypatch):
    """弾く側。未設定のまま https://.slack.com/... という壊れたリンクを出さない。"""
    monkeypatch.setattr(monitor, "SLACK_WORKSPACE", "")
    assert monitor.source_permalink("1757000000.123456") == ""


def test_元のtsが無ければ何も添えない(monkeypatch):
    monkeypatch.setattr(monitor, "SLACK_WORKSPACE", "cnctor")
    out = monitor.transform("【契約獲得】リピッテホテル\n")
    assert "元の投稿" not in out and "元投稿ID" not in out


def test_Kintoneの依頼文は消さない(monkeypatch):
    """リンクを足したせいで、既存の依頼文が押し出されていないこと。"""
    monkeypatch.setattr(monitor, "SLACK_WORKSPACE", "cnctor")
    out = monitor.transform("【契約獲得】リピッテホテル\n", "1757000000.123456")

    assert "Kintoneの更新をお願いします！" in out
