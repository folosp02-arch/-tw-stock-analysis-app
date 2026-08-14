"""
stock_screener.py
選股標的邏輯 — 用 FinMind 歷史資料計算技術指標 + 法人籌碼規則，篩出候選股清單

⚠️ 一樣的提醒：這支程式我沒辦法在沙盒裡實際連線測試，FinMind的欄位名稱是照
   官方文件整理的，但如果實際跑起來欄位對不上，把錯誤訊息貼給我。

⚠️ 重要設計取捨：FinMind免費額度是「每小時300次請求（有註冊token是600次）」，
   台股上市加上櫃合計快2000檔，如果每天全市場掃一輪會直接爆額度。
   所以這版先鎖定一份「觀察名單」(WATCHLIST，預設放權值股/高流動性股)，
   之後你可以自己擴充這份清單，或分批、跨小時掃描更大範圍。

安裝需求:
    pip install pandas requests

本機測試:
    python stock_screener.py
"""

import time
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd
import requests

FINMIND_BASE = "https://api.finmindtrade.com/api/v4/data"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; tw-stock-dashboard/0.1)"}

# FinMind 免費額度沒有 token 是 300次/小時，註冊後帶 token 可以到 600次/小時
# 之後有 token 的話填在這裡即可，程式會自動帶上
FINMIND_TOKEN = ""

# ---------------------------------------------------------------------------
# 觀察名單（先從高流動性權值股開始，之後可自行擴充）
# ---------------------------------------------------------------------------
WATCHLIST = [
    "2330", "2317", "2454", "2308", "2382", "2603", "2609", "2891",
    "3661", "6669", "2412", "3231", "2303", "2379", "3034", "2357",
    "5871", "2881", "2882", "1301", "1303", "2002", "2207", "2201",
    "3008", "2377", "2409", "2395", "2886",
    # 6505（台塑化）先移除：其歷史資料出現多筆異常大幅波動（單筆漲跌超過20%），
    # 懷疑是FinMind資料品質問題，會扭曲回測統計，待查清楚原因再考慮加回來
]

# 篩選規則需要達成的訊號數（分數）門檻，可自行調整
MIN_SIGNALS = 2

# 每次請求間隔秒數，避免短時間內打爆FinMind速率限制
REQUEST_DELAY = 0.35


def _finmind_get(dataset: str, stock_id: str, start_date: str, end_date: Optional[str] = None) -> pd.DataFrame:
    params = {
        "dataset": dataset,
        "data_id": stock_id,
        "start_date": start_date,
        "end_date": end_date or datetime.today().strftime("%Y-%m-%d"),
    }
    headers = dict(HEADERS)
    if FINMIND_TOKEN:
        headers["Authorization"] = f"Bearer {FINMIND_TOKEN}"
    resp = requests.get(FINMIND_BASE, params=params, headers=headers, timeout=15)
    resp.raise_for_status()
    resp.encoding = "utf-8"  # 避免不同作業系統環境猜測編碼不一致導致中文欄位亂碼
    payload = resp.json()
    if payload.get("status") != 200 and "data" not in payload:
        raise ValueError(f"FinMind回應異常（{dataset} / {stock_id}）: {payload.get('msg')}")
    return pd.DataFrame(payload.get("data", []))


# ---------------------------------------------------------------------------
# 1. 個股歷史K線 + 技術指標
# ---------------------------------------------------------------------------
def fetch_price_history(stock_id: str, lookback_days: int = 120) -> pd.DataFrame:
    start = (datetime.today() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    df = _finmind_get("TaiwanStockPrice", stock_id, start)
    if df.empty:
        raise ValueError(f"{stock_id} 沒有股價資料，可能代號錯誤或已下市")
    df = df.sort_values("date").reset_index(drop=True)
    # FinMind TaiwanStockPrice 欄位：date, stock_id, Trading_Volume, Trading_money,
    # open, max, min, close, spread, Trading_turnover
    df = df.rename(columns={"max": "high", "min": "low", "Trading_Volume": "volume"})
    # 資料清理：FinMind偶爾會出現收盤價為0或缺值的異常列（停牌/資料缺漏），
    # 若不濾掉，後面算報酬率時「除以0」會炸出 Infinity，污染整體統計。
    before = len(df)
    df = df[pd.to_numeric(df["close"], errors="coerce") > 0].reset_index(drop=True)
    dropped = before - len(df)
    if dropped:
        print(f"[資料清理] {stock_id} 濾掉 {dropped} 筆收盤價異常（<=0）的資料列")
    return df


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["ma5"] = df["close"].rolling(5).mean()
    df["ma20"] = df["close"].rolling(20).mean()
    df["ma60"] = df["close"].rolling(60).mean()
    df["vol_ma20"] = df["volume"].rolling(20).mean()

    # KD (9,3,3) 慢速隨機指標，K/D初始值設50
    n = 9
    low_n = df["low"].rolling(n).min()
    high_n = df["high"].rolling(n).max()
    rsv = (df["close"] - low_n) / (high_n - low_n).replace(0, pd.NA) * 100
    rsv = rsv.fillna(50)
    k_vals, d_vals = [], []
    k_prev, d_prev = 50.0, 50.0
    for r in rsv:
        k_cur = k_prev * 2 / 3 + r / 3
        d_cur = d_prev * 2 / 3 + k_cur / 3
        k_vals.append(k_cur)
        d_vals.append(d_cur)
        k_prev, d_prev = k_cur, d_cur
    df["k"] = k_vals
    df["d"] = d_vals

    # 簡化版 ATR（用高低價差14日均值近似，非嚴謹版真實區間）
    df["atr14"] = (df["high"] - df["low"]).rolling(14).mean()
    return df


# ---------------------------------------------------------------------------
# 2. 法人買賣超連買天數
# ---------------------------------------------------------------------------
def fetch_institutional_streak(stock_id: str, lookback_days: int = 15) -> int:
    start = (datetime.today() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    df = _finmind_get("TaiwanStockInstitutionalInvestorsBuySell", stock_id, start)
    if df.empty:
        return 0
    # name欄位常見類別包含 Foreign_Investor / Investment_Trust / Dealer_self / Dealer_Hedging等
    # 這裡把「外資」與「投信」相關類別加總為主要籌碼指標；欄位實際字串如與此不符，
    # 印出 df['name'].unique() 確認後調整下面的關鍵字比對
    df["net"] = df["buy"] - df["sell"]
    mask = df["name"].astype(str).str.contains("Foreign|Investment_Trust|外資|投信", case=False, na=False)
    daily_net = df[mask].groupby("date")["net"].sum().sort_index()
    if daily_net.empty:
        return 0
    streak = 0
    for net in daily_net.iloc[::-1]:  # 從最新一天往回數
        if net > 0:
            streak += 1
        else:
            break
    return streak


# ---------------------------------------------------------------------------
# 3. 單檔篩選邏輯
# ---------------------------------------------------------------------------
def evaluate_signal_flags(latest_row, prev_row, inst_streak: int) -> dict:
    """回傳每條規則各自是否成立的布林值，供逐條規則的回測分析使用"""
    vol_ratio = latest_row["volume"] / latest_row["vol_ma20"] if latest_row["vol_ma20"] else 0
    # KD黃金交叉：原本用「K<50」判斷相對低檔，回測顯示這條規則有負向edge。
    # K<50只是低於中性值，不算真正超賣，抓到太多雜訊。改成「昨日K<20」(真正深度超賣)
    # 後才算交叉，門檻更嚴格、出現次數會變少，需要重新回測驗證是否真的有改善。
    return {
        "均線多頭排列": bool(latest_row["close"] > latest_row["ma5"] > latest_row["ma20"] > latest_row["ma60"]),
        "量價齊揚": bool(vol_ratio >= 1.5 and latest_row["close"] > prev_row["close"]),
        "KD黃金交叉": bool(
            prev_row["k"] < prev_row["d"] and latest_row["k"] > latest_row["d"] and prev_row["k"] < 20
        ),
        "法人連買": bool(inst_streak >= 3),
    }


def evaluate_signal_tags(latest_row, prev_row, inst_streak: int) -> list:
    """
    核心規則判斷，回傳符合的訊號標籤列表。
    這個函式被 screen_stock()（即時選股）跟 backtest.py（回測）共用，
    確保回測驗證的「真的是同一套規則」，不是另外寫一份容易兜不起來的邏輯。
    """
    flags = evaluate_signal_flags(latest_row, prev_row, inst_streak)
    tags = []
    if flags["均線多頭排列"]:
        tags.append("均線多頭排列")
    if flags["量價齊揚"]:
        tags.append("量價齊揚")
    if flags["KD黃金交叉"]:
        tags.append("KD黃金交叉")
    if flags["法人連買"]:
        tags.append(f"法人連買 {inst_streak} 日")
    return tags


def screen_stock(stock_id: str) -> Optional[dict]:
    price_df = compute_indicators(fetch_price_history(stock_id))
    if len(price_df) < 60:
        return None  # 資料不足以算60日均線，跳過

    latest = price_df.iloc[-1]
    prev = price_df.iloc[-2]

    latest_streak = fetch_institutional_streak(stock_id)
    tags = evaluate_signal_tags(latest, prev, latest_streak)
    score = len(tags)
    if score < MIN_SIGNALS:
        return None

    atr = latest["atr14"] if pd.notna(latest["atr14"]) else latest["close"] * 0.02
    close = latest["close"]
    entry_low = round(close - 0.3 * atr, 2)
    entry_high = round(close + 0.2 * atr, 2)
    stop_loss = round(close - 2 * atr, 2)

    return {
        "stock_id": stock_id,
        "close": round(float(close), 2),
        "change_percent": round(float(latest["spread"] / prev["close"] * 100), 2) if prev["close"] else 0,
        "tags": tags,
        "score": score,
        "entry_range": [entry_low, entry_high],
        "stop_loss": stop_loss,
    }


# ---------------------------------------------------------------------------
# 4. 批次跑觀察名單
# ---------------------------------------------------------------------------
def run_screener(watchlist=None) -> list:
    watchlist = watchlist or WATCHLIST
    results = []
    for stock_id in watchlist:
        try:
            picked = screen_stock(stock_id)
            if picked:
                results.append(picked)
        except Exception as e:
            print(f"[跳過] {stock_id} 發生錯誤: {e}")
        time.sleep(REQUEST_DELAY)  # 保護 FinMind 速率限制
    results.sort(key=lambda r: r["score"], reverse=True)
    return results


if __name__ == "__main__":
    import json
    picks = run_screener()
    print(f"\n共掃描 {len(WATCHLIST)} 檔，符合條件（訊號數>={MIN_SIGNALS}）：{len(picks)} 檔\n")
    print(json.dumps(picks, ensure_ascii=False, indent=2))