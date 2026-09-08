#!/usr/bin/env python3
"""KOSPI 페어밸류(적정가치) 데이터 수집기.

KRX 정보데이터시스템에서 코스피 전종목의 시세 / PER / PBR / EPS / BPS / DPS /
배당수익률 / 업종을 받아 data/kospi.json 으로 저장한다.
과거 밸류에이션 분포(월말 스냅샷)는 data/history.json 에 누적하며,
현재 PER/PBR 이 자기 과거 대비 몇 퍼센타일인지 계산해 함께 담는다.

적정가치 계산 자체는 프론트엔드(kospi.html)에서 수행한다.
사용자가 요구수익률·성장률 슬라이더를 움직이면 즉시 재계산돼야 하기 때문에,
여기서는 "원재료"만 정확히 모아준다.

의존성 없음(표준 라이브러리만). GitHub Actions ubuntu-latest 에서 바로 실행 가능.
"""

from __future__ import annotations

import gzip
import io
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from typing import Any

KST = timezone(timedelta(hours=9))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
OUT_PATH = os.path.join(DATA_DIR, "kospi.json")
HISTORY_PATH = os.path.join(DATA_DIR, "history.json")

KRX_URL = "https://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd"
KRX_REFERER = (
    "https://data.krx.co.kr/contents/MDC/MDI/mdiLoader/index.cmd"
    "?menuId=MDC0201020101"
)
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# KRX bld 코드
BLD_PRICE = "dbms/MDC/STAT/standard/MDCSTAT01501"   # [12001] 전종목 시세
BLD_VALUE = "dbms/MDC/STAT/standard/MDCSTAT03501"   # [12021] PER/PBR/배당수익률
BLD_SECTOR = "dbms/MDC/STAT/standard/MDCSTAT03901"  # [12025] 업종분류 현황

# 코스피200 현물 지수 후보 (KRX 화면 개편 대비해 순서대로 시도)
BLD_INDEX_CANDIDATES = [
    ("dbms/MDC/STAT/standard/MDCSTAT00101", {"idxIndMidclssCd": "02"}),
    ("dbms/MDC/STAT/standard/MDCSTAT00201", {"idxIndMidclssCd": "02"}),
]
# 코스피200 선물 시세 후보 (prodId KRDRVFUK2I = 코스피200 선물)
BLD_FUTURES_CANDIDATES = [
    ("dbms/MDC/STAT/standard/MDCSTAT12501",
     {"prodId": "KRDRVFUK2I", "mktTpCd": "T", "rghtTpCd": "T"}),
    ("dbms/MDC/STAT/standard/MDCSTAT12502",
     {"prodId": "KRDRVFUK2I", "mktTpCd": "T", "rghtTpCd": "T"}),
]
NAVER_KPI200 = "https://finance.naver.com/sise/sise_index.naver?code=KPI200"

# 과거 밸류에이션 분포용 월말 스냅샷 최대 보관 개수 (약 4년)
MAX_HISTORY = 48
# history.json 이 비어 있을 때 한 번에 백필할 월 수
BACKFILL_MONTHS = int(os.environ.get("KOSPI_BACKFILL_MONTHS", "36"))
# 이론 베이시스 계산용 무위험수익률 (CD 91일 근사)
RISK_FREE = float(os.environ.get("KOSPI_RISKFREE", "0.03"))
# 베이시스 추이 보관 개수 (30분 주기 기준 약 1년)
MAX_BASIS_POINTS = 3600
BASIS_PATH = os.path.join(DATA_DIR, "basis.json")


# ─── HTTP ────────────────────────────────────────────────────────────────

def _post(url: str, payload: dict[str, str], timeout: int = 30) -> bytes:
    body = urllib.parse.urlencode(payload).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "User-Agent": UA,
            "Referer": KRX_REFERER,
            "Origin": "https://data.krx.co.kr",
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "ko-KR,ko;q=0.9",
            "Accept-Encoding": "gzip",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        },
    )
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
        return raw


def krx(bld: str, retries: int = 3, **params: str) -> list[dict[str, Any]]:
    """KRX getJsonData 호출 후 결과 행 리스트를 돌려준다."""
    payload = {
        "bld": bld,
        "locale": "ko_KR",
        "share": "1",
        "money": "1",
        "csvxls_isNo": "false",
    }
    payload.update(params)

    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            raw = _post(KRX_URL, payload)
            doc = json.loads(raw.decode("utf-8", "replace"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
            last_err = e
            time.sleep(2 * (attempt + 1))
            continue
        for key in ("OutBlock_1", "output", "block1"):
            if isinstance(doc.get(key), list):
                return doc[key]
        # 키를 못 찾으면 리스트인 첫 값을 사용
        for value in doc.values():
            if isinstance(value, list):
                return value
        return []
    raise RuntimeError(f"KRX 호출 실패 ({bld}): {last_err}")


def _get(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=timeout, context=ssl.create_default_context()) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
        charset = resp.headers.get_content_charset() or "euc-kr"
    return raw.decode(charset, "replace")


# ─── 파싱 헬퍼 ────────────────────────────────────────────────────────────

def num(raw: Any) -> float | None:
    """KRX 숫자 문자열('1,234', '-', '', 'N/A')을 float 으로."""
    if raw is None:
        return None
    text = str(raw).strip().replace(",", "").replace("%", "")
    if text in ("", "-", "N/A", "null"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def pos(value: float | None) -> float | None:
    """0 이하이거나 없는 값은 None (PER/PBR 0 은 '해당없음' 표기)."""
    return value if value is not None and value > 0 else None


def is_preferred(code: str, name: str) -> bool:
    """우선주 여부. 코스피 우선주는 종목코드 끝자리가 0이 아니다."""
    return bool(code) and code[-1] != "0" and "우" in name


def is_excluded(name: str) -> bool:
    """스팩·리츠 등 밸류에이션 모델이 무의미한 종목."""
    return "스팩" in name or name.endswith("리츠")


# ─── 영업일 탐색 ──────────────────────────────────────────────────────────

def krx_date(d: date) -> str:
    return d.strftime("%Y%m%d")


def resolve_trade_date(start: date, lookback: int = 10) -> tuple[str, list[dict]]:
    """시세 데이터가 존재하는 가장 가까운 영업일과 그 시세 행을 찾는다."""
    day = start
    for _ in range(lookback):
        if day.weekday() < 5:  # 주말 제외
            rows = krx(BLD_PRICE, mktId="STK", trdDd=krx_date(day))
            if rows and any(num(r.get("TDD_CLSPRC")) for r in rows):
                return krx_date(day), rows
        day -= timedelta(days=1)
    raise RuntimeError(f"{lookback}일 내 코스피 영업일 시세를 찾지 못했습니다.")


def month_end_business_days(months: int, before: date) -> list[date]:
    """최근 `months` 개월의 월말 영업일(주말 보정) 리스트를 과거→현재 순으로."""
    out: list[date] = []
    cursor = before.replace(day=1)
    for _ in range(months):
        cursor = (cursor - timedelta(days=1)).replace(day=1)  # 이전 달 1일
        # 해당 월의 마지막 날
        nxt = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
        last = nxt - timedelta(days=1)
        while last.weekday() >= 5:
            last -= timedelta(days=1)
        out.append(last)
    return sorted(out)


# ─── 과거 밸류에이션 분포 ──────────────────────────────────────────────────

def load_history() -> dict[str, Any]:
    if os.path.exists(HISTORY_PATH):
        try:
            with open(HISTORY_PATH, encoding="utf-8") as f:
                doc = json.load(f)
            if isinstance(doc.get("snapshots"), dict):
                return doc
        except (json.JSONDecodeError, OSError) as e:
            print(f"[warn] history.json 을 읽지 못해 새로 만듭니다: {e}")
    return {"snapshots": {}}


def snapshot_from_rows(rows: list[dict]) -> dict[str, list[float | None]]:
    """PER/PBR 행 → {종목코드: [per, pbr]}"""
    snap: dict[str, list[float | None]] = {}
    for r in rows:
        code = str(r.get("ISU_SRT_CD", "")).strip()
        if not code:
            continue
        per, pbr = pos(num(r.get("PER"))), pos(num(r.get("PBR")))
        if per is None and pbr is None:
            continue
        snap[code] = [
            round(per, 2) if per else None,
            round(pbr, 3) if pbr else None,
        ]
    return snap


def backfill_history(history: dict[str, Any], upto: date) -> None:
    """history 가 비어 있으면 과거 월말 스냅샷을 채운다(최초 1회)."""
    have = set(history["snapshots"])
    targets = [d for d in month_end_business_days(BACKFILL_MONTHS, upto)
               if d.strftime("%Y-%m") not in {k[:7] for k in have}]
    if not targets:
        return
    print(f"[history] 과거 {len(targets)}개월 백필 시작")
    for i, d in enumerate(targets, 1):
        try:
            rows = krx(BLD_VALUE, searchType="1", mktId="STK", trdDd=krx_date(d), retries=2)
        except RuntimeError as e:
            print(f"[history] {d} 실패, 건너뜀: {e}")
            continue
        snap = snapshot_from_rows(rows)
        if snap:
            history["snapshots"][d.isoformat()] = snap
            print(f"[history] {i}/{len(targets)} {d} · {len(snap)}종목")
        time.sleep(1.2)  # KRX 부하 배려


def trim_history(history: dict[str, Any]) -> None:
    keys = sorted(history["snapshots"])
    for old in keys[:-MAX_HISTORY]:
        del history["snapshots"][old]


def percentile_stats(series: list[float], current: float) -> dict[str, float] | None:
    """현재값이 과거 분포에서 몇 퍼센타일인지 + 중앙/최저/최고."""
    values = sorted(v for v in series if v and v > 0)
    if len(values) < 6:
        return None
    below = sum(1 for v in values if v <= current)
    mid = len(values) // 2
    median = values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2
    return {
        "pct": round(below / len(values) * 100, 1),
        "med": round(median, 3),
        "min": round(values[0], 3),
        "max": round(values[-1], 3),
        "n": len(values),
    }


# ─── 베이시스 (콘탱고 / 백워데이션) ────────────────────────────────────────

def second_thursday(year: int, month: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(3 - first.weekday()) % 7 + 7)


def next_expiry(after: date) -> date:
    """코스피200 선물 최근월물 만기일(3·6·9·12월 두 번째 목요일) 근사."""
    for year in (after.year, after.year + 1):
        for month in (3, 6, 9, 12):
            exp = second_thursday(year, month)
            if exp >= after:
                return exp
    return second_thursday(after.year + 1, 3)


def fetch_spot(trd: str) -> float | None:
    """코스피200 현물 지수."""
    for bld, extra in BLD_INDEX_CANDIDATES:
        try:
            rows = krx(bld, retries=1, trdDd=trd, **extra)
        except RuntimeError:
            continue
        for r in rows:
            name = str(r.get("IDX_NM") or r.get("IDX_IND_NM") or "").replace(" ", "")
            if name in ("코스피200", "KOSPI200"):
                value = num(r.get("CLSPRC_IDX") or r.get("CLSPRC") or r.get("TDD_CLSPRC"))
                if value:
                    return value
    # KRX 실패 시 네이버 폴백
    try:
        html = _get(NAVER_KPI200)
        m = re.search(r'id="now_value"[^>]*>\s*([\d,.]+)', html)
        if m:
            return num(m.group(1))
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        print(f"[basis] 현물 네이버 폴백 실패: {e}")
    return None


def fetch_futures(trd: str) -> tuple[float, str] | None:
    """코스피200 선물 최근월물 (가격, 종목명)."""
    ym_now = int(trd[:6])
    for bld, extra in BLD_FUTURES_CANDIDATES:
        try:
            rows = krx(bld, retries=1, trdDd=trd, **extra)
        except RuntimeError:
            continue
        best: tuple[int, float, str] | None = None
        for r in rows:
            name = str(r.get("ISU_NM") or r.get("ISU_ABBRV") or "").strip()
            if not name or "-" in name or "스프레드" in name:
                continue  # 스프레드 종목 제외
            price = num(r.get("TDD_CLSPRC") or r.get("CLSPRC"))
            if not price or price <= 0:
                continue
            m = re.search(r"(20\d{2})\s*[./-]?\s*(0[1-9]|1[0-2])", name)
            ym = int(m.group(1) + m.group(2)) if m else ym_now
            if ym < ym_now:
                continue
            if best is None or ym < best[0]:
                best = (ym, price, name)
        if best:
            return best[1], best[2]
    return None


def build_basis(trd: str, div_yield: float | None) -> dict[str, Any] | None:
    """시장 베이시스와 이론 베이시스를 계산한다.

    시장 베이시스 = 선물 − 현물. 음수면 백워데이션(선물 저평가)으로,
    매수차익잔고 청산에 따른 프로그램 매도 압력이 걸리는 국면이다.
    """
    spot = fetch_spot(trd)
    fut = fetch_futures(trd)
    if not spot:
        print("[basis] 코스피200 현물 지수를 얻지 못했습니다.")
        return None

    trade_day = date.fromisoformat(f"{trd[:4]}-{trd[4:6]}-{trd[6:]}")
    expiry = next_expiry(trade_day)
    days = max((expiry - trade_day).days, 0)
    d = (div_yield or 0) / 100
    theo = spot * (RISK_FREE - d) * days / 365

    out: dict[str, Any] = {
        "spot": round(spot, 2),
        "expiry": expiry.isoformat(),
        "daysToExpiry": days,
        "riskFree": RISK_FREE,
        "divYield": round(d * 100, 2),
        "theoBasis": round(theo, 2),
    }
    if fut:
        price, name = fut
        basis = price - spot
        out.update({
            "futures": round(price, 2),
            "contract": name,
            "basis": round(basis, 2),
            "basisPct": round(basis / spot * 100, 3),
            "gap": round(basis - theo, 2),           # 시장 − 이론 (괴리)
            "state": "backwardation" if basis < 0 else "contango",
        })
        print(f"[basis] 현물 {spot:.2f} · 선물 {price:.2f} · 베이시스 {basis:+.2f} ({out['state']})")
    else:
        print(f"[basis] 현물 {spot:.2f} · 선물 데이터 없음")
    return out


def append_basis_history(point: dict[str, Any], stamp: str) -> None:
    """베이시스 추이를 data/basis.json 에 누적한다."""
    hist: dict[str, Any] = {"points": []}
    if os.path.exists(BASIS_PATH):
        try:
            with open(BASIS_PATH, encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded.get("points"), list):
                hist = loaded
        except (json.JSONDecodeError, OSError) as e:
            print(f"[warn] basis.json 을 읽지 못해 새로 만듭니다: {e}")

    row = {
        "t": stamp,
        "spot": point.get("spot"),
        "fut": point.get("futures"),
        "basis": point.get("basis"),
        "theo": point.get("theoBasis"),
    }
    points = hist["points"]
    if points and points[-1].get("t") == stamp:
        points[-1] = row
    else:
        points.append(row)
    hist["points"] = points[-MAX_BASIS_POINTS:]

    os.makedirs(DATA_DIR, exist_ok=True)
    with open(BASIS_PATH, "w", encoding="utf-8") as f:
        json.dump(hist, f, ensure_ascii=False, separators=(",", ":"))


# ─── 메인 ────────────────────────────────────────────────────────────────

def build() -> dict[str, Any]:
    now = datetime.now(KST)
    # 장 시작(09:00) 전에는 당일 데이터가 없으므로 전 영업일부터 탐색
    start = now.date() if now.hour >= 9 else now.date() - timedelta(days=1)

    trd, price_rows = resolve_trade_date(start)
    print(f"[krx] 기준일 {trd} · 시세 {len(price_rows)}건")

    value_rows = krx(BLD_VALUE, searchType="1", mktId="STK", trdDd=trd)
    print(f"[krx] 밸류에이션 {len(value_rows)}건")

    try:
        sector_rows = krx(BLD_SECTOR, mktId="STK", trdDd=trd, retries=2)
    except RuntimeError as e:
        print(f"[warn] 업종 조회 실패, '기타'로 대체합니다: {e}")
        sector_rows = []
    sector_of = {
        str(r.get("ISU_SRT_CD", "")).strip(): str(r.get("IDX_IND_NM", "")).strip() or "기타"
        for r in sector_rows
    }
    print(f"[krx] 업종 {len(sector_of)}건")

    value_of = {str(r.get("ISU_SRT_CD", "")).strip(): r for r in value_rows}

    # 과거 분포
    history = load_history()
    backfill_history(history, date.fromisoformat(f"{trd[:4]}-{trd[4:6]}-{trd[6:]}"))
    month_key = f"{trd[:4]}-{trd[4:6]}"
    if not any(k.startswith(month_key) for k in history["snapshots"]):
        history["snapshots"][f"{trd[:4]}-{trd[4:6]}-{trd[6:]}"] = snapshot_from_rows(value_rows)
        print(f"[history] {month_key} 스냅샷 추가")
    trim_history(history)

    hist_dates = sorted(history["snapshots"])
    per_series: dict[str, list[float]] = {}
    pbr_series: dict[str, list[float]] = {}
    for d in hist_dates:
        for code, (per, pbr) in history["snapshots"][d].items():
            if per:
                per_series.setdefault(code, []).append(per)
            if pbr:
                pbr_series.setdefault(code, []).append(pbr)

    stocks: list[dict[str, Any]] = []
    for row in price_rows:
        code = str(row.get("ISU_SRT_CD", "")).strip()
        name = str(row.get("ISU_ABBRV", "")).strip()
        price = num(row.get("TDD_CLSPRC"))
        if not code or not name or not price:
            continue
        if is_excluded(name):
            continue

        v = value_of.get(code, {})
        eps, bps = num(v.get("EPS")), num(v.get("BPS"))
        per, pbr = pos(num(v.get("PER"))), pos(num(v.get("PBR")))
        dps, dvd = num(v.get("DPS")), num(v.get("DVD_YLD"))

        # PER/PBR 이 비어도 EPS/BPS 가 있으면 직접 계산
        if per is None and eps and eps > 0:
            per = round(price / eps, 2)
        if pbr is None and bps and bps > 0:
            pbr = round(price / bps, 3)

        item: dict[str, Any] = {
            "code": code,
            "name": name,
            "sector": sector_of.get(code, "기타"),
            "price": int(price),
            "chg": num(row.get("FLUC_RT")),
            "cap": int(num(row.get("MKTCAP")) or 0),
            "vol": int(num(row.get("ACC_TRDVAL")) or 0),
            "eps": int(eps) if eps else None,
            "bps": int(bps) if bps else None,
            "per": per,
            "pbr": pbr,
            "dps": int(dps) if dps else None,
            "dvd": dvd,
        }
        if is_preferred(code, name):
            item["pref"] = True

        if pbr and (stats := percentile_stats(pbr_series.get(code, []), pbr)):
            item["pbrHist"] = stats
        if per and (stats := percentile_stats(per_series.get(code, []), per)):
            item["perHist"] = stats

        stocks.append(item)

    stocks.sort(key=lambda s: s["cap"], reverse=True)
    if len(stocks) < 100:
        raise RuntimeError(f"수집 종목이 {len(stocks)}개뿐이라 저장을 중단합니다.")

    # 시장 요약 (시총가중 PER/PBR — 이익/자본 합계 기준)
    total_cap = sum(s["cap"] for s in stocks if not s.get("pref"))
    earnings = sum(s["cap"] / s["per"] for s in stocks if s["per"] and not s.get("pref"))
    equity = sum(s["cap"] / s["pbr"] for s in stocks if s["pbr"] and not s.get("pref"))
    paying = [s for s in stocks if s["dvd"] and not s.get("pref")]
    paying_cap = sum(s["cap"] for s in paying)
    market = {
        "count": len(stocks),
        "cap": total_cap,
        "per": round(total_cap / earnings, 2) if earnings else None,
        "pbr": round(total_cap / equity, 3) if equity else None,
        "dvd": round(sum(s["cap"] * s["dvd"] for s in paying) / paying_cap, 2) if paying_cap else None,
        "advance": sum(1 for s in stocks if (s["chg"] or 0) > 0),
        "decline": sum(1 for s in stocks if (s["chg"] or 0) < 0),
    }

    # 코스피200 선물 베이시스 (콘탱고 / 백워데이션)
    try:
        basis = build_basis(trd, market["dvd"])
    except Exception as e:  # 베이시스 실패가 본 데이터 수집을 막지 않도록
        print(f"[warn] 베이시스 계산 실패: {e}")
        basis = None
    if basis:
        append_basis_history(basis, now.strftime("%Y-%m-%dT%H:%M"))

    save_history(history)
    return {
        "generatedAt": now.isoformat(timespec="seconds"),
        "tradeDate": f"{trd[:4]}-{trd[4:6]}-{trd[6:]}",
        "source": "KRX 정보데이터시스템",
        "delayNote": "장중 시세는 약 20분 지연",
        "historyDates": hist_dates,
        "market": market,
        "basis": basis,
        "stocks": stocks,
    }


def save_history(history: dict[str, Any]) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, separators=(",", ":"))


def main() -> int:
    try:
        payload = build()
    except Exception as e:  # 실패 시 기존 데이터를 덮어쓰지 않는다
        print(f"[error] 수집 실패: {e}", file=sys.stderr)
        return 1

    os.makedirs(DATA_DIR, exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    size_kb = os.path.getsize(OUT_PATH) / 1024
    print(
        f"[done] {payload['tradeDate']} · {len(payload['stocks'])}종목 · "
        f"{size_kb:.0f}KB → data/kospi.json"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
