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

    使える値が入っていないときは None を返す（無い／空／数値として読めない）。
    ★ 「使えるか使えないか」の判定はこの関数だけが持つ。
      read_last_ts と ensure_last_ts が同じ判定を別々に書くと、片方だけ直したときにズレる。
    """
    if not (os.path.exists(STATE_FILE) and os.path.getsize(STATE_FILE) > 0):
        return None
    raw = open(STATE_FILE).read().strip()
    try:
        float(raw)
    except ValueError:
        print(f"[WARN] stateが数値として読めないため起点を引き直します（値={raw!r}）")
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

    started = str(now_ts() - INITIAL_LOOKBACK_SEC)
    write_last_ts(started)
    if existed:
        # ファイルはあるのに使えなかった＝壊れた／消えかけた。2回目以降に出たら異常のサイン。
        print(
            f"[INIT] ★stateが壊れていたので起点を {jst(started)} に引き直しました。"
            f"直前 {INITIAL_LOOKBACK_SEC // 60} 分より古い投稿はこの回で読まれません（要確認）"
        )
    else:
        print(f"[INIT] stateがまだ無いので（初回）、読み始める起点を {jst(started)} に決めました")
    return started


def write_last_ts(ts):
    with open(STATE_FILE, "w") as f:
        f.write(ts)


def advance_last_ts(ts):
    """どこまで読んだかを進める。**前より古い値では上書きしない。**

    ★ stateが巻き戻ると、次回はそこから全部読み直すことになり、
      すでに転記した契約報告を丸ごともう一度 #repitte-hotel へ投稿する。
      Slackが想定外の応答を返したときや、将来ここを触った人が順序を壊したときの歯止め。
    """
    current = read_last_ts()
    try:
        if float(ts) <= float(current):
            print(f"[WARN] stateを巻き戻そうとしたので見送りました（現在={jst(current)} 新={jst(ts)}）")
            return
    except (TypeError, ValueError):
        pass
    write_last_ts(ts)


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


def transform(text):
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
    return "\n".join(lines).rstrip() + "\n\nお手数ですが、\nKintoneの更新をお願いします！"


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
    result = slack_post("chat.postMessage", channel=REPITTE_HOTEL_CHANNEL_ID, text=transform(text))
    return result["ts"]


def post_extras(msg, text, message_ts):
    """添付ファイルの転送と、不足項目のリマインドを行う。

    ★ ここが失敗しても、本体の転記はすでに済んでいる。呼び出し側は state を進めること。
      進めないと、次回また本体から転記し直して #repitte-hotel に同じ報告が二重に並ぶ。"""
    for f in msg.get("files", []):
        upload_file(f, REPITTE_HOTEL_CHANNEL_ID)

    absent = missing_fields(text)
    poster = msg.get("user")
    if absent and poster:
        items = "\n".join(f"・{field}" for field in absent)
        slack_post(
            "chat.postMessage",
            channel=REPITTE_HOTEL_CHANNEL_ID,
            thread_ts=message_ts,
            text=f"<@{poster}> 以下の情報もご共有いただけますか？\n{items}",
        )


def main():
    # 読み始める起点を確定して保存する。取得より前に呼ぶこと（詳細は ensure_last_ts）。
    oldest = ensure_last_ts()
    messages = fetch_all_messages(JOB_SALES_CHANNEL_ID, oldest)

    if not messages:
        # 新着ゼロのときstateは据え置く。起点は既に保存済みなので、次回も同じ場所から
        # 読み直すだけで隙間は生まれない。
        # 「起動しているのに何も進んでいない」が起きうる場所そのものなので、
        # 空振りだったこともログに残す（出さないとActionsのログから区別できない）。
        print(f"[EMPTY] 新着0件（{jst(oldest)} 以降）。stateは据え置きます")
        return

    # ★ 記録するのは「窓の中で一番新しいts」ではなく「処理し終えた最後のts」。
    #   途中で失敗した回に窓の端まで進めてしまうと、まだ転記していない契約報告を
    #   飛び越して二度と読まなくなる。逆に一切進めないと、転記済みの分を再投稿する。
    #
    # ★ しかも「1件ずつ」は**その場でファイルに書く**という意味でなければならない。
    #   ループの外で1回だけ書く作りだと、途中で例外ではなく突然死したとき
    #   （ジョブのタイムアウト・SIGKILL・ランナー障害）に書き込み自体が起きず、
    #   転記済みの契約報告を次回また投稿する。
    posted = 0

    # 並べ替えは時刻として行う（文字列の辞書順だと桁数が変わったときに順序が狂う）。
    for msg in sorted(messages, key=lambda m: float(m["ts"])):
        text = msg.get("text", "")
        if "【契約獲得】" not in text or "リピッテホテル" not in text:
            advance_last_ts(msg["ts"])   # 転記の対象外。読み終えたので進めてよい
            continue

        try:
            message_ts = post_contract(msg, text)
        except Exception as e:
            # まだ #repitte-hotel には何も出ていない。ここで打ち切り、次回この投稿からやり直す。
            # 先へ進めると、この契約報告だけが誰にも転記されないまま埋もれる。
            print(f"[ERROR] 転記に失敗したのでここで打ち切ります（{jst(msg['ts'])}）: {e}")
            break

        # ★ 投稿した直後に、その場で記録する。あとでまとめて書くと、
        #   ここから下で突然死したときに「投稿はされたが記録は無い」状態になり、
        #   次回この契約報告をもう一度投稿する。
        advance_last_ts(msg["ts"])
        posted += 1

        try:
            post_extras(msg, text, message_ts)
        except Exception as e:
            # 本体の転記は済んでいるので、stateは進めたまま次へ行く（重複より欠落を選ぶ）。
            # ただしログ1行では誰も見ないので、転記したメッセージのスレッドに残して人が拾えるようにする。
            print(f"[ERROR] 添付／リマインドに失敗しました（{jst(msg['ts'])}）: {e}")
            try:
                slack_post(
                    "chat.postMessage",
                    channel=REPITTE_HOTEL_CHANNEL_ID,
                    thread_ts=message_ts,
                    text="⚠️この契約報告の転記は済んでいますが、添付ファイルまたは不足項目の確認で"
                         "エラーが出ました。\nやり直すと同じ報告が二重に並ぶため、自動では再実行しません。"
                         "\nお手数ですが #job_sales の元の投稿をご確認ください。",
                )
            except Exception as notify_error:
                print(f"[ERROR] エラーの通知そのものにも失敗しました: {notify_error}")

    # ★ 「動いた」ではなく「何件仕事をしたか」を必ず出す。
    #   成功時のログが無いと、正常な回と何も起きていない回をあとから区別できない。
    print(f"[DONE] 取得{len(messages)}件 / 転記{posted}件（{jst(oldest)} 以降）")


if __name__ == "__main__":
    main()
