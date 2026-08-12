"""
backtest.py
回測驗證 — 用歷史資料檢驗 stock_screener.py 的選股規則過去是否真的有效

邏輯：
1. 把選股規則套用在過去每一個交易日（不只是今天），找出「當時符合條件」的訊號出現日
2. 計算每個訊號出現之後 N 個交易日的報酬率
3. 拿「訊號日之後的報酬」跟「全部交易日之後的報酬（基準）」比較平均報酬與勝率
   如果訊號日的表現明顯優於基準，代表這套規則過去確實有篩選能力；
   如果差不多甚至更差，代表規則可能沒有實際效果，純屬巧合命中。

⚠️ 重要提醒：
- 這是歷史統計驗證，不是保證未來有效（過去有效不代表以後一定有效）
- 固定30檔觀察名單、抓約1年資料，樣本數本來就不大，結果的統計顯著性有限，
  尤其「法人連買」這種訊號本身出現頻率低，樣本可能只有個位數，數字僅供參考
- 我沒辦法在沙盒環境實際連線跑這支程式，邏輯若有欄位對不上的錯誤，把訊息貼給我

安裝需求（跟 stock_screener.py 共用）:
    pip install pandas requests

本機測試:
    python backtest.py
"""

import json
import time
from datetime import datetime, timedelta

import pandas as pd

import stock_screener as ss

LOOKBACK_DAYS = 400          # 抓約400個日曆天，約可覆蓋250個交易日（含算指標要用的緩衝）
HORIZONS = [5, 10, 20]       # 訊號出現後，往後看幾個交易日的報酬率


def _fetch_institutional_streak_series(stock_id: str, start_date: str) -> pd.Series:
    """回傳一個 Series：index是日期，value是「該日為止」的連續買超天數"""
    df = ss._finmind_get("TaiwanStockInstitutionalInvestorsBuySell", stock_id, start_date)
    if df.empty:
        return pd.Series(dtype=int)
    df["net"] = df["buy"] - df["sell"]
    mask = df["name"].astype(str).str.contains("Foreign|Investment_Trust|外資|投信", case=False, na=False)
    daily_net = df[mask].groupby("date")["net"].sum().sort_index()

    streak_vals = []
    current = 0
    for v in daily_net:
        current = current + 1 if v > 0 else 0
        streak_vals.append(current)
    return pd.Series(streak_vals, index=daily_net.index)


def backtest_stock(stock_id: str) -> list:
    start = (datetime.today() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    price_df = ss.compute_indicators(ss.fetch_price_history(stock_id, lookback_days=LOOKBACK_DAYS))
    streak_series = _fetch_institutional_streak_series(stock_id, start)

    price_df = price_df.set_index("date")
    price_df["inst_streak"] = streak_series.reindex(price_df.index).fillna(0).astype(int)
    price_df = price_df.reset_index()

    max_horizon = max(HORIZONS)
    events = []
    # 從第60筆開始（要等MA60算得出來），到「倒數第max_horizon筆」為止（要有未來資料算報酬）
    for i in range(60, len(price_df) - max_horizon):
        latest = price_df.iloc[i]
        prev = price_df.iloc[i - 1]
        if latest["close"] <= 0 or prev["close"] <= 0:
            continue  # 防呆：理論上fetch_price_history已濾過，這裡再擋一次避免除以零
        tags = ss.evaluate_signal_tags(latest, prev, int(latest["inst_streak"]))
        flags = ss.evaluate_signal_flags(latest, prev, int(latest["inst_streak"]))
        score = len(tags)

        record = {
            "date": str(latest["date"]),
            "stock_id": stock_id,
            "score": score,
            "tags": tags,
            "is_signal": score >= ss.MIN_SIGNALS,
            **flags,  # 展開四條規則各自的布林值，例如 record["均線多頭排列"] = True/False
        }
        skip_record = False
        for h in HORIZONS:
            future_close = price_df.iloc[i + h]["close"]
            if future_close <= 0:
                skip_record = True
                break
            record[f"return_{h}d"] = round((future_close / latest["close"] - 1) * 100, 2)
        if skip_record:
            continue
        events.append(record)
    return events


def run_backtest(watchlist=None) -> dict:
    watchlist = watchlist or ss.WATCHLIST
    all_events = []
    for stock_id in watchlist:
        try:
            all_events.extend(backtest_stock(stock_id))
        except Exception as e:
            print(f"[跳過] {stock_id} 回測發生錯誤: {e}")
        time.sleep(ss.REQUEST_DELAY)  # 保護 FinMind 速率限制

    df = pd.DataFrame(all_events)
    if df.empty:
        return {"error": "沒有足夠資料完成回測，請確認觀察名單與網路連線"}

    signal_df = df[df["is_signal"]]
    baseline_df = df  # 用「全部交易日」（含未觸發訊號的日子）當對照基準

    summary = {
        "total_days_scanned": int(len(df)),
        "total_signals": int(len(signal_df)),
        "signal_rate_percent": round(len(signal_df) / len(df) * 100, 2) if len(df) else 0,
    }
    baseline_avg = {}
    for h in HORIZONS:
        col = f"return_{h}d"
        baseline_avg[h] = round(baseline_df[col].mean(), 2)
        summary[f"signal_avg_return_{h}d"] = round(signal_df[col].mean(), 2) if len(signal_df) else None
        summary[f"signal_win_rate_{h}d"] = round((signal_df[col] > 0).mean() * 100, 1) if len(signal_df) else None
        summary[f"baseline_avg_return_{h}d"] = baseline_avg[h]
        summary[f"baseline_win_rate_{h}d"] = round((baseline_df[col] > 0).mean() * 100, 1)
        summary[f"edge_{h}d"] = (
            round(summary[f"signal_avg_return_{h}d"] - baseline_avg[h], 2)
            if summary[f"signal_avg_return_{h}d"] is not None else None
        )

    # 逐條規則單獨拆解：這條規則單狜成立時（不管其他規則），表現是否優於基準
    rule_names = ["均線多頭排列", "量價齊揚", "KD黃金交叉", "法人連買"]
    rule_breakdown = {}
    for rule in rule_names:
        sub = df[df[rule] == True]
        entry = {"count": int(len(sub)), "occurrence_rate_percent": round(len(sub) / len(df) * 100, 2)}
        for h in HORIZONS:
            col = f"return_{h}d"
            avg_ret = round(sub[col].mean(), 2) if len(sub) else None
            entry[f"avg_return_{h}d"] = avg_ret
            entry[f"win_rate_{h}d"] = round((sub[col] > 0).mean() * 100, 1) if len(sub) else None
            entry[f"edge_{h}d"] = round(avg_ret - baseline_avg[h], 2) if avg_ret is not None else None
        rule_breakdown[rule] = entry

    return {
        "summary": summary,
        "rule_breakdown": rule_breakdown,
        "recent_signals": signal_df.sort_values("date", ascending=False).head(15).to_dict("records"),
    }


if __name__ == "__main__":
    result = run_backtest()
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))