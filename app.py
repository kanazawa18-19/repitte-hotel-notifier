import os
import re
import requests
import threading
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

load_dotenv()

SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
JOB_SALES_CHANNEL_ID = os.environ["JOB_SALES_CHANNEL_ID"]
REPITTE_HOTEL_CHANNEL_ID = os.environ["REPITTE_HOTEL_CHANNEL_ID"]

# @Hiramoto の Slack ユーザーID → @Takesue のユーザーID に置換
HIRAMOTO_USER_ID = os.environ["HIRAMOTO_USER_ID"]
TAKESUE_USER_ID = os.environ["TAKESUE_USER_ID"]
# @repitte_team のグループID（Slack管理画面 > ユーザーグループで確認）
REPITTE_TEAM_GROUP_ID = os.environ["REPITTE_TEAM_GROUP_ID"]

app = App(token=SLACK_BOT_TOKEN)


def transform_message(text: str) -> str:
    # @Hiramoto を @Takesue @repitte_team に置換（cc @sales_team はそのまま残す）
    text = text.replace(
        f"<@{HIRAMOTO_USER_ID}>",
        f"<@{TAKESUE_USER_ID}> <!subteam^{REPITTE_TEAM_GROUP_ID}>"
    )

    lines = []
    for line in text.splitlines():
        # 契約サービス行を削除
        if line.strip().startswith("契約サービス："):
            continue
        # 月額費用の粗利情報（括弧内）を削除
        if line.strip().startswith("月額費用："):
            line = re.sub(r"（[^）]*粗利[^）]*）", "", line)
        lines.append(line)

    result = "\n".join(lines).rstrip()
    result += "\n\nお手数ですが、\nKintoneの更新をお願いします！"
    return result


def forward_files(client, files: list, channel: str) -> list[str]:
    """添付ファイルをダウンロードして #repitte-hotel に再アップロード、file_id リストを返す"""
    uploaded_ids = []
    for f in files:
        url = f.get("url_private_download") or f.get("url_private")
        if not url:
            continue
        resp = requests.get(url, headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"})
        if resp.status_code != 200:
            continue
        result = client.files_upload_v2(
            channel=channel,
            content=resp.content,
            filename=f.get("name", "file"),
            title=f.get("title") or f.get("name", "file"),
        )
        if result.get("ok") and result.get("file"):
            uploaded_ids.append(result["file"]["id"])
    return uploaded_ids


@app.event("message")
def handle_message(event, client):
    # サブタイプがある（編集・削除・bot投稿など）は無視
    if event.get("subtype"):
        return
    # #job_sales チャンネル以外は無視
    if event.get("channel") != JOB_SALES_CHANNEL_ID:
        return

    text = event.get("text", "")
    if "契約サービス：リピッテホテル" not in text:
        return

    def process():
        new_text = transform_message(text)
        files = event.get("files", [])

        if files:
            # ファイルを先にアップロード（initial_comment で本文も送る）
            for i, f in enumerate(files):
                url = f.get("url_private_download") or f.get("url_private")
                if not url:
                    continue
                resp = requests.get(url, headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"})
                if resp.status_code != 200:
                    continue
                client.files_upload_v2(
                    channel=REPITTE_HOTEL_CHANNEL_ID,
                    content=resp.content,
                    filename=f.get("name", "file"),
                    title=f.get("title") or f.get("name", "file"),
                    initial_comment=new_text if i == 0 else None,
                )
        else:
            client.chat_postMessage(
                channel=REPITTE_HOTEL_CHANNEL_ID,
                text=new_text,
            )

    threading.Thread(target=process, daemon=True).start()


if __name__ == "__main__":
    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    handler.start()
