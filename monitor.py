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


def read_last_ts():
    if os.path.exists(STATE_FILE) and os.path.getsize(STATE_FILE) > 0:
        return open(STATE_FILE).read().strip()
    return str(datetime.now(timezone.utc).timestamp() - 1800)


def write_last_ts(ts):
    with open(STATE_FILE, "w") as f:
        f.write(ts)


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
    text = text.replace(
        f"<@{HIRAMOTO_USER_ID}>",
        f"<@{TAKESUE_USER_ID}> <!subteam^{REPITTE_TEAM_GROUP_ID}>"
    )
    lines = []
    for line in text.splitlines():
        if line.strip().startswith("契約サービス："):
            continue
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
    oldest = read_last_ts()
    data = slack_get("conversations.history", channel=JOB_SALES_CHANNEL_ID, oldest=oldest, limit=50)
    messages = data.get("messages", [])

    if not messages:
        return

    latest_ts = max(m["ts"] for m in messages)

    for msg in sorted(messages, key=lambda m: m["ts"]):
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
