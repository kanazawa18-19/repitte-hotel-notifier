import json
import math
import os
import re
import requests
from datetime import datetime, timedelta, timezone

TOKEN = os.environ["SLACK_BOT_TOKEN"]
JOB_SALES_CHANNEL_ID = os.environ["JOB_SALES_CHANNEL_ID"]
REPITTE_HOTEL_CHANNEL_ID = os.environ["REPITTE_HOTEL_CHANNEL_ID"]
HIRAMOTO_USER_ID = os.environ["HIRAMOTO_USER_ID"]
TAKESUE_USER_ID = os.environ["TAKESUE_USER_ID"]
REPITTE_TEAM_GROUP_ID = os.environ["REPITTE_TEAM_GROUP_ID"]
# ★ 転記文の末尾に置く「元の投稿」リンクを組み立てるためのワークスペース名
#   （https://<ここ>.slack.com/...）。**未設定ならリンクを出さない。**
#   下流の cnctor-onboarding は、このリンクから
#   「#job_sales のどの投稿の話か」を復元して、同じスレッドへ完了報告を出す。
SLACK_WORKSPACE = os.environ.get("SLACK_WORKSPACE", "").strip()

HEADERS = {"Authorization": f"Bearer {TOKEN}"}
STATE_FILE = "last_processed.txt"

REQUIRED_FIELDS = [
    ("課金開始月",    ["課金開始月", "課金月"]),
    ("請求方法",      ["請求方法"]),
    ("予約番契約有無", ["予約番契約有無", "予約番"]),
]


# stateがまだ無いときに、どこまでさかのぼって読み始めるか（秒）。
# ★ 起点が確実に保存されるようになった（ensure_last_ts）ので、この値が使われるのは
#   stateを失った直後の1回だけ。実行間隔の広さ（AGENTS.md 参照）とは無関係になった。
#   長くすると、その1回で古い契約報告をまとめて転記してしまうので広げない。
INITIAL_LOOKBACK_SEC = 1800

# ページングの打ち切り。50件×20ページ＝1000件あれば、実測の流量（1日約8件）に対して
# どれだけ取得窓が開いても足りる。これを超えるのは応答が異常なとき。
MAX_PAGES = 20

# stateに入っていてよい「未来」の幅（秒）。
#
# ★ なぜ未来のtsが致命的か
#   stateの値はそのまま conversations.history の oldest に渡る。未来の時刻を渡すと
#   「それより後の投稿」は当分のあいだ存在しないので、Slackは毎回0件を返す。
#   例外も出ず、Actionsは緑のまま、ログには [EMPTY] が並ぶだけで、
#   **契約報告が届いても永久に転記されない**。#job_sales が静かなだけの回と
#   ログ上まったく見分けがつかないため、こちらから検知しないと誰も気づけない。
#   しかも advance_last_ts が巻き戻しを禁じているので、放っておいても直らない。
#
# ★ なぜ0ではなく余裕を持たせるか
#   stateに入るのはSlackが発行したtsで、それを読むのはGitHubのランナー。
#   両者の時計は完全には一致しないので、わずかな先行は正常の範囲。
#   ここを0にすると、正常なstateを「壊れている」と誤判定して読み直しを繰り返す。
#   誤報を鳴らし続けると、本物の通知まで無視されるようになる。
#
# ★ この幅がそのまま「見逃す時間」になる
#   許容した未来の値は引き直されないので、現在時刻が追いつくまでSlackは0件を返す。
#   その間に届いた投稿は、追いついた頃には「起点より古い」ので二度と取れない。
#
#     許容幅300秒 → 最大5分ぶんの投稿を落としうる
#     許容幅60秒  → 最大1分
#
#   GitHub のランナーはNTP同期されていて、ずれは実質1秒未満。60秒あれば
#   誤判定は起きない。想定している壊れ方（ミリ秒のepochを書く等）は
#   桁が丸ごと違うので、幅を狭めても検知できる。
FUTURE_TS_TOLERANCE_SEC = 60

# stateとして受け入れる「古さ」の上限（秒）。
#
# ★ 未来だけ見ても足りない（2026-09-10 の GPT-5.6 Sol 指摘）。
#   `1` / `100` / `123456` は有限で正なので、上の検査を全部すり抜ける。
#   これが oldest に渡ると別の形の永久停止になる。
#
#     oldest=1 → #job_sales の一番古い履歴からページングが始まる
#              → MAX_PAGES に到達して例外
#              → stateは 1 のまま（進めないので直らない）
#              → 次回も、その次も、同じところで落ち続ける
#
#   未来のときと違って実行は赤くなるが、**止まっていることに変わりはない**。
#   「壊れたstateから自力で戻る」のが目的なので、ここも引き直しの対象にする。
#
# ★ なぜ90日か
#   ページングの上限は 50件×20ページ＝1000件。#job_sales は1日約8件なので、
#   1000件はおよそ125日ぶん（流量の正本は AGENTS.md）。90日なら余裕を持って
#   追いつける範囲に収まる。実測の最大実行間隔は2日なので、正常なstateを
#   誤って弾くことはない。
#
# ★ 90日より古いstateを弾くと、その間の投稿は取り返せない。
#   だが弾かなければ**そこから先も永久に止まる**ので、動かす方を選ぶ。
#   取りこぼしは通知で人に伝え、scripts/probe_flow.py で事後に数える。
MAX_CATCHUP_SEC = 90 * 24 * 60 * 60

# 「stateを直した」お知らせのうち、まだ人へ伝えられていない分の置き場。
#
# ★ なぜファイルに残すのか
#   直すのは即座にやる（遅らせた分だけ欠落が広がるため）。すると次の回からは
#   正常なstateに見えるので、Slackが不調でお知らせが届かなかった回のことを
#   **二度と言い直せない**。壊れていた間に届いた契約報告は転記できていないので、
#   人が現物を確認できないと困る。だから「まだ言えていない」ことだけを残しておき、
#   伝わるまで毎回言い直す。
REPAIR_NOTICE_FILE = "pending_repair_notice.json"

JST = timezone(timedelta(hours=9))


def jst(ts):
    """ログに出す用に、Slackのts（UTCのepoch秒）を日本時間の表記へ直す。

    ログを読むのはエンジニアではないので、`1788991268.123` のままでは何も分からない。"""
    try:
        return datetime.fromtimestamp(float(ts), JST).strftime("%m-%d %H:%M")
    except (TypeError, ValueError):
        return str(ts)


def now_ts():
    """現在時刻をSlackのtsと同じ形（UTCのepoch秒）で返す。"""
    return datetime.now(timezone.utc).timestamp()


def _read_state_raw():
    """stateファイルに入っている「どこまで読んだか」を返す。

    使える値が入っていないときは None を返す
    （無い／空／数値として読めない／時刻として有り得ない／未来を指している）。
    ★ 「使えるか使えないか」の判定はこの関数だけが持つ。
      read_last_ts と ensure_last_ts が同じ判定を別々に書くと、片方だけ直したときにズレる。
    """
    if not (os.path.exists(STATE_FILE) and os.path.getsize(STATE_FILE) > 0):
        return None
    raw = open(STATE_FILE).read().strip()
    try:
        value = float(raw)
    except ValueError:
        print(f"[WARN] stateが数値として読めないため起点を引き直します（値={raw!r}）")
        return None

    # ★ 「floatになる」だけでは通しすぎる。-1 / -inf / nan / 0 も float としては読める。
    #   Slackのtsは1970年からの経過秒なので、有限で正の値であることまで見る。
    if not (math.isfinite(value) and value > 0):
        print(f"[WARN] stateが時刻として有り得ない値なので起点を引き直します（値={raw!r}）")
        return None

    # ★ 古すぎる値も引き直す。有限で正なだけの `1` や `123456` は、
    #   未来とは別の形で永久に止める（MAX_CATCHUP_SEC のコメントを参照）。
    if value < now_ts() - MAX_CATCHUP_SEC:
        print(f"[WARN] stateが古すぎるため起点を引き直します"
              f"（値={raw!r} ＝ {jst(raw)} / 現在={jst(now_ts())}）")
        return None

    # ★ ここが 2026-09-10 に足した上限の検査。
    #   未来を指すstateは数値としては読めるので黙って通ってしまうが、通すと
    #   以後ずっと0件になって転記が全部止まる（FUTURE_TS_TOLERANCE_SEC のコメントを参照）。
    if value > now_ts() + FUTURE_TS_TOLERANCE_SEC:
        print(f"[WARN] stateが未来の時刻を指しているため起点を引き直します"
              f"（値={raw!r} ＝ {jst(raw)} / 現在={jst(now_ts())}）")
        return None

    return raw


def read_last_ts():
    """どこまで読んだかを返す（読むだけ。ファイルは作らない）。"""
    raw = _read_state_raw()
    if raw is not None:
        return raw
    return str(now_ts() - INITIAL_LOOKBACK_SEC)


def ensure_last_ts():
    """読み始める起点を確定し、まだ記録が無ければその場で保存して返す。

    ★ 2026-09-10 の修正の本体。
       以前は「新着が1件も無かった回はstateを書かずに戻る」作りだった。
       stateファイルを失うと二度と作られず、read_last_ts の既定値
       （実行のたびに計算し直される「今から30分前」）が毎回使われる。
       すると実行間隔が空いたぶんの投稿は、どの回の取得範囲にも入らないまま
       二度と読まれない。[[cnctor-onboarding]] では同じ形で実際に2件落としていた。
       ★ 実行間隔は cron の見た目より遥かに広い。実測値の正本は AGENTS.md（1箇所だけに書く）。

    ★ stateに入れてよいのは「Slackが発行したts」と「初回に決めた起点」だけ。
       こちら側の時計から作った値を毎回書き足さない。ランナーの時計がSlackより
       進んでいると「Slackではまだ届いていない時刻」まで読んだことにしてしまう。

    ★ 取得より前に呼ぶこと。Slackの取得が失敗した回でも起点だけは残る。
    """
    raw = _read_state_raw()
    if raw is not None:
        return raw

    # ★ 保存する前に見ること。保存したあとでは、どちらの経路も「ファイルがある」になる。
    existed = os.path.exists(STATE_FILE)
    broken = open(STATE_FILE).read().strip() if existed else ""

    # ★★ 直すのは「すぐ」。伝わるのを待たない。
    #
    #   いったんは「人へ伝わってから直す」も考えられる。直すと次の回からは正常な
    #   stateに見えて、直したこと自体を二度と言い直せなくなるからだ。
    #   だが**この理屈は成り立たない**。引き直す先は「そのとき」から30分前なので、
    #   直すのを遅らせた分だけ、欠落がまるごと広がる。
    #
    #     10:00 破損を検知。Slack投稿だけが失敗している
    #     10:10 / 10:40 / 11:20 にリピッテホテルの契約報告が届く
    #     12:00 Slackが復旧 → 11:30 から読み直す
    #           → 10:10・10:40・11:20 は永久に転記対象外
    #
    #   すぐ直せば09:30から読み直すので、10:10以降は全部拾える。
    #   **止めている時間がそのまま欠落**なので、先に直して動かす。
    #
    #   「言い直せない」ほうは別の手当てで解く。伝わらなかったお知らせは
    #   pending_repair_notice.json に残し、伝わるまで毎回言い直す
    #   （report_pending_repair_notice）。
    started = str(now_ts() - INITIAL_LOOKBACK_SEC)
    write_last_ts(started)
    if not existed:
        print(f"[INIT] stateがまだ無いので（初回）、読み始める起点を {jst(started)} に決めました")
        return started

    # ファイルはあるのに使えなかった＝壊れた／未来を指していた。2回目以降に出たら異常のサイン。
    print(
        f"[REPAIR] ★stateが壊れていたので起点を {jst(started)} に引き直しました"
        f"（直す前={broken!r}）。"
        f"直前 {INITIAL_LOOKBACK_SEC // 60} 分より古い投稿はこの回で読まれません（要確認）"
    )
    # ★ ログ1行では誰も見ない。転記の取りこぼしが起きうる話なので、人に届くところまで上げる。
    detected_at = datetime.now(timezone.utc).isoformat()
    if not safe_slack_post(REPITTE_HOTEL_CHANNEL_ID, _repair_notice_text(detected_at)):
        write_repair_notice({"broken": broken, "repaired_to": started,
                             "detected_at": detected_at})
        print("[ERROR] stateを直したことを知らせられませんでした。伝わるまで毎回言い直します")
    return started


def write_last_ts(ts):
    """★ 一時ファイルへ書いてから置き換える（_write_text_atomic）。

    `open(..., "w")` は**先にファイルを空にしてから**書く。書き込みの途中で
    ジョブがタイムアウトしたりランナーが落ちたりすると、0バイトのstateが残る。
    今回の修正で「壊れたstate」の扱いが、ログ1行から
    **リピッテチームへの通知＋実行を赤くする**に格上げされた。つまり突然死しただけで
    「契約報告が転記されていないおそれがあります」という重い確認依頼が飛ぶ。
    お知らせの方（pending_repair_notice.json）だけをatomicにして、
    こちらを据え置くのは一貫していない（2026-09-10 の shirokuma-sec 指摘）。"""
    _write_text_atomic(STATE_FILE, ts)


def advance_last_ts(ts):
    """どこまで読んだかを進める。**前より古い値では上書きしない。**

    ★ stateが巻き戻ると、次回はそこから全部読み直すことになり、
      すでに転記した契約報告を丸ごともう一度 #repitte-hotel へ投稿する。
      Slackが想定外の応答を返したときや、将来ここを触った人が順序を壊したときの歯止め。

    ★ **書き込み側では「未来かどうか」を見ない。** 見るのは読み取り側
      （`_read_state_raw`）だけ。この非対称は意図的で、揃えてはいけない
      （2026-09-10 の shirokuma-sec が非対称を指摘。検討のうえ非対称を残した）。

      ここへ渡ってくるのは「いま転記し終えたメッセージのts」なので、
      未来に見えるからと記録を拒むと、**次回その契約報告をもう一度転記する**。
      記録側は「実際にどこまで処理したか」を正直に残し、直すのは読み取り側の仕事にする。
    """
    current = read_last_ts()
    try:
        if float(ts) <= float(current):
            print(f"[WARN] stateを巻き戻そうとしたので見送りました（現在={jst(current)} 新={jst(ts)}）")
            return False
    except (TypeError, ValueError):
        pass
    write_last_ts(ts)

    # ★ 保存はする。**そのうえで、保存した事実をこの回のうちに知らせる**
    #   （2026-09-10 の GPT-5.6 Sol 指摘）。
    #
    #     ✗ 未来だから保存しない  → 次回その契約報告をもう一度転記する
    #     ○ 保存はする ＋ 異常なtsを保存したことを即座に検知する
    #
    #   これが無いと、異常値を書いた回は緑のまま終わり、次の実行（GASの起動係で
    #   30分後）でようやく読み取り側が気づく。その1回ぶんの検知の遅れを無くす。
    try:
        if float(ts) > now_ts() + FUTURE_TS_TOLERANCE_SEC:
            print(f"[ERROR] ★未来を指すtsをstateに記録しました"
                  f"（値={ts!r} ＝ {jst(ts)} / 現在={jst(now_ts())}）。"
                  f"Slackかランナーの時計がずれている可能性があります")
            return True
    except (TypeError, ValueError):
        pass
    return False


def _write_text_atomic(path, text):
    """一時ファイルへ書いてから置き換える。書き込みの途中でプロセスが落ちても、
    読み取り側が中途半端な内容や0バイトのファイルを掴まないようにするため。

    `os.replace` は同じファイルシステム上では不可分。途中で落ちれば、
    置き換えが起きていない＝**前の内容がそのまま残る**。"""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def _write_json_atomic(path, data):
    _write_text_atomic(path, json.dumps(data, ensure_ascii=False, indent=2))


def read_repair_notice():
    """まだ人へ伝えられていない「stateを直した」お知らせ。無ければ None。"""
    if not (os.path.exists(REPAIR_NOTICE_FILE) and os.path.getsize(REPAIR_NOTICE_FILE) > 0):
        return None
    try:
        with open(REPAIR_NOTICE_FILE) as f:
            return json.load(f)
    except ValueError:
        # ★ 中身が壊れていても黙って捨てない。捨てると「伝えるべきことがあった」
        #   ことすら分からなくなる。中身の分からないお知らせとして扱い、言い直させる。
        print("[WARN] 未送のお知らせが読めない形になっていました。中身不明として言い直します")
        return {"broken": "(不明)", "repaired_to": "(不明)", "detected_at": ""}


def write_repair_notice(notice):
    """未送のお知らせを保存する。

    ★ すでに未送のお知らせが残っているときは、**先に気づいた時刻の方を残す**
      （2026-09-10 の shirokuma-sec 指摘）。

      Slackが落ち続けている間にstateが2回壊れると、素直に上書きすると
      1回目の記録が消える。人がこのお知らせを読んで決めるのは
      **「どこまでさかのぼって確認するか」**なので、遅い方の時刻で上書きすると、
      1回目から2回目までの分を確認範囲から丸ごと落としてしまう。
      消えた値そのものは `also_broken` に積んで、あとから追えるようにしておく。"""
    existing = read_repair_notice()
    if existing:
        # ★ どちらが新しいかを条件にしない（2026-09-10 の shirokuma-sec 3周目指摘）。
        #   「既存の方が新しければ素直に上書き」にしていると、時計が巻き戻ったときや
        #   将来ここを触る人が順序を誤ったときに、早い方の記録を無条件に捨てる。
        #   残したいのは常に**いちばん早く気づいた時刻**なので、向きを見ずに min を取る。
        気づいた時刻 = [t for t in (existing.get("detected_at"), notice.get("detected_at")) if t]
        notice = dict(notice)
        if 気づいた時刻:
            notice["detected_at"] = min(気づいた時刻)
        # ★ `broken` が無い古い形式でも None を積まない（2026-09-10 の Gemini Pro 指摘）。
        #   人が読むための記録なので、欠けているなら「(不明)」と書いてある方がよい。
        notice["also_broken"] = ((existing.get("also_broken") or [])
                                 + [existing.get("broken") or "(不明)"])
        print("[WARN] 未送のお知らせが残っている間に、もう一度stateが壊れました。"
              "確認していただく範囲を狭めないよう、先に気づいた時刻の方を残します")
    _write_json_atomic(REPAIR_NOTICE_FILE, notice)


def clear_repair_notice():
    if os.path.exists(REPAIR_NOTICE_FILE):
        os.remove(REPAIR_NOTICE_FILE)


def _repair_notice_text(detected_at):
    """「どこまで読んだかの記録が壊れていた」ことを人に伝える文面。

    ★ 「いつ壊れたか」は分からないが、「いつ気づいたか」は分かる。遅れて言い直す
      ことがあるので、気づいた時刻を必ず入れる。入れないと、読んだ人が
      「今さっき壊れた」と誤解して確認範囲を狭めてしまう。"""
    try:
        when = datetime.fromisoformat(detected_at).astimezone(JST).strftime("%m/%d %H:%M")
        stamp = f"（{when} に検知）"
    except (TypeError, ValueError):
        stamp = ""
    # ★ 「読み直しました」と書かない（2026-09-10 の GPT-5.6 Sol 指摘）。
    #   この文面を出す時点で終わっているのは**読み取り位置を戻したところまで**で、
    #   読み直しはこの後。しかもその取得が失敗すれば、その回は1件も読めていない。
    #   遅れて言い直すこともあるので、済んだことだけを書く。
    minutes = INITIAL_LOOKBACK_SEC // 60
    return (
        f"🔴 #job_sales からの自動転記が一時的に止まっていました{stamp}。"
        f"読み取り位置を{minutes}分前まで戻して、復旧させました。\n"
        f"*いつ止まったかは分かりません。* {minutes}分より前に届いた"
        "リピッテホテルの契約報告が、このチャンネルへ転記されていないおそれがあります。\n"
        "\n"
        # ★ 「心当たりのある契約報告をご確認ください」では動けない
        #   （Gemini Pro と GPT-5.6 Sol が独立に指摘）。自分の分しか見ないか、
        #   分からず放置になる。**何と何を照合するのか**を名指しする。
        "*【お願い】* #job_sales の「【契約獲得】」の投稿と、"
        "このチャンネルの転記を照合してください。\n"
        "・「リピッテホテル」を含む契約報告がこちらに無ければ、"
        "お手数ですが手動で共有をお願いします\n"
        f"・逆に、直近{minutes}分ぶんが二重に並んでいる可能性もわずかにあります。"
        "重複を見つけたら片方は無視してください"
    )


def report_pending_repair_notice():
    """まだ伝えられていない「stateを直した」お知らせを言い直す。

    戻り値: まだ伝えられていなければ True。呼び出し側はこの回を赤くする
    （黙って抱えたままにすると、結局誰も知らないまま終わるため）。"""
    notice = read_repair_notice()
    if not notice:
        return False
    if safe_slack_post(REPITTE_HOTEL_CHANNEL_ID, _repair_notice_text(notice.get("detected_at"))):
        clear_repair_notice()
        print("[REPAIR] 前回言えなかったお知らせを伝えました")
        return False
    print("[ERROR] お知らせを言い直しましたが、まだ伝えられていません")
    return True


def safe_slack_post(channel, text, thread_ts=None):
    """Slackへ投稿し、成否を bool で返す（例外にしない）。

    ★ 投稿できたかどうかで分岐したい場所だけで使う。転記の本体では使わない
      （本体は失敗したらその回を打ち切りたいので、例外のままでよい）。"""
    body = {"channel": channel, "text": text}
    if thread_ts:
        body["thread_ts"] = thread_ts
    try:
        slack_post("chat.postMessage", **body)
        return True
    except Exception as e:
        print(f"[WARN] Slack投稿に失敗しました（channel={channel}）: {e}")
        return False


def fetch_all_messages(channel, oldest):
    """conversations.history は新しい順に返すため、has_more の間 cursor を辿って全件取る。

    1回のlimitを超える未処理メッセージが溜まっていると、**古い方が二度と取得できない**。
    そのうえで最新のtsをstateに書くので、落ちた分は永久に復旧しない。

    2026-09-10 の実測では、直近90日で50件を超えた取得窓は0個（#job_sales は1日約8件）。
    つまり今のところ実害は出ていないが、取得窓は何時間も開くことがあるため、
    流量が増えたときに黙って落ちるのを防ぐ保険として入れてある（実測値の正本は AGENTS.md）。"""
    messages = []
    cursor = None
    for _ in range(MAX_PAGES):
        params = {"channel": channel, "oldest": oldest, "limit": 50}
        if cursor:
            params["cursor"] = cursor
        data = slack_get("conversations.history", **params)
        messages.extend(data.get("messages", []))
        if not data.get("has_more"):
            return messages
        cursor = (data.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            # ★ has_more は「まだ取っていないものがある」という意味。
            #   ここで取れた分だけ返すと、呼び出し側はそれを全部だと思って state を進め、
            #   取れなかった古い方を**永久に落とす**。返さずに落とす方が安全。
            raise Exception("conversations.history が has_more=true のまま次のカーソルを返しませんでした")

    # ここに来るのは、has_more が延々と true を返す異常応答のとき。
    # 黙って抜けると「取り切った」と誤解してstateを進め、残りを永久に落とす。
    raise Exception(f"conversations.history が {MAX_PAGES} ページを超えました（応答が異常な可能性）")


def slack_get(method, **params):
    r = requests.get(f"https://slack.com/api/{method}", headers=HEADERS, params=params)
    data = r.json()
    if not data.get("ok"):
        raise Exception(f"{method} failed: {data.get('error')}")
    return data


def slack_post(method, **body):
    r = requests.post(f"https://slack.com/api/{method}", headers=HEADERS, json=body)
    data = r.json()
    if not data.get("ok"):
        raise Exception(f"{method} failed: {data.get('error')}")
    return data


def source_permalink(ts):
    """#job_sales の元投稿へのリンクを組み立てる。取れなければ空文字。

    ★ これは**人が元投稿へ飛ぶための表示物**。機械の判定には使わせない（source_marker を参照）。
    ★ chat.getPermalink を呼ばない。転記のたびにAPIが1本増えるうえ、
      そこが落ちると転記そのものが落ちる。形式は固定なので文字列で作れる。
      例: https://cnctor.slack.com/archives/C0123ABC/p1757000000123456"""
    if not SLACK_WORKSPACE:
        return ""
    return (f"https://{SLACK_WORKSPACE}.slack.com/archives/"
            f"{JOB_SALES_CHANNEL_ID}/p{ts.replace('.', '')}")


# 下流（cnctor-onboarding）が「#job_sales のどの投稿の話か」を復元するための目印。
# ★ URLをパースさせない（2026-09-10 の GPT-5.6 Sol 指摘）。
#   Slack は conversations.history で本文を返すとき、自動リンク化したURLを
#   <https://...> の形（mrkdwn）に変えて返す。生URLを前提にした正規表現は
#   **本番で必ず外れ**、完了報告が #job_sales の元スレッドへ戻らなくなる。
#   本当の識別子は channel と ts で、URLは人に見せるための表示物にすぎない。
#   この形（山括弧もスラッシュ2つも含まない）なら Slack の整形で変わらない。
SOURCE_MARKER_LABEL = "元投稿ID"


def source_marker(ts):
    """下流が元スレッドを特定するための機械可読な目印。**ワークスペース名が無くても出せる。**"""
    return f"{SOURCE_MARKER_LABEL}: {JOB_SALES_CHANNEL_ID}/{ts}"


def transform(text, source_ts=None):
    """#job_sales の契約報告を #repitte-hotel 向けに書き換える。

    ★ 「契約サービス：」行は消さない（2026-09-10 に修正）。
      以前はこの行を消していたが、下流の cnctor-onboarding が
      「契約サービス行が無い＝転記bot経由＝リピッテホテル本体の契約」と
      判定しているため、オプション契約（例:「リピッテホテル連携WebHook Hub」）を
      転記すると本体契約として契約・解約リストに1行足されてしまっていた。
      オプション名にも「リピッテホテル」が含まれるので、このbotの検知条件
      （【契約獲得】＋リピッテホテル）だけでは区別できない。
      行を残せば下流の _is_repitte_target() が値を見て正しくスキップできる。

    ★ 「月額費用：」の粗利は引き続き伏せる。こちらは社外に出さない情報。
    """
    text = text.replace(
        f"<@{HIRAMOTO_USER_ID}>",
        f"<@{TAKESUE_USER_ID}> <!subteam^{REPITTE_TEAM_GROUP_ID}>"
    )
    lines = []
    for line in text.splitlines():
        if line.strip().startswith("月額費用："):
            line = re.sub(r"（[^）]*粗利[^）]*）", "", line)
        lines.append(line)
    body = "\n".join(lines).rstrip() + "\n\nお手数ですが、\nKintoneの更新をお願いします！"
    if not source_ts:
        return body
    # ★ この行を消すと、初期構築botの完了報告が #job_sales の元スレッドへ戻れなくなる。
    #   リンクは人が飛ぶため（ワークスペース名が未設定なら出ない）、
    #   目印は下流の機械が読むため（常に出る）。役割が違うので両方を残す。
    link = source_permalink(source_ts)
    marker = source_marker(source_ts)
    return body + (f"\n\n（元の投稿: {link} ／ {marker}）" if link
                   else f"\n\n（{marker}）")


def missing_fields(text):
    return [label for label, keywords in REQUIRED_FIELDS if not any(kw in text for kw in keywords)]


def upload_file(f, channel):
    """添付ファイルを #repitte-hotel へ転送する。

    ★ 失敗したら**必ず例外にする**（2026-09-10）。
      以前はログを1行出して黙って戻っていたため、呼び出し側の
      「失敗したらスレッドに警告を残す」経路をすり抜け、
      **添付が付かないまま誰にも気づかれず終わっていた**。
      契約書の添付が落ちるのは、転記そのものが落ちるのと同じくらい困る。"""
    url = f.get("url_private_download") or f.get("url_private")
    if not url:
        raise Exception(f"添付にダウンロード先のURLがありません: {f.get('name')}")

    got = requests.get(url, headers=HEADERS)
    if not got.ok:
        raise Exception(f"添付のダウンロードに失敗しました（HTTP {got.status_code}）: {f.get('name')}")
    content = got.content

    resp = requests.post(
        "https://slack.com/api/files.getUploadURLExternal",
        headers=HEADERS,
        data={"filename": f.get("name", "file"), "length": len(content)},
    ).json()

    if not resp.get("ok"):
        raise Exception(f"files.getUploadURLExternal failed: {resp.get('error')}")

    put = requests.post(resp["upload_url"], data=content)
    if not put.ok:
        raise Exception(f"アップロード先への送信に失敗しました（HTTP {put.status_code}）")

    result = requests.post(
        "https://slack.com/api/files.completeUploadExternal",
        headers=HEADERS,
        json={"files": [{"id": resp["file_id"]}], "channel_id": channel},
    ).json()

    if not result.get("ok"):
        raise Exception(f"files.completeUploadExternal failed: {result.get('error')}")


def post_contract(msg, text):
    """契約報告を #repitte-hotel へ転記する。転記したメッセージの ts を返す。

    ★ ここが失敗した時点では、まだ何も投稿されていない。呼び出し側は state を進めずに
      打ち切ってよい（次回やり直しても二重にならない）。"""
    result = slack_post("chat.postMessage", channel=REPITTE_HOTEL_CHANNEL_ID,
                       text=transform(text, msg["ts"]))
    return result["ts"]


def assignees_text(*user_ids):
    """お願いする相手を `<@Uxxx>さん、<@Uyyy>さん` の形に整える。

    ★ 未設定のIDは黙って落とす。`<@>` のまま出すと、誰宛か分からないうえに
      Slack上ではただの文字列になって**通知も飛ばない**。
      1人も残らなければ空文字を返し、呼び出し側がその行ごと出さない。"""
    named = [f"<@{uid}>さん" for uid in user_ids if uid]
    return "、".join(named)


def remaining_tasks(msg, text):
    """人にお願いする残りを [(やること, お願いする相手), ...] で返す。

    ★ 「相手が決まらないものは並べない」。
      誰に頼むか書いていない残タスクは、全員が自分以外の仕事だと思って誰もやらない。"""
    tasks = []

    absent = missing_fields(text)
    poster = msg.get("user")
    if absent and poster:
        # 足りない項目を知っているのは報告した本人だけ。#job_sales の元スレッドで聞く。
        tasks.append(("、".join(absent) + "のご共有", assignees_text(poster)))

    # ★ 「事業計画の反映」はここでは出さない（2026-09-10 のレビュー指摘）。
    #   リピッテホテルは、この転記のあとに cnctor-onboarding が同じ元スレッドへ
    #   完了報告を出す。両方が同じお願いを積むと、同じ人に同じ依頼が2回届く。
    #   契約の締めとして出すのは初期構築側の役目にして、こちらは転記の話だけに絞る。
    return [(what, who) for what, who in tasks if who]


def completion_report_text(msg, text, done, failed=(), note=""):
    """完了報告の本文を組み立てる。**できてもできなくても出す**（2026-09-10 本人指示）。

    ★ 「できたこと」と「できなかったこと」を必ず両方書く。
      できたことだけ並べると、途中で止まった回が成功した回と見分けられない。"""
    head = "✅ リピッテホテルの契約報告を転記しました！" if not failed else \
           "⚠️ リピッテホテルの契約報告の転記が、途中までで止まりました！"
    lines = [head, ""]

    if done:
        lines.append("*できたこと*")
        lines += [f"・{item}" for item in done]
        lines.append("")

    if failed:
        lines.append("*できなかったこと*")
        lines += [f"・{item}" for item in failed]
        lines.append("")

    if note:
        lines.append(note)
        lines.append("")

    tasks = remaining_tasks(msg, text)
    if tasks:
        lines.append("*残っているタスク*")
        lines += [f"・{what}については、{who}よろしくお願いします！" for what, who in tasks]

    return "\n".join(lines).rstrip()


def post_completion_report(msg, text, done, failed=(), note=""):
    """#job_sales の契約報告の**元スレッド**へ、どこまでやったかを報告する。

    ★ 報告先は常に #job_sales の元スレッド（2026-09-10 本人指示）。
      転記先（#repitte-hotel）のスレッドに出しても、報告した本人と営業部は見に行かない。
      「どこまでやったか」は、契約を報告した場所に戻して初めて人の目に入る。"""
    slack_post(
        "chat.postMessage",
        channel=JOB_SALES_CHANNEL_ID,
        thread_ts=msg["ts"],
        text=completion_report_text(msg, text, done, failed, note),
    )


def safe_report(msg, text, done, failed=(), note=""):
    """完了報告を出す。**出せなくても監視は止めない。**

    ★ 例外を握りつぶしてよい唯一の場所。ここは「人に知らせる」ための処理で、
      転記そのものは既に終わっているか、既に失敗が確定している。
      ここで例外を上げ直すと、報告できなかったせいで後続の契約報告まで止まる。"""
    try:
        post_completion_report(msg, text, done, failed, note)
        return True
    except Exception as e:
        print(f"[ERROR] 完了報告そのものに失敗しました（{jst(msg['ts'])}）: {e}")
        return False


def post_extras(msg, text, message_ts):
    """添付を #repitte-hotel へ転送し、#job_sales の元スレッドへ完了報告を出す。

    ★ ここが失敗しても、本体の転記はすでに済んでいる。呼び出し側は state を進めること。
      進めないと、次回また本体から転記し直して #repitte-hotel に同じ報告が二重に並ぶ。"""
    done = ["#repitte-hotel への転記"]

    files = msg.get("files", [])
    転送済み = 0
    添付の失敗 = None
    for f in files:
        try:
            upload_file(f, REPITTE_HOTEL_CHANNEL_ID)
        except Exception as e:
            # ★ ここで打ち切るが、例外は投げ直さない（2026-09-10 の GPT-5.6 Sol 指摘）。
            #   投げ直すと、呼び出し元が「添付も完了報告も失敗」とまとめて扱い、
            #   **何件転送できたかが人に伝わらない**。転送済みの分は正しく報告する。
            添付の失敗 = e
            break
        転送済み += 1

    if 転送済み:
        done.append(f"添付ファイル{転送済み}件の転送")

    if 添付の失敗 is None:
        post_completion_report(msg, text, done)
        return

    print(f"[ERROR] 添付の転送に失敗しました（{jst(msg['ts'])}）: {添付の失敗}")
    残り = len(files) - 転送済み
    post_completion_report(
        msg, text, done,
        failed=[f"添付ファイル{残り}件の転送"],
        note=("*自動ではやり直しません。* お手数ですが #repitte-hotel の転記を開いて、"
              "足りない添付ファイルをそのスレッドに手で貼っていただけますか。\n"
              "すでに付いているものを貼り直す必要はありません。"),
    )


def main():
    # 前回「stateを直した」ことを伝えられなかった分を、まず言い直す。
    # 直すのは即座にやってしまうので、これを残しておかないと
    # 「壊れていた間の取りこぼしを確認してください」が永久に伝わらない。
    #
    # ★ ここで例外を上げない。お知らせが出せないことを理由に監視まで止めると、
    #   伝えたかった取りこぼしがその間さらに増える。赤くするのは全部やり切った後。
    # ★ 戻り値は「まだ伝えられていない＝True」。失敗側がTrueなので読み違えないこと。
    try:
        notice_still_pending = report_pending_repair_notice()
    except Exception as e:
        print(f"[ERROR] stateを直したお知らせの言い直しに失敗しました: {e}")
        notice_still_pending = True

    # 読み始める起点を確定して保存する。取得より前に呼ぶこと（詳細は ensure_last_ts）。
    oldest = ensure_last_ts()

    recorded_future = _transcribe_new_messages(oldest)

    # ★ 最後に赤くする。監視も転記も終わったうえで「人に伝わっていない」ことだけを残す。
    #   ワークフローの Save state は if: always() なので、赤くしても起点は保存される。
    #
    # ★★ 判定が2つあるのは、拾う対象が違うから。**片方だけにしない**
    #    （2026-09-10 の obasan-quality 指摘）。
    #
    #      notice_still_pending  … **前回まで**に残っていた分を、今回も伝えられなかった
    #      read_repair_notice()  … **今回** ensure_last_ts が新しく破損を見つけ、
    #                              そのお知らせも伝えられなかった
    #
    #    後者を落とすと、「この回に壊れて、この回に伝え損ねた」場合だけが静かに素通りし、
    #    実行は緑のまま終わる。いちばん知らせたい回が、いちばん漏れやすくなる。
    # ★ raise を2本並べない（2026-09-10 の shirokuma-sec 3周目指摘）。
    #   並べると、両方成立したときに2つ目がActionsのエラー文に一切出ない。
    #   ログには残るし次回自己検知もするが、**エラー文だけを見た人は片方に気づけない**。
    問題 = []
    if notice_still_pending or read_repair_notice():
        問題.append("stateが壊れていたことを #repitte-hotel へ知らせられていません。"
                    "壊れていた間の契約報告が転記されていないおそれがあります")
    if recorded_future:
        問題.append("未来を指すtsをstateに記録しました。転記そのものは済んでいますが、"
                    "次の実行で起点が引き直され、直近30分ぶんを読み直します")
    if 問題:
        raise RuntimeError("（要確認）" + " ／ ".join(問題))


def _transcribe_new_messages(oldest):
    """新着を取得し、リピッテホテルの契約報告を転記して、読んだところまでstateを進める。"""
    messages = fetch_all_messages(JOB_SALES_CHANNEL_ID, oldest)

    if not messages:
        # 新着ゼロのときstateは据え置く。起点は既に保存済みなので、次回も同じ場所から
        # 読み直すだけで隙間は生まれない。
        # 「起動しているのに何も進んでいない」が起きうる場所そのものなので、
        # 空振りだったこともログに残す（出さないとActionsのログから区別できない）。
        print(f"[EMPTY] 新着0件（{jst(oldest)} 以降）。stateは据え置きます")
        return False

    # ★ 記録するのは「窓の中で一番新しいts」ではなく「処理し終えた最後のts」。
    #   途中で失敗した回に窓の端まで進めてしまうと、まだ転記していない契約報告を
    #   飛び越して二度と読まなくなる。逆に一切進めないと、転記済みの分を再投稿する。
    #
    # ★ しかも「1件ずつ」は**その場でファイルに書く**という意味でなければならない。
    #   ループの外で1回だけ書く作りだと、途中で例外ではなく突然死したとき
    #   （ジョブのタイムアウト・SIGKILL・ランナー障害）に書き込み自体が起きず、
    #   転記済みの契約報告を次回また投稿する。
    posted = 0
    # 未来を指すtsを記録したか。記録そのものは拒まず、この回のうちに人へ知らせるため
    # （詳細は advance_last_ts）。
    recorded_future = False

    # 並べ替えは時刻として行う（文字列の辞書順だと桁数が変わったときに順序が狂う）。
    for msg in sorted(messages, key=lambda m: float(m["ts"])):
        text = msg.get("text", "")
        if "【契約獲得】" not in text or "リピッテホテル" not in text:
            # 転記の対象外。読み終えたので進めてよい
            recorded_future |= advance_last_ts(msg["ts"])
            continue

        try:
            message_ts = post_contract(msg, text)
        except Exception as e:
            # まだ #repitte-hotel には何も出ていない。ここで打ち切り、次回この投稿からやり直す。
            # 先へ進めると、この契約報告だけが誰にも転記されないまま埋もれる。
            print(f"[ERROR] 転記に失敗したのでここで打ち切ります（{jst(msg['ts'])}）: {e}")
            # ★ できなかった回も必ず報告する（2026-09-10 本人指示）。
            #   黙って打ち切ると、#job_sales からは「まだ誰も見ていない」のと
            #   区別が付かない。Slackごと不調なら報告も飛ばないが、そのときは
            #   次の実行でやり直すので、言えなかったこと自体は失われない。
            safe_report(msg, text, done=[], failed=["#repitte-hotel への転記"],
                        note="*次の実行でやり直します。* 手で転記しないでください（二重に並びます）。")
            break

        # ★ 投稿した直後に、その場で記録する。あとでまとめて書くと、
        #   ここから下で突然死したときに「投稿はされたが記録は無い」状態になり、
        #   次回この契約報告をもう一度投稿する。
        recorded_future |= advance_last_ts(msg["ts"])
        posted += 1

        try:
            post_extras(msg, text, message_ts)
        except Exception as e:
            # 本体の転記は済んでいるので、stateは進めたまま次へ行く（重複より欠落を選ぶ）。
            # ただしログ1行では誰も見ないので、転記したメッセージのスレッドに残して人が拾えるようにする。
            # ★ ここに来るのは「完了報告そのものを出せなかった」場合だけ。
            #   添付の失敗は post_extras が自分で報告するので、ここでは扱わない
            #   （混ぜると、添付は全部転送できているのに「添付が失敗」と伝えてしまう）。
            print(f"[ERROR] 完了報告に失敗しました（{jst(msg['ts'])}）: {e}")
            safe_report(msg, text, done=["#repitte-hotel への転記"])

    # ★ 「動いた」ではなく「何件仕事をしたか」を必ず出す。
    #   成功時のログが無いと、正常な回と何も起きていない回をあとから区別できない。
    print(f"[DONE] 取得{len(messages)}件 / 転記{posted}件（{jst(oldest)} 以降）")
    return recorded_future


if __name__ == "__main__":
    main()
