"""
ai_insight.py
AI視角 — 把當日大盤盤勢 + 選股標的的真實結構化資料餵給 Claude API，
產生籌碼面解讀摘要。

⚠️ 誠實聲明：這裡沒有新聞資料源，所以只能讓AI讀「數字」（指數、漲跌家數、
   三大法人、選股訊號），不是真的讀新聞來解讀消息面。如果之後想做新聞解讀，
   需要另外接新聞API或RSS，把新聞內容一起放進 prompt。

安裝需求:
    pip install anthropic

環境變數（必須先設定好才能用）:
    Windows PowerShell（當次視窗有效）:
        $env:ANTHROPIC_API_KEY = "你的key"
    Windows（永久生效，設定後要開新視窗）:
        setx ANTHROPIC_API_KEY "你的key"
"""

import json
import os
from datetime import datetime

MODEL = "claude-sonnet-5"


def _build_prompt(market: dict, picks: list) -> str:
    taiex = market["taiex"]
    breadth = market["breadth"]
    inst = market["institutional_yi"]

    picks_summary = [
        {
            "股票": p["stock_id"],
            "訊號": p["tags"],
            "收盤": p["close"],
            "漲跌%": p["change_percent"],
        }
        for p in picks
    ]

    data_block = {
        "加權指數": taiex["close"],
        "漲跌點數": taiex["change_points"],
        "漲跌百分比": taiex["change_percent"],
        "成交值_億": market["turnover_yi"],
        "上漲家數": breadth["advance"],
        "下跌家數": breadth["decline"],
        "外資買賣超_億": inst["foreign"],
        "投信買賣超_億": inst["trust"],
        "自營商買賣超_億": inst["dealer"],
        "今日符合規則的選股標的": picks_summary,
    }

    return f"""你是台股籌碼面分析助手。以下是今天收盤後的真實市場數據（JSON），
請純粹根據這些數字做解讀，不要編造你不知道的新聞或消息面資訊。

資料：
{json.dumps(data_block, ensure_ascii=False, indent=2)}

請用繁體中文，只回傳以下格式的JSON，不要有其他文字、不要加markdown code fence：
{{
  "market_view": "一到兩句話描述今天大盤整體氣氛（根據指數漲跌、家數、成交值）",
  "chip_view": "一到兩句話描述三大法人籌碼動向的意義（根據買賣超數字）",
  "picks_view": "一到兩句話描述今天選股標的的共同特徵或值得注意的地方，若清單為空就說明目前無標的符合條件",
  "risk_note": "一句話風險提示，語氣中性，不要誇大恐慌也不要過度樂觀"
}}"""


def _market_view(market: dict) -> str:
    t = market["taiex"]
    b = market["breadth"]
    chg_pct = t["change_percent"]
    advance, decline = b["advance"], b["decline"]

    if chg_pct >= 1.0:
        tone = "大盤強勢上漲"
    elif chg_pct > 0:
        tone = "大盤小幅收紅"
    elif chg_pct > -1.0:
        tone = "大盤小幅收黑"
    else:
        tone = "大盤明顯重挫"

    if advance > decline * 1.5:
        breadth_desc = "個股普遍走揚，上漲家數明顯多於下跌"
    elif decline > advance * 1.5:
        breadth_desc = "跌多漲少，市場氣氛偏弱"
    else:
        breadth_desc = "漲跌家數相當，個股表現分歧"

    return (
        f"{tone}，收在 {t['close']:.2f} 點，{'上漲' if chg_pct >= 0 else '下跌'}"
        f"{abs(t['change_points']):.2f} 點（{abs(chg_pct):.2f}%）；"
        f"{breadth_desc}（上漲 {advance} 檔、下跌 {decline} 檔）。"
    )


def _chip_view(market: dict) -> str:
    inst = market["institutional_yi"]
    foreign, trust, dealer = inst.get("foreign") or 0, inst.get("trust") or 0, inst.get("dealer") or 0

    if foreign > 0 and trust > 0:
        direction = f"外資買超 {foreign:.1f} 億、投信買超 {trust:.1f} 億，兩大法人買盤方向一致"
    elif foreign < 0 and trust < 0:
        direction = f"外資賣超 {abs(foreign):.1f} 億、投信賣超 {abs(trust):.1f} 億，法人同步撤出"
    elif foreign > 0 and trust < 0:
        direction = f"外資買超 {foreign:.1f} 億但投信賣超 {abs(trust):.1f} 億，法人買賣方向分歧"
    elif foreign < 0 and trust > 0:
        direction = f"外資賣超 {abs(foreign):.1f} 億但投信買超 {trust:.1f} 億，法人買賣方向分歧"
    else:
        direction = "外資與投信買賣力道皆不明顯"

    dealer_note = f"自營商{'買超' if dealer >= 0 else '賣超'} {abs(dealer):.1f} 億"
    return f"{direction}；{dealer_note}。"


def _picks_view(picks: list) -> str:
    if not picks:
        return "今日觀察名單中沒有標的同時符合多項技術訊號，市場可能處於盤整或訊號不明朗階段，建議觀望為主。"

    tag_counts = {}
    for p in picks:
        for tag in p["tags"]:
            key = tag.split(" ")[0]  # 把「法人連買 3 日」歸到「法人連買」這個key
            tag_counts[key] = tag_counts.get(key, 0) + 1
    top_tag = max(tag_counts, key=tag_counts.get)

    return (
        f"今日觀察名單中共有 {len(picks)} 檔標的符合條件，"
        f"其中以「{top_tag}」訊號最為常見（{tag_counts[top_tag]} 檔），"
        f"訊號分數最高的是 {picks[0]['stock_id']}（{picks[0]['score']} 項訊號同時成立）。"
    )


def _risk_note(market: dict, picks: list) -> str:
    t = market["taiex"]
    b = market["breadth"]
    notes = []
    if abs(t["change_percent"]) >= 1.5:
        notes.append("今日大盤波動幅度較大，追價風險相對提高")
    if b["decline"] > b["advance"] * 2:
        notes.append("跌家數遠多於漲家數，個股風險需留意")
    if len(picks) >= 5:
        notes.append("符合條件的標的數量偏多，留意是否為齊漲齊跌的系統性行情而非個股轉強")
    if not notes:
        notes.append("整體數據未顯示極端訊號，仍建議留意國際股市與消息面變化")
    return "；".join(notes) + "。本區塊為規則式邏輯自動產生，非投資建議。"


def generate_rule_based_insight(market: dict, picks: list) -> dict:
    """不呼叫任何外部AI服務，純粹用if/else規則把數字轉成文字描述，完全免費。"""
    return {
        "market_view": _market_view(market),
        "chip_view": _chip_view(market),
        "picks_view": _picks_view(picks),
        "risk_note": _risk_note(market, picks),
        "generated_at": datetime.now().isoformat(),
        "model": "rule-based-v1",
    }


def generate_ai_insight(market: dict, picks: list) -> dict:
    try:
        from anthropic import Anthropic
    except ImportError as e:
        raise RuntimeError("尚未安裝 anthropic 套件，請先執行 pip install anthropic") from e

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "找不到 ANTHROPIC_API_KEY 環境變數。請先設定："
            '$env:ANTHROPIC_API_KEY = "你的key"（PowerShell當次有效）'
            '或 setx ANTHROPIC_API_KEY "你的key"（永久生效，需開新終端機視窗）'
        )

    client = Anthropic(api_key=api_key)
    prompt = _build_prompt(market, picks)

    resp = client.messages.create(
        model=MODEL,
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}],
    )
    raw_text = resp.content[0].text.strip()

    # 防呆：萬一模型還是加了 ```json 這種 code fence，去掉再解析
    cleaned = raw_text.replace("```json", "").replace("```", "").strip()
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise ValueError(f"AI回傳內容無法解析成JSON，原始內容：{raw_text}") from e

    parsed["generated_at"] = datetime.now().isoformat()
    parsed["model"] = MODEL
    return parsed


if __name__ == "__main__":
    # 本機快速測試：假造一份簡單的market/picks資料結構來測試 prompt 與解析邏輯
    fake_market = {
        "taiex": {"close": 24586.32, "change_points": -170.79, "change_percent": -0.38},
        "turnover_yi": 3842.0,
        "breadth": {"advance": 577, "decline": 679, "unchanged": 121},
        "institutional_yi": {"foreign": -407.2, "trust": -12.0, "dealer": -9.7},
    }
    fake_picks = [
        {"stock_id": "2002", "close": 19.05, "change_percent": 0.26,
         "tags": ["均線多頭排列", "KD黃金交叉", "法人連買 3 日"]}
    ]
    print(json.dumps(generate_ai_insight(fake_market, fake_picks), ensure_ascii=False, indent=2))