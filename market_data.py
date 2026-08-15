"""
market_data.py
台股大盤資料擷取模組 — 串接 TWSE OpenAPI + FinMind

⚠️ 重要提醒：目前這段對話是在沒有對外網路連線的沙盒環境執行，
   我沒辦法在這裡實際呼叫這些 API 驗證回傳結果。程式邏輯與欄位名稱
   是依官方文件與現有開源專案整理，但實際欄位命名有時會微調，
   請在你自己有網路的環境跑一次 `python market_data.py`，
   如果有欄位對不上（KeyError），把錯誤訊息貼給我，我馬上幫你修。

安裝需求:
    pip install requests fastapi uvicorn

本機測試資料擷取:
    python market_data.py

啟動 API 伺服器（給前端 dashboard 呼叫）:
    uvicorn market_data:app --reload --port 8000
"""

import json
from datetime import datetime
from typing import Optional

import requests
import pandas as pd

TWSE_BASE = "https://openapi.twse.com.tw/v1"
FINMIND_BASE = "https://api.finmindtrade.com/api/v4/data"

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; tw-stock-dashboard/0.1)"}


def _get(url: str, params: Optional[dict] = None, timeout: int = 10):
    resp = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
    resp.raise_for_status()
    # 明確指定UTF-8解碼：不同作業系統環境（本機Windows vs Render的Linux）
    # 對回應編碼的自動猜測結果可能不一致，導致中文欄位（例如指數名稱、股票名稱）亂碼，
    # 數字欄位不受影響。TWSE的回應本身就是UTF-8，直接指定最保險。
    resp.encoding = "utf-8"
    return resp.json()


# ---------------------------------------------------------------------------
# 1. 大盤指數
#    來源: TWSE OpenAPI /exchangeReport/MI_INDEX（每日收盤行情-大盤統計資訊）
# ---------------------------------------------------------------------------
def get_taiex_snapshot() -> dict:
    data = _get(f"{TWSE_BASE}/exchangeReport/MI_INDEX")
    taiex = next((row for row in data if row.get("指數") == "發行量加權股價指數"), None)
    if not taiex:
        raise ValueError(
            "MI_INDEX 回傳資料中找不到「發行量加權股價指數」，"
            "請印出 data[0] 檢查目前實際欄位名稱"
        )
    sign = -1 if taiex.get("漲跌") == "-" else 1
    change_pts = abs(float(taiex["漲跌點數"])) * sign
    change_pct = abs(float(taiex["漲跌百分比"])) * sign
    return {
        "index_name": taiex["指數"],
        "close": float(taiex["收盤指數"]),
        "change_points": round(change_pts, 2),
        "change_percent": round(change_pct, 2),
        "is_up": sign > 0,
    }


# ---------------------------------------------------------------------------
# 2. 三大法人買賣超
#    來源: 證交所「傳統報表」API（注意：不是 openapi.twse.com.tw！）
#    網址格式跟 STOCK_DAY 那種舊版報表一樣，回傳 {fields:[...], data:[[...]]}
#    而不是新版 openapi 那種「中文欄位物件陣列」，兩套系統欄位格式不同。
#    金額單位為「元」
# ---------------------------------------------------------------------------
_INSTITUTION_KEY_MAP = {
    "外資及陸資(不含外資自營商)": "foreign",
    "外資及陸資": "foreign",
    "外資自營商": "foreign_dealer",
    "投信": "trust",
    "自營商(自行買賣)": "dealer_self",
    "自營商(避險)": "dealer_hedge",
    "自營商": "dealer_self",
}


def get_institutional_flows(date: Optional[str] = None) -> dict:
    """date 格式 YYYYMMDD，預設抓今天。遇假日/尚未收盤可能查無資料。"""
    query_date = date or datetime.today().strftime("%Y%m%d")
    raw = _get(
        "https://www.twse.com.tw/rwd/zh/fund/BFI82U",
        params={"response": "json", "date": query_date},
    )
    if raw.get("stat") != "OK":
        raise ValueError(
            f"BFI82U 查無資料（date={query_date}），可能是非交易日或當日尚未收盤。"
            f"原始回應: {raw.get('stat')}"
        )
    fields = raw.get("fields", [])
    result = {}
    for row in raw.get("data", []):
        record = dict(zip(fields, row))
        name = record.get("單位名稱", "").strip()
        key = _INSTITUTION_KEY_MAP.get(name)
        if not key:
            continue  # 未對應到的分類先略過（例如合計列）
        try:
            result[key] = {
                "buy": int(str(record["買進金額"]).replace(",", "")),
                "sell": int(str(record["賣出金額"]).replace(",", "")),
                "net": int(str(record["買賣差額"]).replace(",", "")),
            }
        except (KeyError, ValueError):
            continue
    return result


# ---------------------------------------------------------------------------
# 3. 漲跌家數 + 全市場成交值 + 個股報價（同一支API，兩個功能共用，加簡易快取避免重複請求）
#    來源: TWSE OpenAPI /exchangeReport/STOCK_DAY_ALL（全部上市股票當日成交資訊）
# ---------------------------------------------------------------------------
_stock_day_all_cache = {"data": None, "fetched_at": None}
_STOCK_DAY_ALL_TTL_SECONDS = 5 * 60


def _get_stock_day_all() -> list:
    now = datetime.now()
    cache = _stock_day_all_cache
    if cache["data"] is not None and (now - cache["fetched_at"]).total_seconds() < _STOCK_DAY_ALL_TTL_SECONDS:
        return cache["data"]
    data = _get(f"{TWSE_BASE}/exchangeReport/STOCK_DAY_ALL")
    cache["data"] = data
    cache["fetched_at"] = now
    return data


def get_advance_decline_and_turnover() -> dict:
    data = _get_stock_day_all()
    up = down = flat = 0
    total_value = 0
    for row in data:
        try:
            total_value += int(float(row.get("TradeValue", 0) or 0))
        except (ValueError, TypeError):
            pass
        change_raw = str(row.get("Change", "")).strip().replace(",", "")
        if not change_raw:
            continue
        try:
            change_val = float(change_raw)
        except ValueError:
            continue  # 欄位可能是空字串或特殊註記，跳過不計入漲跌家數
        if change_val > 0:
            up += 1
        elif change_val < 0:
            down += 1
        else:
            flat += 1
    return {
        "advance": up,
        "decline": down,
        "unchanged": flat,
        "total_turnover": total_value,  # 單位：元
    }


# 跑馬燈預設顯示的個股（跟前端 STOCK_NAMES 對照表保持一致比較好維護）
TICKER_CODES = ["2330", "2317", "2454", "2308", "2382", "2603", "5871", "3661", "2412", "2882"]


# ---------------------------------------------------------------------------
# 大盤歷史走勢
#    來源: 證交所傳統報表 /rwd/zh/TAIEX/MI_5MINS_HIST（注意也不是openapi.twse.com.tw）
#    這支API是「以月為單位」：帶入某個月份中任一天的日期，會回傳當月整月的每日指數。
#    做法：分別查最近 N 個月各一次（每個月傳該月1號當代表日期），合併去重、依日期排序。
# ---------------------------------------------------------------------------
def _shift_month(dt: datetime, months_back: int) -> datetime:
    total_month_index = dt.month - 1 - months_back
    year = dt.year + total_month_index // 12
    month = total_month_index % 12 + 1
    return dt.replace(year=year, month=month, day=1)


def _roc_date_to_iso(roc_date: str) -> Optional[str]:
    """民國年日期字串（例如 115/07/01）轉成西元 ISO 格式（2026-07-01）"""
    try:
        y, m, d = roc_date.strip().split("/")
        return f"{int(y) + 1911:04d}-{int(m):02d}-{int(d):02d}"
    except (ValueError, AttributeError):
        return None


def get_taiex_history(months_back: int = 3) -> list:
    today = datetime.today()
    merged = {}
    for i in range(months_back):
        target = _shift_month(today, i)
        query_date = target.strftime("%Y%m%d")
        try:
            raw = _get(
                "https://www.twse.com.tw/rwd/zh/TAIEX/MI_5MINS_HIST",
                params={"date": query_date, "response": "json"},
            )
        except Exception:
            continue  # 單月查詢失敗就跳過，不影響其他月份
        if raw.get("stat") != "OK":
            continue

        fields = raw.get("fields", [])
        date_idx = next((idx for idx, f in enumerate(fields) if "日期" in f), None)
        close_idx = next((idx for idx, f in enumerate(fields) if "收盤" in f), None)
        if date_idx is None or close_idx is None:
            continue  # 欄位對不上，可能格式又變了，需要人工檢查

        for row in raw.get("data", []):
            iso_date = _roc_date_to_iso(str(row[date_idx]))
            if not iso_date:
                continue
            try:
                close_val = float(str(row[close_idx]).replace(",", ""))
            except (ValueError, IndexError):
                continue
            if close_val > 0:
                merged[iso_date] = close_val

    return [{"date": d, "close": c} for d, c in sorted(merged.items())]


def get_ticker_quotes(codes=None) -> list:
    """
    個股跑馬燈報價。注意：STOCK_DAY_ALL 是「當日累計成交資訊」，收盤前查詢
    會是當下最新一筆累計資料，不是逐筆跳動的即時報價，但已收盤後就是正式收盤價。
    """
    codes = codes or TICKER_CODES
    data = _get_stock_day_all()
    by_code = {row.get("Code"): row for row in data}
    results = []
    for code in codes:
        row = by_code.get(code)
        if not row:
            continue  # 找不到這檔代號，可能當日停牌或代號打錯
        try:
            close = float(row["ClosingPrice"])
            change_val = float(str(row.get("Change", "0")).replace(",", "") or 0)
        except (KeyError, ValueError):
            continue
        prev_close = close - change_val
        change_pct = (change_val / prev_close * 100) if prev_close else 0.0
        results.append({
            "code": code,
            "name": row.get("Name", "").strip(),
            "close": round(close, 2),
            "change_percent": round(change_pct, 2),
            "is_up": change_val >= 0,
        })
    return results


# ---------------------------------------------------------------------------
# 4. FinMind 補充資料 — 個股歷史K線 / 個股法人買賣明細
#    選股邏輯（技術指標規則引擎）之後會用到這兩支
#    公開額度免 token 可用但有速率限制，長期使用建議申請免費 token：
#    https://finmindtrade.com/analysis/#/data/api
# ---------------------------------------------------------------------------
def get_stock_price_history(stock_id: str, start_date: str, end_date: Optional[str] = None) -> list:
    params = {
        "dataset": "TaiwanStockPrice",
        "data_id": stock_id,
        "start_date": start_date,
        "end_date": end_date or datetime.today().strftime("%Y-%m-%d"),
    }
    data = _get(FINMIND_BASE, params=params)
    return data.get("data", [])


def get_institutional_by_stock(stock_id: str, start_date: str, end_date: Optional[str] = None) -> list:
    params = {
        "dataset": "TaiwanStockInstitutionalInvestorsBuySell",
        "data_id": stock_id,
        "start_date": start_date,
        "end_date": end_date or datetime.today().strftime("%Y-%m-%d"),
    }
    data = _get(FINMIND_BASE, params=params)
    return data.get("data", [])


# ---------------------------------------------------------------------------
# 5. 彙整成前端 dashboard 需要的格式
# ---------------------------------------------------------------------------
def _to_yi(amount_ntd: int) -> float:
    """元 -> 億元，取到小數1位"""
    return round(amount_ntd / 1e8, 1)


def build_market_overview() -> dict:
    taiex = get_taiex_snapshot()
    flows = get_institutional_flows()
    breadth = get_advance_decline_and_turnover()

    foreign_net = flows.get("foreign", {}).get("net", 0)
    trust_net = flows.get("trust", {}).get("net", 0)
    dealer_net = flows.get("dealer_self", {}).get("net", 0) + flows.get("dealer_hedge", {}).get("net", 0)

    return {
        "updated_at": datetime.now().isoformat(),
        "taiex": taiex,
        "turnover_yi": _to_yi(breadth["total_turnover"]),
        "breadth": {
            "advance": breadth["advance"],
            "decline": breadth["decline"],
            "unchanged": breadth["unchanged"],
        },
        "institutional_yi": {
            "foreign": foreign_net and _to_yi(foreign_net),
            "trust": trust_net and _to_yi(trust_net),
            "dealer": dealer_net and _to_yi(dealer_net),
        },
    }


# ---------------------------------------------------------------------------
# FastAPI 端點 — 給前端 dashboard fetch() 呼叫
# ---------------------------------------------------------------------------
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
import os

import stock_screener  # 需與 market_data.py 放在同一個資料夾
import ai_insight as ai_insight_module  # 同上，需放在同一個資料夾
import backtest as backtest_module  # 同上，需放在同一個資料夾

app = FastAPI(title="台股盤勢 API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 開發階段先開放，正式上線請改成前端實際網域
    allow_methods=["GET"],
)

# 這個路由原本是給「區網內用手機連PC」的方案用的（把前端跟API掛在同一個伺服器）。
# 現在改用 GitHub Pages 放前端、Render 純粹當API，所以這裡不是必要路徑了，
# 但保留一個安全版本：本機還是可以用，雲端環境（檔案不存在）就回傳提示訊息，
# 不要讓整個服務因為單一路由的檔案讀取錯誤而500當機。
_FRONTEND_HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tw-stock-dashboard-prototype.html")


@app.get("/")
def serve_frontend():
    if os.path.exists(_FRONTEND_HTML_PATH):
        return FileResponse(_FRONTEND_HTML_PATH)
    return {"message": "台股盤勢 API 運作中。前端頁面請至 GitHub Pages 開啟，或參考 /docs 查看可用端點。"}


@app.get("/api/health")
def health():
    """給UptimeRobot之類的服務定時ping用，不做任何資料抓取，純粹確認伺服器還活著。"""
    return {"status": "ok", "time": datetime.now().isoformat()}


@app.get("/api/market-overview")
def market_overview():
    return build_market_overview()


@app.get("/api/ticker")
def ticker():
    return {"updated_at": datetime.now().isoformat(), "quotes": get_ticker_quotes()}


_taiex_history_cache = {"data": None, "fetched_at": None}
_TAIEX_HISTORY_TTL_SECONDS = 6 * 60 * 60


@app.get("/api/taiex-history")
def taiex_history(months: int = 3):
    now = datetime.now()
    cache = _taiex_history_cache
    if cache["data"] is not None and (now - cache["fetched_at"]).total_seconds() < _TAIEX_HISTORY_TTL_SECONDS:
        return {"updated_at": cache["fetched_at"].isoformat(), "series": cache["data"], "cached": True}
    series = get_taiex_history(months_back=months)
    cache["data"] = series
    cache["fetched_at"] = now
    return {"updated_at": now.isoformat(), "series": series, "cached": False}


# 選股掃描一次要跑 WATCHLIST 每一檔的 FinMind 請求，耗時較久（約數十秒），
# 用簡易記憶體快取避免每次打開頁面都重新掃一次；快取存活30分鐘。
_stock_picks_cache = {"data": None, "fetched_at": None}
_CACHE_TTL_SECONDS = 30 * 60


@app.get("/api/stock-picks")
def stock_picks():
    now = datetime.now()
    cache = _stock_picks_cache
    if cache["data"] is not None and (now - cache["fetched_at"]).total_seconds() < _CACHE_TTL_SECONDS:
        return {"updated_at": cache["fetched_at"].isoformat(), "picks": cache["data"], "cached": True}

    picks = stock_screener.run_screener()
    cache["data"] = picks
    cache["fetched_at"] = now
    return {"updated_at": now.isoformat(), "picks": picks, "cached": False}


# 個股詳情：依股票代號分開快取，30分鐘內重複點同一檔不用重打FinMind
_stock_detail_cache = {}
_STOCK_DETAIL_TTL_SECONDS = 30 * 60


@app.get("/api/stock-detail")
def stock_detail(stock_id: str):
    now = datetime.now()
    cache = _stock_detail_cache.setdefault(stock_id, {"data": None, "fetched_at": None})
    if cache["data"] is not None and (now - cache["fetched_at"]).total_seconds() < _STOCK_DETAIL_TTL_SECONDS:
        result = dict(cache["data"])
        result["cached"] = True
        return result

    try:
        df = stock_screener.compute_indicators(stock_screener.fetch_price_history(stock_id, lookback_days=150))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"抓取 {stock_id} 歷史資料失敗：{e}")

    # 只取最近90筆，避免資料量太大；轉成前端好用的格式，NaN（前面均線還沒算出來的部分）轉成null
    recent = df.tail(90)
    series = []
    for _, row in recent.iterrows():
        series.append({
            "date": str(row["date"]),
            "close": round(float(row["close"]), 2),
            "ma5": round(float(row["ma5"]), 2) if pd.notna(row["ma5"]) else None,
            "ma20": round(float(row["ma20"]), 2) if pd.notna(row["ma20"]) else None,
            "ma60": round(float(row["ma60"]), 2) if pd.notna(row["ma60"]) else None,
            "k": round(float(row["k"]), 1) if pd.notna(row["k"]) else None,
            "d": round(float(row["d"]), 1) if pd.notna(row["d"]) else None,
        })

    result = {"stock_id": stock_id, "series": series}
    cache["data"] = result
    cache["fetched_at"] = now
    result = dict(result)
    result["cached"] = False
    return result


# AI視角快取；用字典依 engine（rules/llm）分開存，llm模式會產生API費用所以更需要快取。
_ai_insight_cache = {}
_AI_CACHE_TTL_SECONDS = 60 * 60


@app.get("/api/ai-insight")
def ai_insight(engine: str = "rules"):
    """
    engine="rules"（預設）：免費，用if/else規則把數字轉成文字，不呼叫任何外部AI服務。
    engine="llm"：呼叫Claude API，需要先設定 ANTHROPIC_API_KEY，會產生費用。
    """
    now = datetime.now()
    cache_key = engine
    cache = _ai_insight_cache.setdefault(cache_key, {"data": None, "fetched_at": None})
    if cache["data"] is not None and (now - cache["fetched_at"]).total_seconds() < _AI_CACHE_TTL_SECONDS:
        result = dict(cache["data"])
        result["cached"] = True
        return result

    market = build_market_overview()
    picks_result = stock_picks()  # 直接重用上面選股端點的邏輯（也吃它自己的快取）

    if engine == "llm":
        try:
            insight = ai_insight_module.generate_ai_insight(market, picks_result["picks"])
        except RuntimeError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"呼叫Claude API失敗：{e}")
    else:
        insight = ai_insight_module.generate_rule_based_insight(market, picks_result["picks"])

    cache["data"] = insight
    cache["fetched_at"] = now
    result = dict(insight)
    result["cached"] = False
    return result


# 回測跑一次要掃過觀察名單全部股票的歷史資料，耗時較久（約1-2分鐘），
# 而且規則跟資料不會每分鐘變，所以快取存活拉長到24小時，避免每次開頁面都重跑。
_backtest_cache = {"data": None, "fetched_at": None}
_BACKTEST_CACHE_TTL_SECONDS = 24 * 60 * 60


@app.get("/api/backtest")
def backtest_report(refresh: bool = False):
    """refresh=true 可以強制重新跑一次，忽略快取（一樣要等1-2分鐘）"""
    now = datetime.now()
    cache = _backtest_cache
    if not refresh and cache["data"] is not None and (now - cache["fetched_at"]).total_seconds() < _BACKTEST_CACHE_TTL_SECONDS:
        result = dict(cache["data"])
        result["cached"] = True
        return result

    try:
        result = backtest_module.run_backtest()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"回測執行失敗：{e}")

    cache["data"] = result
    cache["fetched_at"] = now
    result = dict(result)
    result["cached"] = False
    return result


if __name__ == "__main__":
    # 直接執行本檔可在終端機測試資料擷取是否正常運作
    print(json.dumps(build_market_overview(), ensure_ascii=False, indent=2))