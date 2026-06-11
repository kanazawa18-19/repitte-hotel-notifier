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


def read_last_ts():
    if os.path.exists(STATE_FILE) and os.path.getsize(STATE_FILE) > 0:
        return open(STATE_FILE).read().strip()
    return str(datetime.now(timezone.utc).timestamp() - 600)


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


def upload_file(f, channel, comment=None):
    url = f.get("url_private_download") or f.get("url_private")
    if not url:
        return
    content = requests.get(url, headers=HEADERS).content

    resp = requests.post(
        "https://slack.com/api/files.getUploadURLExternal",
        headers=HEADERS,
        json={"filename": f.get("name", "file"), "length": len(content)},
    ).json()

    requests.post(resp["upload_url"], data=content)

    payload = {"files": [{"id": resp["file_id"]}], "channel_id": channel}
    if comment:
        payload["initial_comment"] = comment
    requests.post(
        "https://slack.com/api/files.completeUploadExternal",
        headers=HEADERS,
        json=payload,
    )


def main():
    oldest = read_last_ts()
    data = slack_get("conversations.history", channel=JOB_SALES_CHANNEL_ID, oldest=oldest, limit=50)
    messages = data.get("messages", [])

    if not messages:
        return

    latest_ts = max(m["ts"] for m in messages)

    for msg in sorted(messages, key=lambda m: m["ts"]):
        text = msg.get("text", "")
        if "契約サービス：リピッテホテル" not in text:
            continue

        new_text = transform(text)
        files = msg.get("files", [])

        if files:
            for i, f in enumerate(files):
                upload_file(f, REPITTE_HOTEL_CHANNEL_ID, comment=new_text if i == 0 else None)
        else:
            slack_post("chat.postMessage", channel=REPITTE_HOTEL_CHANNEL_ID, text=new_text)

    write_last_ts(latest_ts)


if __name__ == "__main__":
    main()
