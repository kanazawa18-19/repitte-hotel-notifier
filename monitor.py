import os
import re
import requests
from datetime import datetime, timezone

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
INITIAL_LOOKBACK_SEC = 1800


def now_ts():
    """現在時刻をSlackのtsと同じ形（UTCのepoch秒）で返す。"""
    return datetime.now(timezone.utc).timestamp()


def read_last_ts():
    """どこまで読んだかを返す（読むだけ。ファイルは作らない）。

    stateが無い・数値として読めないときは「今から INITIAL_LOOKBACK_SEC 前」を返す。"""
    if os.path.exists(STATE_FILE) and os.path.getsize(STATE_FILE) > 0:
        raw = open(STATE_FILE).read().strip()
        try:
            float(raw)
            return raw
        except ValueError:
            print(f"[WARN] stateが数値として読めないため起点を引き直します（値={raw!r}）")
    return str(now_ts() - INITIAL_LOOKBACK_SEC)


def ensure_last_ts():
    """読み始める起点を確定し、まだ記録が無ければその場で保存して返す。

    ★ 2026-09-10 の修正の本体。
       以前は「新着が1件も無かった回はstateを書かずに戻る」作りだった。
       stateファイルを失うと二度と作られず、read_last_ts の既定値
       （実行のたびに計算し直される「今から30分前」）が毎回使われる。
       すると実行間隔が30分より空いたぶんの投稿は、どの回の取得範囲にも入らないまま
       二度と読まれない。[[cnctor-onboarding]] では同じ形で実際に2件落としていた。

       このリポジトリの実行間隔は**中央値433分・最大2925分**（stateのgit履歴から実測）。
       30分の既定値では到底足りないので、起点の保存は必須。

    ★ stateに入れてよいのは「Slackが発行したts」と「初回に決めた起点」だけ。
       こちら側の時計から作った値を毎回書き足さない。ランナーの時計がSlackより
       進んでいると「Slackではまだ届いていない時刻」まで読んだことにしてしまう。"""
    if os.path.exists(STATE_FILE) and os.path.getsize(STATE_FILE) > 0:
        existing = read_last_ts()
        if existing == open(STATE_FILE).read().strip():
            return existing
        # 壊れていて既定値に落ちた。読める値へ直しておく。
        write_last_ts(existing)
        return existing

    started = str(now_ts() - INITIAL_LOOKBACK_SEC)
    write_last_ts(started)
    print(f"[INIT] stateが無かったので読み始める起点を {started} に決めました")
    return started


def write_last_ts(ts):
    with open(STATE_FILE, "w") as f:
        f.write(ts)


def fetch_all_messages(channel, oldest):
    """conversations.history は新しい順に返すため、has_more の間 cursor を辿って全件取る。

    1回のlimitを超える未処理メッセージが溜まっていると、**古い方が二度と取得できない**。
    そのうえで最新のtsをstateに書くので、落ちた分は永久に復旧しない。

    2026-09-10 の実測では、直近90日で50件を超えた取得窓は0個（#job_sales は1日約8件）。
    つまり今のところ実害は出ていないが、取得窓は最大2925分まで開くことがあるため、
    流量が増えたときに黙って落ちるのを防ぐ保険として入れてある。"""
    messages = []
    cursor = None
    while True:
        params = {"channel": channel, "oldest": oldest, "limit": 50}
        if cursor:
            params["cursor"] = cursor
        data = slack_get("conversations.history", **params)
        messages.extend(data.get("messages", []))
        if not data.get("has_more"):
            return messages
        cursor = (data.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            return messages


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
    url = f.get("url_private_download") or f.get("url_private")
    if not url:
        return
    content = requests.get(url, headers=HEADERS).content

    resp = requests.post(
        "https://slack.com/api/files.getUploadURLExternal",
        headers=HEADERS,
        data={"filename": f.get("name", "file"), "length": len(content)},
    ).json()

    if not resp.get("ok"):
        print(f"files.getUploadURLExternal failed: {resp}")
        return

    requests.post(resp["upload_url"], data=content)

    result = requests.post(
        "https://slack.com/api/files.completeUploadExternal",
        headers=HEADERS,
        json={"files": [{"id": resp["file_id"]}], "channel_id": channel},
    ).json()

    if not result.get("ok"):
        print(f"files.completeUploadExternal failed: {result}")


def main():
    # 読み始める起点を確定して保存する。ここで保存しておかないと、新着ゼロの回が続く限り
    # stateファイルが作られず、実行のたびに「今から30分前」が計算し直されて、
    # 実行間隔が空いた分の投稿を落とす（詳細は ensure_last_ts）。
    oldest = ensure_last_ts()
    messages = fetch_all_messages(JOB_SALES_CHANNEL_ID, oldest)

    if not messages:
        # 新着ゼロのときstateは据え置く。起点は既に保存済みなので、次回も同じ場所から
        # 読み直すだけで隙間は生まれない。
        # 「起動しているのに何も進んでいない」が起きうる場所そのものなので、
        # 空振りだったこともログに残す（出さないとActionsのログから区別できない）。
        print(f"[EMPTY] 新着0件（{oldest} 以降）。stateは据え置きます")
        return

    # tsは文字列だが、比較は時刻として行う（桁数が変わったときに辞書順で狂わないように）。
    latest_ts = max(messages, key=lambda m: float(m["ts"]))["ts"]

    # 並べ替えも時刻として行う（文字列の辞書順だと桁数が変わったときに順序が狂う）。
    for msg in sorted(messages, key=lambda m: float(m["ts"])):
        text = msg.get("text", "")
        if "【契約獲得】" not in text or "リピッテホテル" not in text:
            continue

        new_text = transform(text)
        files = msg.get("files", [])
        poster = msg.get("user")

        # #repitte-hotel にメッセージ投稿（ts を取得するため先に送る）
        result = slack_post("chat.postMessage", channel=REPITTE_HOTEL_CHANNEL_ID, text=new_text)
        message_ts = result["ts"]

        # 添付ファイルをアップロード
        for f in files:
            upload_file(f, REPITTE_HOTEL_CHANNEL_ID)

        # 不足項目があればスレッドでリマインド
        absent = missing_fields(text)
        if absent and poster:
            items = "\n".join(f"・{field}" for field in absent)
            slack_post(
                "chat.postMessage",
                channel=REPITTE_HOTEL_CHANNEL_ID,
                thread_ts=message_ts,
                text=f"<@{poster}> 以下の情報もご共有いただけますか？\n{items}",
            )

    write_last_ts(latest_ts)


if __name__ == "__main__":
    main()
