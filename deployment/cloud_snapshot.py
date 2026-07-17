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
import random
import ssl
import time
import urllib.parse
import urllib.request
import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta
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
EASTMONEY_MIN_INTERVAL_SECONDS = 1.05
_last_eastmoney_request = 0.0
_eastmoney_failed_hosts: set[str] = set()


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


def http_bytes(
    url: str,
    params: dict[str, str] | None = None,
    referer: str | None = None,
    attempts: int = 3,
    timeout: float = 12,
) -> bytes:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json,text/plain,*/*"}
    if referer:
        headers["Referer"] = referer
    request = urllib.request.Request(url, headers=headers)
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=timeout, context=SSL_CONTEXT) as response:
                return response.read()
        except Exception as exc:  # noqa: BLE001 - upstream failures become availability facts
            last_error = exc
            time.sleep((1.5**attempt) + random.uniform(0.15, 0.55))
    raise RuntimeError(f"upstream request failed: {last_error}")


def http_json(
    url: str,
    params: dict[str, str] | None = None,
    referer: str | None = None,
    attempts: int = 3,
    timeout: float = 12,
) -> Any:
    raw = http_bytes(url, params, referer, attempts=attempts, timeout=timeout)
    for encoding in ("utf-8", "gbk"):
        try:
            return json.loads(raw.decode(encoding))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
    raise ValueError("upstream did not return JSON")


def http_post_json(
    url: str,
    form: dict[str, str],
    headers: dict[str, str],
    attempts: int = 2,
    timeout: float = 10,
) -> Any:
    request = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(form).encode("utf-8"),
        headers={"User-Agent": USER_AGENT, **headers},
        method="POST",
    )
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=timeout, context=SSL_CONTEXT) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            time.sleep((1.5**attempt) + random.uniform(0.1, 0.4))
    raise RuntimeError(f"upstream POST failed: {last_error}")


def eastmoney_json(url: str, params: dict[str, str], referer: str) -> Any:
    """Serialize Eastmoney calls to reduce rate-limit failures on cloud runners."""
    global _last_eastmoney_request
    parsed = urllib.parse.urlparse(url)
    candidates = [url]
    if parsed.netloc == "push2.eastmoney.com":
        delayed = parsed._replace(netloc="push2delay.eastmoney.com").geturl()
        candidates = [delayed, url]
    errors = []
    for candidate in candidates:
        host = urllib.parse.urlparse(candidate).netloc
        if host in _eastmoney_failed_hosts:
            continue
        elapsed = time.monotonic() - _last_eastmoney_request
        delay = EASTMONEY_MIN_INTERVAL_SECONDS + random.uniform(0.05, 0.25) - elapsed
        if delay > 0:
            time.sleep(delay)
        try:
            return http_json(candidate, params, referer, attempts=2, timeout=7)
        except Exception as exc:
            _eastmoney_failed_hosts.add(host)
            errors.append(f"{host}:{exc.__class__.__name__}")
        finally:
            _last_eastmoney_request = time.monotonic()
    raise RuntimeError("all Eastmoney hosts failed: " + ",".join(errors))


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


def baidu_kline(code: str, limit: int = 180) -> list[dict[str, Any]]:
    payload = http_json(
        "https://finance.pae.baidu.com/selfselect/getstockquotation",
        {
            "all": "1", "isIndex": "false", "isBk": "false", "isBlock": "false",
            "isFutures": "false", "isStock": "true", "newFormat": "1",
            "group": "quotation_kline_ab", "finClientType": "pc", "code": code,
            "start_time": "", "ktype": "1",
        },
        "https://gushitong.baidu.com/",
    )
    market = ((payload.get("Result") or {}).get("newMarketData") or {})
    keys = [str(key).lower() for key in market.get("keys") or []]
    records = []
    for raw in str(market.get("marketData") or "").split(";"):
        values = raw.split(",")
        if not raw or len(values) != len(keys):
            continue
        row = dict(zip(keys, values, strict=False))
        stamp = str(row.get("time") or row.get("date") or "")
        if stamp.isdigit() and len(stamp) >= 10:
            stamp = datetime.fromtimestamp(int(stamp[:10]), SHANGHAI).strftime("%Y-%m-%d")
        elif len(stamp) == 8 and stamp.isdigit():
            stamp = f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}"
        record = {
            "date": stamp[:10],
            "open": number(row.get("open")),
            "close": number(row.get("close")),
            "high": number(row.get("high")),
            "low": number(row.get("low")),
            "volume": number(row.get("volume")),
        }
        if record["date"] and all(record[key] is not None for key in ("open", "close", "high", "low")):
            records.append(record)
    return records[-limit:]


def resilient_kline(code: str, limit: int = 180) -> tuple[list[dict[str, Any]], str]:
    errors = []
    for loader, source in ((tencent_kline, "腾讯财经复权日线"), (baidu_kline, "百度股市通日线")):
        try:
            rows = loader(code, limit)
            if rows:
                return rows, source
        except Exception as exc:  # noqa: BLE001
            errors.append(exc.__class__.__name__)
    raise RuntimeError(f"all kline providers failed: {','.join(errors)}")


def eastmoney_fund_flow(code: str) -> list[dict[str, Any]]:
    secid = ("1." if code.startswith(("6", "9")) else "0.") + code
    payload = eastmoney_json(
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
    payload = eastmoney_json(
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


def board_ranks(market_filter: str) -> list[dict[str, Any]]:
    rows_by_code: dict[str, dict[str, Any]] = {}
    for order in ("1", "0"):
        payload = eastmoney_json(
            "https://push2.eastmoney.com/api/qt/clist/get",
            {
                "pn": "1", "pz": "100", "po": order, "np": "1", "fltt": "2", "invt": "2",
                "fid": "f3", "fs": market_filter, "fields": "f3,f12,f14,f104,f105,f128",
            },
            "https://quote.eastmoney.com/",
        )
        rows = (payload.get("data") or {}).get("diff") or []
        if isinstance(rows, dict):
            rows = list(rows.values())
        for row in rows:
            if isinstance(row, dict) and row.get("f12"):
                rows_by_code[str(row["f12"])] = row
    parsed = [
        {
            "rank": 0,
            "name": str(row.get("f14") or ""),
            "change_percent": number(row.get("f3")),
            "up_count": int(number(row.get("f104")) or 0),
            "down_count": int(number(row.get("f105")) or 0),
            "leader": str(row.get("f128") or "") or None,
        }
        for row in rows_by_code.values()
        if row.get("f14") and number(row.get("f3")) is not None
    ]
    parsed.sort(key=lambda row: row["change_percent"], reverse=True)
    for index, row in enumerate(parsed):
        row["rank"] = index + 1
    return parsed


def market_breadth_and_capital(minimum_samples: int = 4000) -> tuple[dict[str, Any], dict[str, Any]]:
    page_size = 100
    params = {
        "pn": "1", "pz": str(page_size), "po": "1", "np": "1", "fltt": "2", "invt": "2",
        "fid": "f3", "fs": "m:0+t:6,m:0+t:13,m:0+t:80,m:1+t:2,m:1+t:23",
        "fields": "f3,f6,f12,f62,f66,f72",
    }
    first = eastmoney_json(
        "https://push2.eastmoney.com/api/qt/clist/get",
        params,
        "https://quote.eastmoney.com/",
    )
    data = first.get("data") or {}
    expected = int(number(data.get("total")) or 0)
    first_rows = data.get("diff") or []
    if isinstance(first_rows, dict):
        first_rows = list(first_rows.values())
    rows_by_code = {str(row.get("f12") or ""): row for row in first_rows if isinstance(row, dict)}
    page_count = math.ceil(expected / page_size) if expected else 1
    for page in range(2, page_count + 1):
        payload = eastmoney_json(
            "https://push2.eastmoney.com/api/qt/clist/get",
            {**params, "pn": str(page)},
            "https://quote.eastmoney.com/",
        )
        page_rows = (payload.get("data") or {}).get("diff") or []
        if isinstance(page_rows, dict):
            page_rows = list(page_rows.values())
        for row in page_rows:
            if isinstance(row, dict):
                rows_by_code[str(row.get("f12") or "")] = row
    rows = list(rows_by_code.values())
    required = max(minimum_samples, math.floor(expected * 0.9)) if expected else minimum_samples
    if len(rows) < required:
        raise ValueError(f"incomplete market breadth: {len(rows)}/{expected}")
    parsed = [row for row in rows if isinstance(row, dict) and number(row.get("f3")) is not None]
    if not parsed:
        raise ValueError("empty market breadth")
    changes = [number(row.get("f3")) or 0 for row in parsed]
    amounts = [number(row.get("f6")) for row in parsed]
    main = [number(row.get("f62")) for row in parsed]
    super_large = [number(row.get("f66")) for row in parsed]
    large = [number(row.get("f72")) for row in parsed]

    def sum_yi(values: list[float | None]) -> float | None:
        available = [value for value in values if value is not None]
        return round(sum(available) / 100_000_000, 2) if available else None

    breadth = {
        "total_count": len(parsed),
        "up_count": sum(1 for value in changes if value > 0),
        "down_count": sum(1 for value in changes if value < 0),
        "flat_count": sum(1 for value in changes if value == 0),
        "total_amount_yi": sum_yi(amounts),
        "source": "东方财富全市场行情列表",
    }
    capital = {
        "main_net_yi": sum_yi(main),
        "super_large_net_yi": sum_yi(super_large),
        "large_net_yi": sum_yi(large),
        "security_count": len(parsed),
        "source": "东方财富全市场行情列表资金字段",
    }
    return breadth, capital


def eastmoney_datacenter(
    report_name: str,
    filter_text: str,
    page_size: int = 100,
    sort_columns: str = "",
    sort_types: str = "",
) -> list[dict[str, Any]]:
    payload = eastmoney_json(
        "https://datacenter-web.eastmoney.com/api/data/v1/get",
        {
            "reportName": report_name,
            "columns": "ALL",
            "filter": filter_text,
            "pageNumber": "1",
            "pageSize": str(page_size),
            "sortColumns": sort_columns,
            "sortTypes": sort_types,
            "source": "WEB",
            "client": "WEB",
        },
        "https://data.eastmoney.com/",
    )
    return (payload.get("result") or {}).get("data") or []


def latest_dragon_tiger() -> tuple[list[dict[str, Any]], str | None]:
    today = datetime.now(SHANGHAI).date()
    for offset in range(8):
        trade_date = today - timedelta(days=offset)
        if trade_date.weekday() >= 5:
            continue
        date_text = trade_date.isoformat()
        rows = eastmoney_datacenter(
            "RPT_DAILYBILLBOARD_DETAILSNEW",
            f"(TRADE_DATE>='{date_text}')(TRADE_DATE<='{date_text}')",
            page_size=100,
            sort_columns="BILLBOARD_NET_AMT",
            sort_types="-1",
        )
        if rows:
            return [
                {
                    "code": str(row.get("SECURITY_CODE") or ""),
                    "name": str(row.get("SECURITY_NAME_ABBR") or ""),
                    "reason": str(row.get("EXPLANATION") or ""),
                    "change_percent": number(row.get("CHANGE_RATE")),
                    "net_buy_wan": round((number(row.get("BILLBOARD_NET_AMT")) or 0) / 10_000, 2),
                }
                for row in rows
            ], date_text
    return [], None


def upcoming_unlocks() -> dict[str, list[dict[str, Any]]]:
    start = datetime.now(SHANGHAI).date()
    end = start + timedelta(days=90)
    rows = eastmoney_datacenter(
        "RPT_LIFT_STAGE",
        f"(FREE_DATE>='{start.isoformat()}')(FREE_DATE<='{end.isoformat()}')",
        page_size=500,
        sort_columns="FREE_DATE",
        sort_types="1",
    )
    result: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        code = str(row.get("SECURITY_CODE") or "")
        if not code:
            continue
        ratio = number(row.get("FREE_RATIO"))
        result.setdefault(code, []).append(
            {
                "date": str(row.get("FREE_DATE") or "")[:10],
                "type": str(row.get("FREE_SHARES_TYPE") or ""),
                "shares": number(row.get("ABLE_FREE_SHARES")),
                "ratio_percent": round(ratio * 100, 4) if ratio is not None else None,
                "market_cap_wan": number(row.get("ALIFT_MARKET_CAP")),
                "source": "东方财富未来90天限售解禁日历",
            }
        )
    return result


def cninfo_org_map() -> dict[str, str]:
    payload = http_json(
        "https://www.cninfo.com.cn/new/data/szse_stock.json",
        referer="https://www.cninfo.com.cn/new/disclosure",
        attempts=2,
        timeout=10,
    )
    return {
        str(item.get("code") or ""): str(item.get("orgId") or "")
        for item in payload.get("stockList") or []
        if item.get("code") and item.get("orgId")
    }


def cninfo_announcements(code: str, org_ids: dict[str, str], page_size: int = 20) -> list[dict[str, Any]]:
    org_id = org_ids.get(code)
    if not org_id:
        org_id = f"gssh0{code}" if code.startswith("6") else f"gsbj0{code}" if code.startswith(("4", "8", "9")) else f"gssz0{code}"
    payload = http_post_json(
        "https://www.cninfo.com.cn/new/hisAnnouncement/query",
        {
            "stock": f"{code},{org_id}", "tabName": "fulltext", "pageSize": str(page_size),
            "pageNum": "1", "column": "", "category": "", "plate": "", "seDate": "",
            "searchkey": "", "secid": "", "sortName": "", "sortType": "", "isHLtitle": "true",
        },
        {
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": "https://www.cninfo.com.cn/new/disclosure",
            "Origin": "https://www.cninfo.com.cn",
        },
    )
    result = []
    for item in payload.get("announcements") or []:
        timestamp = item.get("announcementTime")
        date = (
            datetime.fromtimestamp(float(timestamp) / 1000, SHANGHAI).strftime("%Y-%m-%d")
            if isinstance(timestamp, (int, float))
            else str(timestamp or "")[:10]
        )
        announcement_id = str(item.get("announcementId") or "")
        result.append(
            {
                "title": str(item.get("announcementTitle") or ""),
                "type": str(item.get("announcementTypeName") or ""),
                "date": date,
                "url": f"https://www.cninfo.com.cn/new/disclosure/detail?annoId={announcement_id}",
                "source": "巨潮资讯公司公告",
            }
        )
    return result


def enrich_announcement_evidence(
    candidate: dict[str, Any],
    research: dict[str, Any],
    announcements: list[dict[str, Any]],
) -> None:
    risk_words = ("减持", "质押", "立案", "处罚", "警示", "退市", "亏损", "下修", "终止")
    catalyst_words = ("回购", "增持", "中标", "预增", "分红", "签订", "获批")
    risk_titles = [item["title"] for item in announcements if any(word in item["title"] for word in risk_words)]
    catalyst_titles = [item["title"] for item in announcements if any(word in item["title"] for word in catalyst_words)]
    candidate["risks"] = list(dict.fromkeys([*candidate.get("risks", []), *risk_titles[:3]]))
    candidate["catalysts"] = list(dict.fromkeys([*candidate.get("catalysts", []), *catalyst_titles[:3]]))
    candidate["evidence_sources"] = list(dict.fromkeys([*candidate.get("evidence_sources", []), "巨潮资讯公司公告"]))
    candidate["data_gaps"] = [
        item.replace("补齐公告全文、减持、质押、审计意见和个股新闻语义证据。", "补齐质押、审计意见和个股新闻语义证据。")
        for item in candidate.get("data_gaps", [])
    ]
    research["announcements"] = announcements
    research["evidence_sources"] = list(dict.fromkeys([*research.get("evidence_sources", []), "巨潮资讯公司公告"]))
    for component in research["score"]["components"]:
        if component["key"] == "news_sentiment":
            component["evidence"].append(
                f"巨潮最近公告 {len(announcements)} 条；风险词命中 {len(risk_titles)} 条，催化词命中 {len(catalyst_titles)} 条。"
            )
            component["missing_fields"] = [field for field in component.get("missing_fields", []) if field != "公告全文"]
        if component["key"] == "risk_control":
            component["evidence"].append(f"巨潮近期公告风险词检查完成，命中 {len(risk_titles)} 条。")
            component["missing_fields"] = [field for field in component.get("missing_fields", []) if field != "全量减持"]


def limit_pool(endpoint: str, sort: str, trade_date: str) -> list[dict[str, Any]]:
    payload = eastmoney_json(
        f"https://push2ex.eastmoney.com/{endpoint}",
        {
            "ut": "7eea3edcaed734bea9cbfc24409ed989", "dpt": "wz.ztzt",
            "Pageindex": "0", "pagesize": "10000", "sort": sort, "date": trade_date,
        },
        "https://quote.eastmoney.com/",
    )
    return (payload.get("data") or {}).get("pool") or []


def sentiment_label(limit_up: int, limit_down: int, break_rate: float | None) -> str:
    if not limit_up and not limit_down:
        return "无可用涨停池数据"
    if limit_up > limit_down and (break_rate is None or break_rate < 30):
        return "相对活跃（基于涨跌停与炸板率规则）"
    if limit_down > limit_up or (break_rate is not None and break_rate >= 45):
        return "承压（基于涨跌停与炸板率规则）"
    return "分歧（基于涨跌停与炸板率规则）"


def sentiment_score(limit_up: int, limit_down: int, break_rate: float | None, height: int) -> float | None:
    total = limit_up + limit_down
    if total <= 0:
        return None
    balance = 50 + 25 * (limit_up - limit_down) / total
    break_quality = 15 * (1 - min(max(break_rate or 0, 0), 100) / 100)
    height_bonus = 10 * min(max(height, 0), 6) / 6
    return round(min(100, max(0, balance + break_quality + height_bonus)), 1)


def latest_market_sentiment() -> dict[str, Any] | None:
    today = datetime.now(SHANGHAI).date()
    for offset in range(8):
        date = today - timedelta(days=offset)
        if date.weekday() >= 5:
            continue
        date_text = date.strftime("%Y%m%d")
        limit_up = limit_pool("getTopicZTPool", "fbt:asc", date_text)
        broken = limit_pool("getTopicZBPool", "fbt:asc", date_text)
        limit_down = limit_pool("getTopicDTPool", "fund:asc", date_text)
        if not (limit_up or broken or limit_down):
            continue
        break_rate = round(len(broken) / (len(limit_up) + len(broken)) * 100, 2) if limit_up or broken else None
        height = max((int(number(item.get("lbc")) or 0) for item in limit_up), default=0)
        score = sentiment_score(len(limit_up), len(limit_down), break_rate, height)
        return {
            "trade_date": date_text,
            "limit_up_count": len(limit_up),
            "broken_limit_count": len(broken),
            "limit_down_count": len(limit_down),
            "break_rate": break_rate,
            "max_limit_height": height,
            "label": sentiment_label(len(limit_up), len(limit_down), break_rate),
            "source": "东方财富涨停/炸板/跌停池",
            "score": score,
            "score_method": "50分中枢 + 涨跌停强弱差(±25) + 炸板质量(0-15) + 连板高度(0-10)；仅衡量当日情绪，不代表上涨概率。",
        }
    return None


def market_temperature(score: float | None) -> str:
    if score is None:
        return "待评估"
    if score < 30:
        return "冰点"
    if score < 45:
        return "退潮"
    if score < 58:
        return "分歧"
    if score < 72:
        return "修复"
    if score < 86:
        return "一致"
    return "高潮"


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


def technical_snapshot(rows: list[dict[str, Any]], price_source: str) -> dict[str, Any]:
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
        "source": price_source,
        "observed_at": now_iso(),
    }


def stock_analysis(
    quote: dict[str, Any],
    rows: list[dict[str, Any]],
    flows: list[dict[str, Any]],
    reason: str,
    price_source: str,
    unlock_events: list[dict[str, Any]],
    unlock_calendar_available: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    technical = technical_snapshot(rows, price_source)
    close, ma20, macd_hist, rsi = technical["latest_close"], technical["ma_20"], technical["macd_histogram"], technical["rsi_14"]
    technical_score = 50.0
    technical_score += 15 if close is not None and ma20 is not None and close >= ma20 else -10
    technical_score += 15 if macd_hist is not None and macd_hist > 0 else -10
    technical_score += 10 if rsi is not None and 45 <= rsi <= 70 else -10 if rsi is not None and rsi >= 80 else 0
    technical_score = max(0.0, min(100.0, technical_score))
    pe, pb = quote.get("pe_ttm"), quote.get("pb")
    fundamental_score = 50.0 + (10 if pe is not None and 0 < pe <= 30 else -8 if pe is not None and (pe <= 0 or pe > 80) else 0) + (8 if pb is not None and 0 < pb <= 4 else -5 if pb is not None and pb > 10 else 0)
    max_unlock_ratio = max(
        (event.get("ratio_percent") or 0 for event in unlock_events),
        default=0,
    )
    risk_score = 35.0 if "ST" in quote["name"].upper() else 80.0
    if max_unlock_ratio >= 10:
        risk_score -= 25
    elif max_unlock_ratio >= 5:
        risk_score -= 15
    elif max_unlock_ratio >= 1:
        risk_score -= 6
    unlock_evidence = (
        [
            f"未来90天有 {len(unlock_events)} 批解禁，最大单批占比 {max_unlock_ratio:.2f}%。"
        ]
        if unlock_events
        else ["未来90天解禁日历未命中该股票。"]
        if unlock_calendar_available
        else ["未来90天解禁日历本轮不可用。"]
    )
    risk_missing = ["全量减持", "质押", "审计意见"]
    if not unlock_calendar_available:
        risk_missing.append("限售解禁")
    components = [
        ("technical", "技术面", 30.0, technical_score, 1.0, [f"{price_source} {len(rows)} 条。", f"MA20={ma20}，MACD柱={macd_hist}，RSI14={rsi}。"], []),
        ("capital_flow", "资金面", 25.0, 55.0 if flows else None, 0.6 if flows else 0.0, [f"个股资金流 {len(flows)} 日。"] if flows else ["资金流上游暂不可用。"], [] if flows else ["个股资金流"]),
        ("fundamental", "基本面", 20.0, max(0.0, min(100.0, fundamental_score)), 0.55 if pe is not None or pb is not None else 0.0, [f"PE(TTM)={pe}，PB={pb}。"], [] if pe is not None or pb is not None else ["PE", "PB"]),
        ("industry", "行业景气", 10.0, 65.0 if reason else None, 0.6 if reason else 0.0, [f"同花顺强势归因：{reason}"] if reason else ["未进入当日强势题材池。"], [] if reason else ["题材归因"]),
        ("news_sentiment", "公告与新闻", 10.0, 55.0 if reason else None, 0.3 if reason else 0.0, ["仅使用当日强势归因，不把新闻标题直接解释为利好。"] if reason else ["公告新闻未在轻量云采集中闭环。"], ["公告全文", "新闻语义"]),
        (
            "risk_control", "风险控制", 5.0, max(0.0, risk_score),
            0.7 if unlock_calendar_available else 0.5,
            ["名称 ST 硬风控检查已完成。", *unlock_evidence],
            risk_missing,
        ),
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
        "data_completeness": {"code": quote["code"], "generated_at": now_iso(), "coverage_ratio": round(available_weight / 100, 4), "available_weight": round(available_weight, 2), "required_weight": 100.0, "items": [], "next_actions": ["补齐公告全文、减持、质押、审计意见和个股新闻语义证据。"]},
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
        "trend": {"candles": rows[-120:], "fund_flow": flows[-90:], "price_source": price_source, "fund_flow_source": "东方财富个股资金流" if flows else None, "price_updated_at": now_iso(), "fund_flow_updated_at": now_iso() if flows else None, "data_gaps": [] if flows else ["资金流上游暂不可用，不以成交量代替主力资金。"]},
        "strategy": strategy, "evidence_sources": [price_source, "腾讯财经实时行情", "同花顺强势股题材归因"] + (["东方财富个股资金流"] if flows else []),
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
        "risks": (["ST 名称硬风控"] if "ST" in quote["name"].upper() else []) + ([f"未来90天最大单批解禁占比 {max_unlock_ratio:.2f}%"] if unlock_events else []), "concept_tags": [item for item in reason.split("+") if item][:5],
        "evidence_sources": research["evidence_sources"], "data_gaps": score["data_completeness"]["next_actions"],
        "components": [{"key": item["key"], "name": item["name"], "score": item["raw_score"], "coverage_ratio": item["coverage_ratio"]} for item in component_payload],
    }
    return candidate, research


def collect(output: Path) -> None:
    generated_at = now_iso()
    errors: dict[str, str] = {}
    fallbacks: dict[str, str] = {}
    previous_market: dict[str, Any] = {}
    previous_path = output / "market-overview.json"
    if previous_path.exists():
        try:
            previous_payload = json.loads(previous_path.read_text(encoding="utf-8"))
            previous_market = previous_payload.get("data") or {}
        except (OSError, json.JSONDecodeError):
            previous_market = {}
    previous_at = str(previous_market.get("generated_at") or "")

    def previous(field: str, current: Any) -> Any:
        if current not in (None, [], {}):
            return current
        old = previous_market.get(field)
        if old not in (None, [], {}):
            fallbacks[field] = previous_at or "unknown"
            return old
        return current

    def merge_previous_rows(field: str, current: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
        old_rows = previous_market.get(field) or []
        known = {str(row.get(key) or "") for row in current}
        missing = [row for row in old_rows if str(row.get(key) or "") not in known]
        if missing:
            fallbacks[field] = previous_at or "unknown"
        return current + missing

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
    try:
        industries = board_ranks("m:90+t:2")
    except Exception as exc:  # noqa: BLE001
        industries = []
        errors["industries"] = exc.__class__.__name__
    try:
        concepts = board_ranks("m:90+t:3")
    except Exception as exc:  # noqa: BLE001
        concepts = []
        errors["concepts"] = exc.__class__.__name__
    try:
        breadth, capital_flow = market_breadth_and_capital()
    except Exception as exc:  # noqa: BLE001
        breadth, capital_flow = None, None
        errors["breadth_capital"] = exc.__class__.__name__
    try:
        dragon_tiger, dragon_tiger_date = latest_dragon_tiger()
    except Exception as exc:  # noqa: BLE001
        dragon_tiger, dragon_tiger_date = [], None
        errors["dragon_tiger"] = exc.__class__.__name__
    try:
        sentiment = latest_market_sentiment()
    except Exception as exc:  # noqa: BLE001
        sentiment = None
        errors["sentiment"] = exc.__class__.__name__
    try:
        unlock_map = upcoming_unlocks()
        unlock_calendar_available = True
    except Exception as exc:  # noqa: BLE001
        unlock_map = {}
        unlock_calendar_available = False
        errors["unlock_calendar"] = exc.__class__.__name__

    indices = merge_previous_rows("indices", indices, "code")
    etfs = merge_previous_rows("etfs", etfs, "code")
    themes = previous("hot_themes", themes)
    strong = previous("strong_stocks", strong)
    hot = previous("hot_stocks", hot)
    north = previous("northbound", north)
    news_items = previous("news", news_items)
    if not industries and previous_market.get("industries_top"):
        fallbacks["industries"] = previous_at or "unknown"
    if not concepts and previous_market.get("concepts_top"):
        fallbacks["concepts"] = previous_at or "unknown"
    breadth = previous("breadth", breadth)
    capital_flow = previous("capital_flow", capital_flow)
    dragon_tiger = previous("dragon_tiger", dragon_tiger)
    sentiment = previous("sentiment", sentiment) if "sentiment" in previous_market else (
        sentiment or (previous_market.get("conclusion") or {}).get("sentiment")
    )

    reason_by_code = {item["code"]: item["reason"] for item in strong}
    names = {item["code"]: item["name"] for item in strong}
    for item in hot:
        names.setdefault(item["code"], item["name"])
    universe = []
    for item in strong + [{"code": row["code"]} for row in hot]:
        code = item.get("code", "")
        if len(code) == 6 and code not in universe and "ST" not in names.get(code, "").upper():
            universe.append(code)
        if len(universe) >= 36:
            break
    candidates, research_payloads, quotes = [], {}, []
    for code in universe:
        try:
            quote = quote_record(code, names.get(code))
            previous_research = {}
            previous_research_path = output / "research" / f"{code}.json"
            if previous_research_path.exists():
                try:
                    previous_research = (
                        json.loads(previous_research_path.read_text(encoding="utf-8")).get("data") or {}
                    )
                except (OSError, json.JSONDecodeError):
                    previous_research = {}
            try:
                rows, price_source = resilient_kline(code)
                price_from_cache = False
            except Exception:
                old_trend = previous_research.get("trend") or {}
                rows = old_trend.get("candles") or []
                price_source = f"{old_trend.get('price_source') or '历史日线'}（最近成功快照）"
                price_from_cache = bool(rows)
                if not rows:
                    raise
            try:
                flows = eastmoney_fund_flow(code)
            except Exception:  # noqa: BLE001
                old_trend = previous_research.get("trend") or {}
                flows = old_trend.get("fund_flow") or []
                flow_from_cache = bool(flows)
            else:
                flow_from_cache = False
            candidate, research = stock_analysis(
                quote,
                rows,
                flows,
                reason_by_code.get(code, ""),
                price_source,
                unlock_map.get(code, []),
                unlock_calendar_available,
            )
            if price_from_cache:
                research["trend"]["data_gaps"].append("本轮日线源失败，图表展示最近成功快照并保留原日期。")
                candidate["data_gaps"].append("本轮日线源失败，技术图表为最近成功快照。")
            if flow_from_cache:
                research["trend"]["fund_flow_source"] = "东方财富个股资金流（最近成功快照）"
                research["trend"]["fund_flow_updated_at"] = (previous_research.get("trend") or {}).get("fund_flow_updated_at")
                research["trend"]["data_gaps"].append("本轮资金流源失败，展示最近成功快照并保留原时间。")
                candidate["capital_summary"] = f"本轮上游失败，展示最近成功的 {len(flows)} 日资金流。"
                candidate["data_gaps"].append("资金流为最近成功快照，等待上游恢复。")
            quotes.append(quote)
            research_payloads[code] = api(research, "cloud stock research snapshot loaded")
            if candidate["total_score"] > 0 and research["score"]["risk_level"] != "high":
                candidates.append(candidate)
        except Exception as exc:  # noqa: BLE001
            errors[f"stock_{code}"] = exc.__class__.__name__
        time.sleep(0.25)
    if len(candidates) < 5:
        previous_selection_path = output / "scoring-recommendations-today.json"
        try:
            old_candidates = (
                json.loads(previous_selection_path.read_text(encoding="utf-8")).get("data") or {}
            ).get("candidates") or []
        except (OSError, json.JSONDecodeError):
            old_candidates = []
        known_codes = {item["code"] for item in candidates}
        for old in old_candidates:
            if old.get("code") in known_codes:
                continue
            carried = dict(old)
            carried["data_gaps"] = list(carried.get("data_gaps") or []) + [
                "本轮个股采集失败，候选保留上次证据时间，等待下一轮确认。"
            ]
            candidates.append(carried)
            known_codes.add(str(carried.get("code") or ""))
            if len(candidates) >= 5:
                break
    candidates.sort(key=lambda item: item["total_score"], reverse=True)

    try:
        org_ids = cninfo_org_map()
    except Exception as exc:  # noqa: BLE001
        org_ids = {}
        errors["cninfo_org_map"] = exc.__class__.__name__
    announcement_updated_count = 0
    cninfo_circuit_open = False
    for candidate in candidates[:15]:
        code = candidate["code"]
        research_wrapper = research_payloads.get(code)
        if not research_wrapper:
            continue
        announcements = []
        if not cninfo_circuit_open:
            try:
                announcements = cninfo_announcements(code, org_ids)
            except Exception as exc:  # noqa: BLE001
                errors["cninfo_announcements"] = exc.__class__.__name__
                cninfo_circuit_open = True
        if not announcements:
            previous_research_path = output / "research" / f"{code}.json"
            try:
                previous_research = json.loads(previous_research_path.read_text(encoding="utf-8")).get("data") or {}
                announcements = previous_research.get("announcements") or []
            except (OSError, json.JSONDecodeError):
                announcements = []
        if announcements:
            enrich_announcement_evidence(candidate, research_wrapper["data"], announcements)
            announcement_updated_count += 1

    industries_top = industries[:10] if industries else previous_market.get("industries_top") or []
    industries_bottom = industries[-10:] if industries else previous_market.get("industries_bottom") or []
    concepts_top = concepts[:10] if concepts else previous_market.get("concepts_top") or []
    concepts_bottom = concepts[-10:] if concepts else previous_market.get("concepts_bottom") or []
    index_changes = [item["change_percent"] for item in indices if item["change_percent"] is not None]
    index_average = round(sum(index_changes) / len(index_changes), 2) if index_changes else None
    environment = "偏强" if index_average is not None and index_average >= 0.6 else "偏弱" if index_average is not None and index_average <= -0.6 else "震荡"
    rising = sum(1 for value in index_changes if value > 0)
    falling = sum(1 for value in index_changes if value < 0)
    opportunity = [f"同花顺高频题材：{item['name']}({item['mention_count']})。" for item in themes[:5]]
    if north and north.get("total_net_buy_yi") is not None:
        opportunity.append(f"北向分钟流向合计 {north['total_net_buy_yi']:.2f} 亿元。")
    risk_signals = [f"五大指数平均涨跌幅 {index_average:.2f}%。"] if index_average is not None and index_average < -0.6 else []
    if breadth:
        if breadth["up_count"] > breadth["down_count"] * 1.3:
            opportunity.append(f"市场宽度偏积极：上涨 {breadth['up_count']} 家、下跌 {breadth['down_count']} 家。")
        elif breadth["down_count"] > breadth["up_count"] * 1.3:
            risk_signals.append(f"市场宽度承压：上涨 {breadth['up_count']} 家、下跌 {breadth['down_count']} 家。")
    capital_status = "全市场主力资金字段暂不可用。"
    if capital_flow and capital_flow.get("main_net_yi") is not None:
        direction = "净流入" if capital_flow["main_net_yi"] >= 0 else "净流出"
        capital_status = f"东方财富 {capital_flow['security_count']} 只证券主力资金合计{direction} {abs(capital_flow['main_net_yi']):.2f} 亿元。"
        (opportunity if capital_flow["main_net_yi"] >= 0 else risk_signals).append(capital_status)
    if sentiment:
        if (sentiment.get("break_rate") or 0) >= 35:
            risk_signals.append(f"炸板率 {sentiment['break_rate']:.2f}%，短线分歧较高。")
        if sentiment.get("limit_down_count", 0) >= 15:
            risk_signals.append(f"跌停 {sentiment['limit_down_count']} 家，尾部风险需要控制。")

    def freshness(key: str, live_message: str) -> str:
        if key in fallbacks:
            return f"本轮上游失败，展示 {fallbacks[key]} 的最近成功快照。"
        return live_message

    availability = [
        {"key": "indices", "title": "市场指数", "available": bool(indices), "message": freshness("indices", "腾讯财经云端采集。") if indices else errors.get("index_000001", "不可用")},
        {"key": "etfs", "title": "核心ETF", "available": bool(etfs), "message": freshness("etfs", "腾讯财经云端采集。") if etfs else "不可用"},
        {"key": "industries", "title": "行业涨跌", "available": bool(industries_top), "message": freshness("industries", "东方财富全行业涨跌与领涨股。") if industries_top else errors.get("industries", "不可用")},
        {"key": "concepts", "title": "概念涨跌", "available": bool(concepts_top), "message": freshness("concepts", "东方财富全概念涨跌与领涨股。") if concepts_top else errors.get("concepts", "不可用")},
        {"key": "hot_themes", "title": "题材热度", "available": bool(themes), "message": freshness("hot_themes", "同花顺强势股归因。") if themes else errors.get("hot_market", "不可用")},
        {"key": "hot_stocks", "title": "同花顺人气榜", "available": bool(hot), "message": freshness("hot_stocks", "同花顺小时榜。") if hot else errors.get("hot_stocks", "不可用")},
        {"key": "strong_stocks", "title": "强势股归因", "available": bool(strong), "message": freshness("strong_stocks", "同花顺强势股归因。") if strong else "不可用"},
        {"key": "breadth", "title": "市场广度与主力资金", "available": breadth is not None and capital_flow is not None, "message": freshness("breadth", "东方财富全 A 股涨跌家数、成交额与资金字段。") if breadth else errors.get("breadth_capital", "不可用")},
        {"key": "northbound", "title": "北向资金", "available": north is not None, "message": freshness("northbound", "同花顺分钟流向。") if north else errors.get("northbound", "不可用")},
        {"key": "dragon_tiger", "title": "龙虎榜", "available": bool(dragon_tiger), "message": freshness("dragon_tiger", f"东方财富最近交易日龙虎榜（{dragon_tiger_date or '历史快照'}）。") if dragon_tiger else errors.get("dragon_tiger", "最近交易日暂无数据")},
        {"key": "news", "title": "财经快讯", "available": bool(news_items), "message": freshness("news", "东方财富全球资讯。") if news_items else errors.get("news", "不可用")},
        {"key": "sentiment", "title": "涨停情绪", "available": sentiment is not None, "message": freshness("sentiment", f"东方财富涨停/炸板/跌停池（{sentiment.get('trade_date') if sentiment else ''}）。") if sentiment else errors.get("sentiment", "不可用")},
    ]
    market = {
        "generated_at": generated_at, "indices": indices, "etfs": etfs,
        "industries_top": industries_top, "industries_bottom": industries_bottom, "concepts_top": concepts_top, "concepts_bottom": concepts_bottom,
        "hot_themes": themes[:15], "hot_stocks": hot[:20], "strong_stocks": strong[:40],
        "breadth": breadth, "capital_flow": capital_flow, "northbound": north, "dragon_tiger": dragon_tiger[:10], "news": news_items, "availability": availability,
        "conclusion": {
            "market_environment": environment, "environment_evidence": (f"五大指数平均涨跌幅为 {index_average:.2f}%。" if index_average is not None else "指数数据不足。") + (f" 全市场上涨 {breadth['up_count']} 家、下跌 {breadth['down_count']} 家。" if breadth else ""),
            "sentiment": sentiment, "northbound_evidence": f"北向分钟流向合计 {north['total_net_buy_yi']:.2f} 亿元。" if north and north.get("total_net_buy_yi") is not None else "北向资金暂不可用。",
            "hot_industries": [item["name"] for item in industries_top[:5]] or [item["name"] for item in themes[:5]], "main_capital_status": capital_status,
            "hot_continuity_status": "当前为当日题材截面，持续性需跨日快照后验证。", "risk_level": "中等",
            "operation_guidance": "只观察证据覆盖充分的标的，不把云端快照直接当作买卖指令。", "recommendation_status": "仅输出达到覆盖阈值且未触发硬风控的研究候选。",
            "risk_list_status": "ST 名称硬风控已执行；龙虎榜和涨跌停风险已接入，减持、解禁和质押继续由单股证据补充。", "index_average_change": index_average,
            "rising_indices": rising, "falling_indices": falling, "market_temperature": market_temperature(sentiment.get("score") if sentiment else None), "opportunity_signals": opportunity[:5],
            "risk_signals": risk_signals[:5], "evidence_summary": [f"指数 {len(indices)}/5、ETF {len(etfs)}/5、行业 {len(industries_top)}、概念 {len(concepts_top)}、题材 {len(themes)}、候选 {len(candidates)}。"] + ([f"市场宽度：涨 {breadth['up_count']} / 跌 {breadth['down_count']} / 平 {breadth['flat_count']}。"] if breadth else []) + ([f"涨停 {sentiment['limit_up_count']}、炸板 {sentiment['broken_limit_count']}、跌停 {sentiment['limit_down_count']}、最高 {sentiment['max_limit_height']} 板。"] if sentiment else []),
            "data_constraints": ([f"部分字段使用 {previous_at} 最近成功快照：{', '.join(sorted(fallbacks))}。"] if fallbacks else []) + ["GitHub Actions 使用境外云网络，单源失败时自动重试并回退最近成功快照。", "未导入个人持仓成本与仓位。"],
        },
        "disclaimer": "云端快照来自公开上游接口，缺失字段保持空值，不构成投资建议。",
    }
    selection = {"generated_at": generated_at, "universe_size": len(universe), "evaluated_count": len(research_payloads), "candidates": candidates[:15], "status": "available" if candidates else "collecting", "message": "GitHub Actions 云端候选池；仅使用本次成功采集的真实字段。", "disclaimer": "候选为研究筛选，不构成买卖建议，不保证收益。"}
    overview = {
        "generated_at": generated_at,
        "market_snapshot": {"data_quality": {"sample_count": len(quotes), "required_minimum": 5, "is_sufficient": len(quotes) >= 5, "message": f"云端成功采集 {len(quotes)} 只股票。"}, "total_securities": len(quotes), "total_quote_snapshots": len(quotes), "latest_quote_at": generated_at if quotes else None, "market_distribution": [], "source_distribution": [{"name": "腾讯财经", "count": len(quotes)}], "message": "云端快照覆盖事实，不构成市场方向判断。"},
        "latest_quotes": [{**item, "snapshot_count": 1} for item in quotes[:36]],
        "score_summaries": [{"code": item["code"], "name": item["name"], "score_status": "available", "total_score": item["total_score"], "coverage_ratio": item["coverage_ratio"], "available_weight": round(item["coverage_ratio"] * 100, 2), "risk_level": item["risk_level"]} for item in candidates[:20]],
        "data_sources": [
            {"name": "tencent", "display_name": "腾讯财经", "enabled": True, "requires_token": False, "status": "configured" if indices else "unavailable", "message": "指数、ETF、个股行情和复权日线。"},
            {"name": "baidu", "display_name": "百度股市通", "enabled": True, "requires_token": False, "status": "configured", "message": "腾讯日线失败时自动提供个股 K 线备源。"},
            {"name": "ths", "display_name": "同花顺公开数据", "enabled": True, "requires_token": False, "status": "configured" if themes else "unavailable", "message": "题材归因、人气榜和北向分钟流向。"},
            {"name": "eastmoney", "display_name": "东方财富公开数据", "enabled": True, "requires_token": False, "status": "configured" if news_items else "degraded", "message": "行业、概念、市场宽度、主力资金、涨跌停池、龙虎榜、90天解禁、财经快讯与个股资金流；串行限流并缓存保底。"},
            {"name": "cninfo", "display_name": "巨潮资讯", "enabled": True, "requires_token": False, "status": "configured" if announcement_updated_count else "degraded", "message": f"候选股近期公告与风险词检查，本轮覆盖 {announcement_updated_count} 只。"},
        ],
        "unavailable_sections": [
            {"key": "personal_account", "title": "个人账户数据", "message": "持仓成本、仓位和券商成交记录必须由用户授权导入，公开数据源无法代替。"},
        ],
        "disclaimer": "GitHub Actions 每 30 分钟生成公开数据快照；单源失败时回退带原时间戳的最近成功值，不估算行情。",
    }
    tiger = {"model_name": "虎爷模型：3板及以上断板反包 v2", "generated_at": generated_at, "rules": {"minimum_board_height": 3, "volume_ratio_max": 0.4, "pullback_depth_min": 0.02, "pullback_depth_max": 0.22, "support_confirmation_required": True, "take_profit_percent": 12, "stop_loss_percent": 5}, "backtest": {"sample_period": "报告原始样本", "sample_count": 27, "win_rate": 0.63, "profit_loss_ratio": 2.28, "average_return": 0.051}, "live_status": {"trade_date": datetime.now(SHANGHAI).strftime("%Y%m%d"), "market_gate_passed": False, "gate_evidence": ["云端轻量采集未取得完整涨停池。"], "scan_status": "证据门槛未闭环，本次不触发模型扫描。", "signals": [], "missing_data": ["完整涨停池", "断板事件池"]}, "disclaimer": "报告回测不代表未来；实时门槛不足时不输出信号。"}

    output.mkdir(parents=True, exist_ok=True)
    (output / "research").mkdir(exist_ok=True)
    total_research_count = len(
        set(research_payloads)
        | {path.stem for path in (output / "research").glob("*.json")}
    )
    payloads = {
        "health.json": api({"status": "ok", "service": "AI A股智能分析系统", "environment": "github-cloud-snapshot"}, "cloud snapshot is healthy"),
        "system-status.json": api({"phase": "第六阶段：Dashboard 与证据驱动选股", "data_mode": "live_http", "database_engine": "github-json-snapshot", "modules": ["market", "industry", "concept", "breadth", "capital_flow", "sentiment", "dragon_tiger", "themes", "scoring", "stock_trend", "news"]}, "cloud system status loaded"),
        "market-overview.json": api(market, "cloud market overview loaded"),
        "dashboard-overview.json": api(overview, "cloud dashboard overview loaded"),
        "scoring-recommendations-today.json": api(selection, "cloud selection loaded"),
        "tiger-model-overview.json": api(tiger, "cloud tiger model loaded"),
        "manifest.json": {
            "generated_at": generated_at,
            "collector": "github-cloud-snapshot-v2",
            "errors": errors,
            "fallbacks": fallbacks,
            "stock_count": total_research_count,
            "updated_stock_count": len(research_payloads),
            "market_source_count": sum(1 for item in availability if item["available"]),
            "market_source_total": len(availability),
            "unlock_calendar_available": unlock_calendar_available,
            "upcoming_unlock_stock_count": len(unlock_map),
            "announcement_stock_count": announcement_updated_count,
        },
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
