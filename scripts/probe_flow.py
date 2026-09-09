"""#job_sales の流量と、転記の取りこぼしを**読むだけ**で確かめる。

なぜ要るか
  monitor.py は conversations.history を limit=50 で1回しか呼ばず、ページングしない。
  Slackは新しい順に返すので、1回の取得窓に51件以上あると**古い方が二度と読まれない**。
  そのうえで最新のtsをstateに書いてしまうため、落ちた分は永久に復旧しない。

  state（last_processed.txt）のgit履歴から、実際の取得窓は
  中央値 433分・最大 2925分（2026-09-10 実測）と分かっている。
  窓がこれだけ広いと50件を超えうるので、「超えたか」「落ちたか」を実データで確かめる。

安全性
  読み取り専用。conversations.history しか呼ばない。投稿も書き込みも一切しない。
"""
import os
import sys
import bisect
import subprocess
from datetime import datetime, timezone, timedelta

import requests

JST = timezone(timedelta(hours=9))
HEADERS = {"Authorization": f"Bearer {os.environ['SLACK_BOT_TOKEN']}"}
JOB_SALES = os.environ["JOB_SALES_CHANNEL_ID"]
REPITTE_HOTEL = os.environ["REPITTE_HOTEL_CHANNEL_ID"]
DAYS = int(os.environ.get("PROBE_DAYS", "30"))


def jst(ts):
    return datetime.fromtimestamp(float(ts), JST).strftime("%m-%d %H:%M")


def fetch_all(channel, oldest):
    """ページングして全部取る。ここは monitor.py と違い、最初から全件読む。"""
    out, cursor = [], None
    while True:
        params = {"channel": channel, "oldest": str(oldest), "limit": 200}
        if cursor:
            params["cursor"] = cursor
        d = requests.get("https://slack.com/api/conversations.history",
                         headers=HEADERS, params=params, timeout=30).json()
        if not d.get("ok"):
            print(f"🔴 {channel}: {d.get('error')}")
            return out
        out.extend(d.get("messages", []))
        if not d.get("has_more"):
            return out
        cursor = (d.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            return out


def state_history():
    """last_processed.txt のgit履歴から、この bot が実際に使った取得窓を復元する。"""
    log = subprocess.run(["git", "log", "--format=%H", "--", "last_processed.txt"],
                         capture_output=True, text=True).stdout.split()
    values = []
    for h in log:
        raw = subprocess.run(["git", "show", f"{h}:last_processed.txt"],
                             capture_output=True, text=True).stdout.strip()
        try:
            values.append(float(raw))
        except ValueError:
            continue
    return sorted(values)


def main():
    now = datetime.now(timezone.utc).timestamp()
    oldest = now - DAYS * 86400

    job = fetch_all(JOB_SALES, oldest)
    hotel = fetch_all(REPITTE_HOTEL, oldest)
    print(f"直近{DAYS}日  #job_sales {len(job)}件 / #repitte-hotel {len(hotel)}件")
    print()

    job_ts = sorted(float(m["ts"]) for m in job)

    # ① 1回の取得窓に何件入るか。50件を超えた窓があれば、そこで古い方を落としている。
    print("--- ① 取得窓ごとの件数（stateの実履歴に沿って数える） ---")
    windows = state_history()
    over = []
    for prev, cur in zip(windows, windows[1:]):
        if cur <= oldest:
            continue
        n = bisect.bisect_right(job_ts, cur) - bisect.bisect_left(job_ts, prev)
        mark = ""
        if n > 50:
            mark = f"  ← ★50件超。古い {n - 50}件は読まれていない"
            over.append((prev, cur, n))
        if n >= 30:
            print(f"   {jst(prev)} 〜 {jst(cur)}  {n:>3}件{mark}")
    print(f"   → 50件を超えた窓: {len(over)}個")
    print()

    # ② 契約報告そのものが落ちていないか。ここが業務影響の有無を決める。
    print("--- ② 契約報告が #repitte-hotel に転記されているか ---")
    reports = [m for m in job
               if "【契約獲得】" in m.get("text", "") and "リピッテホテル" in m.get("text", "")]
    print(f"   #job_sales の契約報告（リピッテホテル）: {len(reports)}件")
    hotel_ts = sorted(float(m["ts"]) for m in hotel)
    missed = []
    for m in sorted(reports, key=lambda m: float(m["ts"])):
        t = float(m["ts"])
        # 転記は元投稿より後に出る。10分以内に #repitte-hotel の投稿があるかで見る。
        i = bisect.bisect_left(hotel_ts, t)
        relayed = i < len(hotel_ts) and hotel_ts[i] - t < 600
        first = (m.get("text", "").splitlines() or [""])[0][:44]
        print(f"   {'✅' if relayed else '🔴'} {jst(t)}  {first}")
        if not relayed:
            missed.append(t)
    print(f"   → 転記が見当たらないもの: {len(missed)}件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
