import os
import pandas as pd
import requests
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from datetime import datetime, timedelta

# ------------------------------------------------------------------
# 1. Slackから「名簿」を作る関数 (属性情報のマスター)
# ------------------------------------------------------------------
def fetch_slack_user_directory():
    """
    Slackから全メンバーの属性情報を取得し、Emailをキーにした辞書を作る
    """
    token = os.environ.get("SLACK_TOKEN")
    if not token:
        raise ValueError("環境変数 'SLACK_TOKEN' が設定されていません。")

    client = WebClient(token=token)
    
    try:
        users_resp = client.users_list()
    except SlackApiError as e:
        print(f"Error fetching users: {e}")
        return {}

    directory = {}
    
    for u in users_resp["members"]:
        # Bot、削除済み、プロフィール取得不可のユーザーは除外
        if u["is_bot"] or u["deleted"] or "profile" not in u:
            continue
            
        email = u["profile"].get("email")
        if not email:
            continue
            
        # Slackのゲストアカウント判定 (シングル/マルチチャンネルゲスト)
        is_guest = u.get("is_restricted", False) or u.get("is_ultra_restricted", False)
        
        directory[email] = {
            "User Name": u.get("real_name") or u["name"], # Slackの表示名を採用
            "Role": "Contractor" if is_guest else "Employee", # ゲストなら委託、それ以外は正社員
            "Avatar": u["profile"].get("image_48", "") # アイコン画像
        }
    
    return directory

# ------------------------------------------------------------------
# 2. Slackのメッセージ数を集計する関数
# ------------------------------------------------------------------
def fetch_slack_data(start_date, end_date):
    token = os.environ.get("SLACK_TOKEN")
    channel_id = os.environ.get("SLACK_CHANNEL_ID")
    
    if not token or not channel_id:
        print("Skipping Slack data fetch: Token or Channel ID missing.")
        return pd.DataFrame(columns=["Email", "Slack Count"])

    client = WebClient(token=token)
    
    # UNIXタイムスタンプに変換
    oldest = start_date.timestamp()
    latest = end_date.timestamp()
    
    try:
        # ユーザーIDとEmailの対応表を作成
        users_resp = client.users_list()
        uid_to_email = {}
        for u in users_resp["members"]:
            if "profile" in u and "email" in u["profile"]:
                uid_to_email[u["id"]] = u["profile"]["email"]

        # 履歴取得 (limit=1000: 必要に応じてページネーション実装)
        history = client.conversations_history(
            channel=channel_id, 
            oldest=oldest, 
            latest=latest,
            limit=1000
        )
        
        counts = {} # {Email: Count}
        for msg in history["messages"]:
            uid = msg.get("user")
            if uid in uid_to_email:
                email = uid_to_email[uid]
                counts[email] = counts.get(email, 0) + 1
                
        return pd.DataFrame(list(counts.items()), columns=["Email", "Slack Count"])

    except SlackApiError as e:
        print(f"Slack API Error: {e.response['error']}")
        return pd.DataFrame(columns=["Email", "Slack Count"])

# ------------------------------------------------------------------
# 3. LinearのIssue完了数を集計する関数
# ------------------------------------------------------------------
def fetch_linear_data(start_date):
    api_key = os.environ.get("LINEAR_KEY")
    if not api_key:
        print("Skipping Linear data fetch: API Key missing.")
        return pd.DataFrame(columns=["Email", "Linear Count"])

    url = "https://api.linear.app/graphql"
    date_str = start_date.strftime("%Y-%m-%d")
    
    query = f"""
    query {{
      issues(filter: {{ completedAt: {{ gte: "{date_str}" }} }}) {{
        nodes {{
          title
          assignee {{
            email
          }}
          completedAt
        }}
      }}
    }}
    """
    
    headers = {"Authorization": api_key, "Content-Type": "application/json"}
    
    try:
        response = requests.post(url, json={"query": query}, headers=headers)
        if response.status_code != 200:
            print(f"Linear API Error: {response.text}")
            return pd.DataFrame(columns=["Email", "Linear Count"])
            
        data = response.json()
        issues = data.get("data", {}).get("issues", {}).get("nodes", [])
        
        counts = {}
        for issue in issues:
            assignee = issue.get("assignee")
            if assignee and assignee.get("email"):
                email = assignee["email"]
                counts[email] = counts.get(email, 0) + 1
                
        return pd.DataFrame(list(counts.items()), columns=["Email", "Linear Count"])

    except Exception as e:
        print(f"Linear Connection Error: {e}")
        return pd.DataFrame(columns=["Email", "Linear Count"])

# ------------------------------------------------------------------
# 4. メイン実行処理 (結合とCSV保存)
# ------------------------------------------------------------------
def main():
    print("🚀 Starting data update...")
    
    # 集計期間の設定 (例: 過去30日間)
    # 定期実行でデータを上書き更新していくスタイル
    end_date = datetime.now()
    start_date = end_date - timedelta(days=30)
    
    print(f"📅 Range: {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}")

    # 1. 名簿の取得 (Slack)
    print("running: fetch_slack_user_directory...")
    user_directory = fetch_slack_user_directory()
    
    # 2. データの取得
    print("running: fetch_slack_data...")
    df_slack = fetch_slack_data(start_date, end_date)
    
    print("running: fetch_linear_data...")
    df_linear = fetch_linear_data(start_date)
    
    # 3. メールアドレスのユニオンを作成
    emails_slack = set(df_slack["Email"]) if not df_slack.empty else set()
    emails_linear = set(df_linear["Email"]) if not df_linear.empty else set()
    all_emails = set(user_directory.keys()) | emails_slack | emails_linear
    
    # 4. データ結合
    rows = []
    for email in all_emails:
        # プロフィール取得 (名簿になければUnknown)
        profile = user_directory.get(email, {
            "User Name": email, 
            "Role": "Unknown", 
            "Avatar": ""
        })
        
        # Slackカウント取得
        slack_count = 0
        if not df_slack.empty:
            s_row = df_slack[df_slack["Email"] == email]
            if not s_row.empty:
                slack_count = s_row["Slack Count"].sum()
        
        # Linearカウント取得
        linear_count = 0
        if not df_linear.empty:
            l_row = df_linear[df_linear["Email"] == email]
            if not l_row.empty:
                linear_count = l_row["Linear Count"].sum()
        
        # 行データの追加
        rows.append({
            "Email": email,
            "User": profile["User Name"],
            "Role": profile["Role"],
            "Avatar": profile["Avatar"],
            "Slack Count": int(slack_count),
            "Linear Count": int(linear_count),
            # 稼働時間の仮定 (正社員:40h, 委託:20h)
            "Working Hours": 40 if profile["Role"] == "Employee" else 20
        })
    
    # 5. CSV保存
    if not rows:
        print("⚠️ No data found.")
        return

    df_merged = pd.DataFrame(rows)
    
    # 保存先ディレクトリの作成
    os.makedirs("data", exist_ok=True)
    
    # CSV出力
    output_path = "data/engagement.csv"
    df_merged.to_csv(output_path, index=False)
    print(f"✅ Saved to {output_path}")
    print(df_merged.head())

if __name__ == "__main__":
    main()