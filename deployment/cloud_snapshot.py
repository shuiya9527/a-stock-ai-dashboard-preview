#!/usr/bin/env python3
# ruff: noqa: E501
"""Generate a no-token A-share dashboard snapshot for GitHub Pages.

The collector intentionally uses transparent public endpoints and leaves missing
sections empty. It never carries a previous value forward as if it were current.
"""

from __future__ import annotations

import argparse
import json
import math
import ssl
import time
import urllib.parse
import urllib.request
import uuid
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
SHANGHAI = ZoneInfo("Asia/Shanghai")
try:
    import certifi

    SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    SSL_CONTEXT = ssl.create_default_context()
INDEX_SYMBOLS = [
    ("000001", "上证指数", "sh000001"),
    ("399001", "深证成指", "sz399001"),
    ("399006", "创业板指", "sz399006"),
    ("000688", "科创50", "sh000688"),
    ("000300", "沪深300", "sh000300"),
]
ETF_SYMBOLS = [
    ("510050", "上证50ETF", "sh510050"),
    ("510300", "沪深300ETF", "sh510300"),
    ("510500", "中证500ETF", "sh510500"),
    ("159915", "创业板ETF", "sz159915"),
    ("588000", "科创50ETF", "sh588000"),
]


def now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def api(data: Any, message: str) -> dict[str, Any]:
    return {"success": True, "data": data, "message": message}


def number(value: Any) -> float | None:
    try:
        if value in (None, "", "-", "--"):
            return None
        return float(str(value).replace(",", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return None


def http_bytes(url: str, params: dict[str, str] | None = None, referer: str | None = None) -> bytes:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json,text/plain,*/*"}
    if referer:
        headers["Referer"] = referer
    request = urllib.request.Request(url, headers=headers)
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            with urllib.request.urlopen(request, timeout=18, context=SSL_CONTEXT) as response:
                return response.read()
        except Exception as exc:  # noqa: BLE001 - upstream failures become availability facts
            last_error = exc
            time.sleep(1.2 * (attempt + 1))
    raise RuntimeError(f"upstream request failed: {last_error}")


def http_json(url: str, params: dict[str, str] | None = None, referer: str | None = None) -> Any:
    raw = http_bytes(url, params, referer)
    for encoding in ("utf-8", "gbk"):
        try:
            return json.loads(raw.decode(encoding))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
    raise ValueError("upstream did not return JSON")


def tencent_values(symbol: str) -> list[str]:
    text = http_bytes(f"https://qt.gtimg.cn/q={symbol}", referer="https://gu.qq.com/").decode(
        "gbk", errors="ignore"
    )
    if '"' not in text:
        raise ValueError(f"Tencent quote missing for {symbol}")
    return text.split('"', 2)[1].split("~")


def quote_record(code: str, expected_name: str | None = None) -> dict[str, Any]:
    symbol = ("bj" if code.startswith(("4", "8", "92")) else "sh" if code.startswith(("6", "9")) else "sz") + code
    values = tencent_values(symbol)

    def at(index: int) -> float | None:
        return number(values[index]) if index < len(values) else None

    return {
        "code": code,
        "name": values[1] if len(values) > 1 and values[1] else expected_name or code,
        "market": "上海" if symbol.startswith("sh") else "北交所" if symbol.startswith("bj") else "深圳",
        "source": "腾讯财经实时行情",
        "updated_at": now_iso(),
        "price": at(3),
        "previous_close": at(4),
        "open": at(5),
        "high": at(33),
        "low": at(34),
        "change_amount": at(31),
        "change_percent": at(32),
        "volume": at(36),
        "amount": at(37) * 10_000 if at(37) is not None else None,
        "turnover_percent": at(38),
        "pe_ttm": at(39),
        "pb": at(46),
        "total_market_cap": at(44) * 100_000_000 if at(44) is not None else None,
        "float_market_cap": at(45) * 100_000_000 if at(45) is not None else None,
    }


def index_record(code: str, name: str, symbol: str, source: str) -> dict[str, Any]:
    values = tencent_values(symbol)
    return {
        "code": code,
        "name": name,
        "price": number(values[3]) if len(values) > 3 else None,
        "change_percent": number(values[32]) if len(values) > 32 else None,
        "updated_at": now_iso(),
        "source": source,
    }


def tencent_kline(code: str, limit: int = 180) -> list[dict[str, Any]]:
    symbol = ("bj" if code.startswith(("4", "8", "92")) else "sh" if code.startswith(("6", "9")) else "sz") + code
    payload = http_json(
        "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
        {"param": f"{symbol},day,,,{limit},qfq"},
        "https://gu.qq.com/",
    )
    stock = (payload.get("data") or {}).get(symbol) or {}
    rows = stock.get("qfqday") or stock.get("day") or []
    return [
        {
            "date": str(row[0])[:10],
            "open": number(row[1]),
            "close": number(row[2]),
            "high": number(row[3]),
            "low": number(row[4]),
            "volume": number(row[5]),
        }
        for row in rows
        if len(row) >= 6 and all(number(row[index]) is not None for index in (1, 2, 3, 4))
    ]


def eastmoney_fund_flow(code: str) -> list[dict[str, Any]]:
    secid = ("1." if code.startswith(("6", "9")) else "0.") + code
    payload = http_json(
        "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get",
        {
            "secid": secid,
            "lmt": "90",
            "klt": "101",
            "fields1": "f1,f2,f3,f7",
            "fields2": "f51,f52,f53,f54,f55,f56,f57",
        },
        "https://quote.eastmoney.com/",
    )
    result = []
    for line in (payload.get("data") or {}).get("klines") or []:
        values = line.split(",")
        if len(values) >= 6:
            result.append(
                {
                    "date": values[0],
                    "main_net": number(values[1]),
                    "small_net": number(values[2]),
                    "mid_net": number(values[3]),
                    "large_net": number(values[4]),
                    "super_net": number(values[5]),
                }
            )
    return result


def hot_market() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    date = datetime.now(SHANGHAI).strftime("%Y-%m-%d")
    payload = http_json(
        f"http://zx.10jqka.com.cn/event/api/getharden/date/{date}/orderby/date/orderway/desc/charset/GBK/"
    )
    counts: Counter[str] = Counter()
    examples: dict[str, list[str]] = {}
    stocks = []
    for row in payload.get("data") or []:
        code, name, reason = str(row.get("code") or ""), str(row.get("name") or ""), str(row.get("reason") or "").strip()
        if not (code and name and reason):
            continue
        stocks.append({"code": code, "name": name, "reason": reason, "source": "同花顺当日强势股题材归因"})
        for theme in (item.strip() for item in reason.split("+")):
            if not theme:
                continue
            counts[theme] += 1
            examples.setdefault(theme, [])
            if len(examples[theme]) < 3:
                examples[theme].append(f"{name}({code})")
    themes = [
        {"name": name, "mention_count": count, "examples": examples[name], "source": "同花顺当日强势股题材归因"}
        for name, count in counts.most_common(20)
    ]
    return themes, stocks


def hot_stocks() -> list[dict[str, Any]]:
    payload = http_json(
        "https://dq.10jqka.com.cn/fuyao/hot_list_data/out/hot_list/v1/stock",
        {"stock_type": "a", "type": "hour", "list_type": "normal"},
    )
    result = []
    for index, row in enumerate((payload.get("data") or {}).get("stock_list") or []):
        tag = row.get("tag") or {}
        result.append(
            {
                "rank": int(number(row.get("order")) or index + 1),
                "code": str(row.get("code") or ""),
                "name": str(row.get("name") or ""),
                "heat": number(row.get("rate")),
                "change_percent": number(row.get("rise_and_fall")),
                "rank_change": int(number(row.get("hot_rank_chg")) or 0),
                "concepts": [str(item) for item in tag.get("concept_tag") or []],
                "tag": str(tag.get("popularity_tag") or "") or None,
                "source": "同花顺小时人气榜",
            }
        )
    return result


def northbound() -> dict[str, Any] | None:
    payload = http_json(
        "https://data.hexin.cn/market/hsgtApi/method/dayChart/",
        referer="https://data.hexin.cn/",
    )
    times, shanghai, shenzhen = payload.get("time") or [], payload.get("hgt") or [], payload.get("sgt") or []
    for index in range(min(len(shanghai), len(shenzhen)) - 1, -1, -1):
        sh, sz = number(shanghai[index]), number(shenzhen[index])
        if sh is not None and sz is not None:
            return {
                "time": str(times[index]) if index < len(times) else None,
                "shanghai_net_buy_yi": sh,
                "shenzhen_net_buy_yi": sz,
                "total_net_buy_yi": round(sh + sz, 4),
                "source": "同花顺沪深股通分钟流向",
            }
    return None


def news() -> list[dict[str, Any]]:
    payload = http_json(
        "https://np-weblist.eastmoney.com/comm/web/getFastNewsList",
        {"client": "web", "biz": "web_724", "fastColumn": "102", "sortEnd": "", "pageSize": "20", "req_trace": str(uuid.uuid4())},
        "https://kuaixun.eastmoney.com/",
    )
    return [
        {
            "title": str(row.get("title") or ""),
            "summary": str(row.get("summary") or "")[:300],
            "published_at": str(row.get("showTime") or "") or None,
            "source": "东方财富全球资讯",
            "url": None,
        }
        for row in (payload.get("data") or {}).get("fastNewsList") or []
    ][:10]


def average(values: list[float], period: int) -> float | None:
    return sum(values[-period:]) / period if len(values) >= period else None


def ema_latest(values: list[float], period: int) -> float | None:
    if not values:
        return None
    multiplier, result = 2 / (period + 1), values[0]
    for value in values[1:]:
        result = value * multiplier + result * (1 - multiplier)
    return result


def macd_latest(values: list[float]) -> tuple[float | None, float | None, float | None]:
    if len(values) < 26:
        return None, None, None
    fast, slow = values[0], values[0]
    dif_values = []
    for value in values:
        fast = value * (2 / 13) + fast * (11 / 13)
        slow = value * (2 / 27) + slow * (25 / 27)
        dif_values.append(fast - slow)
    dea = ema_latest(dif_values, 9)
    return dif_values[-1], dea, (dif_values[-1] - dea) * 2 if dea is not None else None


def rsi_latest(values: list[float], period: int = 14) -> float | None:
    if len(values) <= period:
        return None
    changes = [values[index] - values[index - 1] for index in range(len(values) - period, len(values))]
    gain = sum(max(value, 0) for value in changes) / period
    loss = sum(max(-value, 0) for value in changes) / period
    return 100 if loss == 0 else 100 - 100 / (1 + gain / loss)


def technical_snapshot(rows: list[dict[str, Any]]) -> dict[str, Any]:
    closes = [float(row["close"]) for row in rows if row.get("close") is not None]
    volumes = [float(row["volume"]) for row in rows if row.get("volume") is not None]
    dif, dea, histogram = macd_latest(closes)
    ma20 = average(closes, 20)
    deviation = math.sqrt(sum((value - ma20) ** 2 for value in closes[-20:]) / 20) if ma20 is not None else None
    return {
        "sample_count": len(closes),
        "latest_close": closes[-1] if closes else None,
        "ma_5": average(closes, 5), "ma_10": average(closes, 10), "ma_20": ma20,
        "ema_5": ema_latest(closes, 5), "ema_10": ema_latest(closes, 10), "ema_20": ema_latest(closes, 20),
        "macd_dif": dif, "macd_dea": dea, "macd_histogram": histogram,
        "rsi_14": rsi_latest(closes), "kdj_k": None, "kdj_d": None, "kdj_j": None,
        "boll_upper": ma20 + 2 * deviation if ma20 is not None and deviation is not None else None,
        "boll_middle": ma20,
        "boll_lower": ma20 - 2 * deviation if ma20 is not None and deviation is not None else None,
        "support": min((float(row["low"]) for row in rows[-20:] if row.get("low") is not None), default=None),
        "resistance": max((float(row["high"]) for row in rows[-20:] if row.get("high") is not None), default=None),
        "volume_ratio_5d": volumes[-1] / average(volumes, 5) if volumes and average(volumes, 5) not in (None, 0) else None,
        "source": "腾讯财经复权日线",
        "observed_at": now_iso(),
    }


def stock_analysis(quote: dict[str, Any], rows: list[dict[str, Any]], flows: list[dict[str, Any]], reason: str) -> tuple[dict[str, Any], dict[str, Any]]:
    technical = technical_snapshot(rows)
    close, ma20, macd_hist, rsi = technical["latest_close"], technical["ma_20"], technical["macd_histogram"], technical["rsi_14"]
    technical_score = 50.0
    technical_score += 15 if close is not None and ma20 is not None and close >= ma20 else -10
    technical_score += 15 if macd_hist is not None and macd_hist > 0 else -10
    technical_score += 10 if rsi is not None and 45 <= rsi <= 70 else -10 if rsi is not None and rsi >= 80 else 0
    technical_score = max(0.0, min(100.0, technical_score))
    pe, pb = quote.get("pe_ttm"), quote.get("pb")
    fundamental_score = 50.0 + (10 if pe is not None and 0 < pe <= 30 else -8 if pe is not None and (pe <= 0 or pe > 80) else 0) + (8 if pb is not None and 0 < pb <= 4 else -5 if pb is not None and pb > 10 else 0)
    risk_score = 35.0 if "ST" in quote["name"].upper() else 80.0
    components = [
        ("technical", "技术面", 30.0, technical_score, 1.0, [f"腾讯复权日线 {len(rows)} 条。", f"MA20={ma20}，MACD柱={macd_hist}，RSI14={rsi}。"], []),
        ("capital_flow", "资金面", 25.0, 55.0 if flows else None, 0.6 if flows else 0.0, [f"个股资金流 {len(flows)} 日。"] if flows else ["资金流上游暂不可用。"], [] if flows else ["个股资金流"]),
        ("fundamental", "基本面", 20.0, max(0.0, min(100.0, fundamental_score)), 0.55 if pe is not None or pb is not None else 0.0, [f"PE(TTM)={pe}，PB={pb}。"], [] if pe is not None or pb is not None else ["PE", "PB"]),
        ("industry", "行业景气", 10.0, 65.0 if reason else None, 0.6 if reason else 0.0, [f"同花顺强势归因：{reason}"] if reason else ["未进入当日强势题材池。"], [] if reason else ["题材归因"]),
        ("news_sentiment", "公告与新闻", 10.0, 55.0 if reason else None, 0.3 if reason else 0.0, ["仅使用当日强势归因，不把新闻标题直接解释为利好。"] if reason else ["公告新闻未在轻量云采集中闭环。"], ["公告全文", "新闻语义"]),
        ("risk_control", "风险控制", 5.0, risk_score, 0.5, ["名称 ST 硬风控检查已完成；完整减持、解禁、质押仍待 API 证据。"], ["全量减持", "质押"]),
    ]
    available_weight = sum(weight * coverage for _, _, weight, _, coverage, _, _ in components)
    weighted = sum((score or 0) * weight * coverage / 100 for _, _, weight, score, coverage, _, _ in components)
    total_score = round(weighted / available_weight * 100, 2) if available_weight >= 60 else None
    component_payload = [
        {"key": key, "name": name, "weight": weight, "status": "available" if score is not None else "missing", "raw_score": score, "weighted_score": round((score or 0) * weight * coverage / 100, 4), "coverage_ratio": coverage, "evidence": evidence, "missing_fields": missing}
        for key, name, weight, score, coverage, evidence, missing in components
    ]
    risk_level = "high" if risk_score <= 45 else "medium" if total_score is not None and total_score < 75 else "low"
    score = {
        "code": quote["code"], "name": quote["name"], "generated_at": now_iso(), "model_version": "github-cloud-snapshot-v1",
        "score_status": "available" if total_score is not None else "partial", "total_score": total_score, "partial_score": round(weighted, 2),
        "available_weight": round(available_weight, 2), "coverage_ratio": round(available_weight / 100, 4), "upward_probability": None,
        "risk_level": risk_level, "components": component_payload,
        "data_completeness": {"code": quote["code"], "generated_at": now_iso(), "coverage_ratio": round(available_weight / 100, 4), "available_weight": round(available_weight, 2), "required_weight": 100.0, "items": [], "next_actions": ["补齐公告、解禁、质押和个股新闻语义证据。"]},
        "notes": ["云端轻量评分只使用本次成功采集的真实字段，缺失维度不填默认分。"],
    }
    support, resistance = technical["support"], technical["resistance"]
    if total_score is None:
        state, title, summary = "data_accumulation", "数据积累", "覆盖率不足，不形成选股状态。"
    elif risk_level == "high":
        state, title, summary = "risk_control", "风险控制优先", "名称或风险规则触发硬风控。"
    elif close is not None and resistance is not None and close >= resistance * 0.97:
        state, title, summary = "avoid_chasing", "接近压力，避免追高", "价格接近 20 日压力位，等待突破或回踩。"
    elif close is not None and ma20 is not None and close >= ma20 and (macd_hist or 0) > 0:
        state, title, summary = "trend_observe", "趋势观察", "价格位于 MA20 上方且 MACD 柱为正。"
    else:
        state, title, summary = "pullback_watch", "回踩观察", "等待价格在支撑和 MA20 附近形成新证据。"
    strategy = {"state": state, "title": title, "summary": summary, "observation_trigger": f"支撑 {support} / 压力 {resistance}，结合量能复核。", "invalidation_reference": f"跌破 {support} 后重新评估。" if support else "支撑数据不足。", "risk_note": "规则状态不构成买卖指令。"}
    research = {
        "code": quote["code"], "name": quote["name"], "generated_at": now_iso(), "score": score, "technical": technical,
        "trend": {"candles": rows[-120:], "fund_flow": flows[-90:], "price_source": "腾讯财经复权日线", "fund_flow_source": "东方财富个股资金流" if flows else None, "price_updated_at": now_iso(), "fund_flow_updated_at": now_iso() if flows else None, "data_gaps": [] if flows else ["资金流上游暂不可用，不以成交量代替主力资金。"]},
        "strategy": strategy, "evidence_sources": ["腾讯财经复权日线", "腾讯财经实时行情", "同花顺强势股题材归因"] + (["东方财富个股资金流"] if flows else []),
        "disclaimer": "云端快照用于研究观察，不构成投资建议或收益承诺。",
    }
    candidate = {
        "code": quote["code"], "name": quote["name"], "total_score": total_score or 0, "coverage_ratio": score["coverage_ratio"], "risk_level": "medium" if risk_level == "high" else risk_level,
        "reasons": [item for item in [f"技术规则分 {technical_score:.0f}", f"强势归因：{reason}" if reason else None] if item], "evidence_updated_at": now_iso(),
        "latest_price": quote.get("price"), "change_percent": quote.get("change_percent"), "pe_ttm": pe, "pb": pb,
        "total_market_cap_yi": round(quote["total_market_cap"] / 100_000_000, 2) if quote.get("total_market_cap") else None,
        "industry": reason.split("+")[0] if reason else None, "opportunity_type": {"trend_observe": "trend", "pullback_watch": "pullback", "risk_control": "risk_control"}.get(state, "observe"),
        "strategy_title": title, "strategy_summary": summary, "support": support, "resistance": resistance, "volume_ratio_5d": technical["volume_ratio_5d"],
        "capital_summary": f"已采集 {len(flows)} 日资金流。" if flows else "资金流上游暂不可用。", "catalysts": [f"强势归因：{reason}"] if reason else [],
        "risks": ["ST 名称硬风控"] if "ST" in quote["name"].upper() else [], "concept_tags": [item for item in reason.split("+") if item][:5],
        "evidence_sources": research["evidence_sources"], "data_gaps": score["data_completeness"]["next_actions"],
        "components": [{"key": item["key"], "name": item["name"], "score": item["raw_score"], "coverage_ratio": item["coverage_ratio"]} for item in component_payload],
    }
    return candidate, research


def collect(output: Path) -> None:
    generated_at = now_iso()
    errors: dict[str, str] = {}
    indices, etfs = [], []
    for code, name, symbol in INDEX_SYMBOLS:
        try:
            indices.append(index_record(code, name, symbol, "腾讯财经实时指数"))
        except Exception as exc:  # noqa: BLE001
            errors[f"index_{code}"] = exc.__class__.__name__
    for code, name, symbol in ETF_SYMBOLS:
        try:
            etfs.append(index_record(code, name, symbol, "腾讯财经实时ETF"))
        except Exception as exc:  # noqa: BLE001
            errors[f"etf_{code}"] = exc.__class__.__name__
    try:
        themes, strong = hot_market()
    except Exception as exc:  # noqa: BLE001
        themes, strong = [], []
        errors["hot_market"] = exc.__class__.__name__
    try:
        hot = hot_stocks()
    except Exception as exc:  # noqa: BLE001
        hot = []
        errors["hot_stocks"] = exc.__class__.__name__
    try:
        north = northbound()
    except Exception as exc:  # noqa: BLE001
        north = None
        errors["northbound"] = exc.__class__.__name__
    try:
        news_items = news()
    except Exception as exc:  # noqa: BLE001
        news_items = []
        errors["news"] = exc.__class__.__name__

    reason_by_code = {item["code"]: item["reason"] for item in strong}
    names = {item["code"]: item["name"] for item in strong}
    for item in hot:
        names.setdefault(item["code"], item["name"])
    universe = []
    for item in strong + [{"code": row["code"]} for row in hot]:
        code = item.get("code", "")
        if len(code) == 6 and code not in universe and "ST" not in names.get(code, "").upper():
            universe.append(code)
        if len(universe) >= 18:
            break
    candidates, research_payloads, quotes = [], {}, []
    for code in universe:
        try:
            quote = quote_record(code, names.get(code))
            rows = tencent_kline(code)
            try:
                flows = eastmoney_fund_flow(code)
            except Exception:  # noqa: BLE001
                flows = []
            candidate, research = stock_analysis(quote, rows, flows, reason_by_code.get(code, ""))
            quotes.append(quote)
            research_payloads[code] = api(research, "cloud stock research snapshot loaded")
            if candidate["total_score"] > 0 and research["score"]["risk_level"] != "high":
                candidates.append(candidate)
        except Exception as exc:  # noqa: BLE001
            errors[f"stock_{code}"] = exc.__class__.__name__
        time.sleep(0.25)
    candidates.sort(key=lambda item: item["total_score"], reverse=True)

    index_changes = [item["change_percent"] for item in indices if item["change_percent"] is not None]
    index_average = round(sum(index_changes) / len(index_changes), 2) if index_changes else None
    environment = "偏强" if index_average is not None and index_average >= 0.6 else "偏弱" if index_average is not None and index_average <= -0.6 else "震荡"
    rising = sum(1 for value in index_changes if value > 0)
    falling = sum(1 for value in index_changes if value < 0)
    opportunity = [f"同花顺高频题材：{item['name']}({item['mention_count']})。" for item in themes[:5]]
    if north and north.get("total_net_buy_yi") is not None:
        opportunity.append(f"北向分钟流向合计 {north['total_net_buy_yi']:.2f} 亿元。")
    risk_signals = [f"五大指数平均涨跌幅 {index_average:.2f}%。"] if index_average is not None and index_average < -0.6 else []
    availability = [
        {"key": "indices", "title": "市场指数", "available": bool(indices), "message": "腾讯财经云端采集。" if indices else errors.get("index_000001", "不可用")},
        {"key": "etfs", "title": "核心ETF", "available": bool(etfs), "message": "腾讯财经云端采集。" if etfs else "不可用"},
        {"key": "industries", "title": "行业涨跌", "available": False, "message": "轻量云采集暂不请求东方财富全行业列表。"},
        {"key": "concepts", "title": "概念涨跌", "available": False, "message": "轻量云采集暂不请求东方财富全概念列表。"},
        {"key": "hot_themes", "title": "题材热度", "available": bool(themes), "message": "同花顺强势股归因。" if themes else errors.get("hot_market", "不可用")},
        {"key": "hot_stocks", "title": "同花顺人气榜", "available": bool(hot), "message": "同花顺小时榜。" if hot else errors.get("hot_stocks", "不可用")},
        {"key": "strong_stocks", "title": "强势股归因", "available": bool(strong), "message": "同花顺强势股归因。" if strong else "不可用"},
        {"key": "breadth", "title": "市场广度与主力资金", "available": False, "message": "云端轻量采集不估算全市场广度。"},
        {"key": "northbound", "title": "北向资金", "available": north is not None, "message": "同花顺分钟流向。" if north else errors.get("northbound", "不可用")},
        {"key": "dragon_tiger", "title": "龙虎榜", "available": False, "message": "收盘后由完整 API 补充。"},
        {"key": "news", "title": "财经快讯", "available": bool(news_items), "message": "东方财富全球资讯。" if news_items else errors.get("news", "不可用")},
        {"key": "sentiment", "title": "涨停情绪", "available": False, "message": "云端未取得完整涨跌停池，不计算情绪分。"},
    ]
    market = {
        "generated_at": generated_at, "indices": indices, "etfs": etfs,
        "industries_top": [], "industries_bottom": [], "concepts_top": [], "concepts_bottom": [],
        "hot_themes": themes[:15], "hot_stocks": hot[:20], "strong_stocks": strong[:40],
        "breadth": None, "capital_flow": None, "northbound": north, "dragon_tiger": [], "news": news_items, "availability": availability,
        "conclusion": {
            "market_environment": environment, "environment_evidence": f"五大指数平均涨跌幅为 {index_average:.2f}%。" if index_average is not None else "指数数据不足。",
            "sentiment": None, "northbound_evidence": f"北向分钟流向合计 {north['total_net_buy_yi']:.2f} 亿元。" if north and north.get("total_net_buy_yi") is not None else "北向资金暂不可用。",
            "hot_industries": [item["name"] for item in themes[:5]], "main_capital_status": "全市场主力资金暂未在轻量云采集中闭环。",
            "hot_continuity_status": "当前为当日题材截面，持续性需跨日快照后验证。", "risk_level": "中等",
            "operation_guidance": "只观察证据覆盖充分的标的，不把云端快照直接当作买卖指令。", "recommendation_status": "仅输出达到覆盖阈值且未触发硬风控的研究候选。",
            "risk_list_status": "ST 名称硬风控已执行；减持、解禁和质押由完整 API 补齐。", "index_average_change": index_average,
            "rising_indices": rising, "falling_indices": falling, "market_temperature": "待完整涨跌停池评估", "opportunity_signals": opportunity,
            "risk_signals": risk_signals, "evidence_summary": [f"指数 {len(indices)}/5、ETF {len(etfs)}/5、题材 {len(themes)}、候选 {len(candidates)}。"],
            "data_constraints": ["GitHub Actions 使用境外云网络，部分东方财富接口可能受地域或风控影响。", "未取得完整涨跌停池时不计算情绪分。", "未导入个人持仓成本与仓位。"],
        },
        "disclaimer": "云端快照来自公开上游接口，缺失字段保持空值，不构成投资建议。",
    }
    selection = {"generated_at": generated_at, "universe_size": len(universe), "evaluated_count": len(research_payloads), "candidates": candidates[:15], "status": "available" if candidates else "collecting", "message": "GitHub Actions 云端候选池；仅使用本次成功采集的真实字段。", "disclaimer": "候选为研究筛选，不构成买卖建议，不保证收益。"}
    overview = {
        "generated_at": generated_at,
        "market_snapshot": {"data_quality": {"sample_count": len(quotes), "required_minimum": 5, "is_sufficient": len(quotes) >= 5, "message": f"云端成功采集 {len(quotes)} 只股票。"}, "total_securities": len(quotes), "total_quote_snapshots": len(quotes), "latest_quote_at": generated_at if quotes else None, "market_distribution": [], "source_distribution": [{"name": "腾讯财经", "count": len(quotes)}], "message": "云端快照覆盖事实，不构成市场方向判断。"},
        "latest_quotes": [{**item, "snapshot_count": 1} for item in quotes[:20]],
        "score_summaries": [{"code": item["code"], "name": item["name"], "score_status": "available", "total_score": item["total_score"], "coverage_ratio": item["coverage_ratio"], "available_weight": round(item["coverage_ratio"] * 100, 2), "risk_level": item["risk_level"]} for item in candidates[:20]],
        "data_sources": [
            {"name": "tencent", "display_name": "腾讯财经", "enabled": True, "requires_token": False, "status": "configured" if indices else "unavailable", "message": "指数、ETF、个股行情和复权日线。"},
            {"name": "ths", "display_name": "同花顺公开数据", "enabled": True, "requires_token": False, "status": "configured" if themes else "unavailable", "message": "题材归因、人气榜和北向分钟流向。"},
            {"name": "eastmoney", "display_name": "东方财富公开数据", "enabled": True, "requires_token": False, "status": "configured" if news_items else "unavailable", "message": "财经快讯与可用的个股资金流。"},
        ],
        "unavailable_sections": [
            {"key": "cloud_dynamic_research", "title": "云端即时查询", "message": "关机期间只能查看已进入云端候选池的个股走势；新代码即时查询需等待下一轮采集。"},
            {"key": "full_risk", "title": "完整风险证据", "message": "减持、解禁、质押、审计意见和公告全文仍由完整 FastAPI 补齐。"},
        ],
        "disclaimer": "GitHub Actions 每 30 分钟生成公开数据快照；缺失项不估算。",
    }
    tiger = {"model_name": "虎爷模型：3板及以上断板反包 v2", "generated_at": generated_at, "rules": {"minimum_board_height": 3, "volume_ratio_max": 0.4, "pullback_depth_min": 0.02, "pullback_depth_max": 0.22, "support_confirmation_required": True, "take_profit_percent": 12, "stop_loss_percent": 5}, "backtest": {"sample_period": "报告原始样本", "sample_count": 27, "win_rate": 0.63, "profit_loss_ratio": 2.28, "average_return": 0.051}, "live_status": {"trade_date": datetime.now(SHANGHAI).strftime("%Y%m%d"), "market_gate_passed": False, "gate_evidence": ["云端轻量采集未取得完整涨停池。"], "scan_status": "证据门槛未闭环，本次不触发模型扫描。", "signals": [], "missing_data": ["完整涨停池", "断板事件池"]}, "disclaimer": "报告回测不代表未来；实时门槛不足时不输出信号。"}

    output.mkdir(parents=True, exist_ok=True)
    (output / "research").mkdir(exist_ok=True)
    payloads = {
        "health.json": api({"status": "ok", "service": "AI A股智能分析系统", "environment": "github-cloud-snapshot"}, "cloud snapshot is healthy"),
        "system-status.json": api({"phase": "第六阶段：Dashboard 与证据驱动选股", "data_mode": "live_http", "database_engine": "github-json-snapshot", "modules": ["market", "themes", "scoring", "stock_trend", "news"]}, "cloud system status loaded"),
        "market-overview.json": api(market, "cloud market overview loaded"),
        "dashboard-overview.json": api(overview, "cloud dashboard overview loaded"),
        "scoring-recommendations-today.json": api(selection, "cloud selection loaded"),
        "tiger-model-overview.json": api(tiger, "cloud tiger model loaded"),
        "manifest.json": {"generated_at": generated_at, "collector": "github-cloud-snapshot-v1", "errors": errors, "stock_count": len(research_payloads)},
    }
    for filename, payload in payloads.items():
        (output / filename).write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    for code, payload in research_payloads.items():
        (output / "research" / f"{code}.json").write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    if not indices and not research_payloads:
        raise RuntimeError("critical cloud sources returned no usable market or stock data")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    collect(args.output)


if __name__ == "__main__":
    main()
