#!/usr/bin/env python3
"""매일 자정(KST) 실행 — 서울/경기 '민간임대' 분양 시작 기사를 모아 텔레그램으로 보낸다.

사용 소스
  1) 네이버 뉴스 검색 API  (NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 있을 때, 원문 링크 제공)
  2) 구글 뉴스 RSS         (키 없이 항상 동작, 보조 소스)

실행 예)
  python3 scripts/collect_rental_news.py            # 실제 발송
  python3 scripts/collect_rental_news.py --dry-run  # 발송 없이 결과만 출력
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime

from rental_common import (
    KST,
    already_sent,
    escape_html,
    http_get,
    load_state,
    log,
    mark_sent,
    match_rental_launch,
    save_state,
    send_telegram,
)

# 검색어 — 소스별로 그대로 사용한다.
QUERIES = [
    "민간임대 분양",
    "공공지원 민간임대 입주자 모집",
    "민간임대 아파트 청약",
    "민간임대 임차인 모집공고",
]

DEFAULT_WINDOW_HOURS = 26  # 크론 지연/중복 대비 여유분 (중복은 상태 파일이 걸러낸다)


def strip_tags(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text or "")).strip()


def normalize_key(link: str, title: str) -> str:
    """같은 기사가 소스마다 다른 URL로 오므로 제목 기준으로도 중복을 잡는다."""
    clean_title = re.sub(r"[^0-9A-Za-z가-힣]", "", title)[:60]
    return clean_title or link


# ---------------------------------------------------------------------------
# 소스 1: 네이버 뉴스 검색 API
# ---------------------------------------------------------------------------


def fetch_naver(query: str) -> list[dict]:
    client_id = os.environ.get("NAVER_CLIENT_ID", "").strip()
    client_secret = os.environ.get("NAVER_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        return []

    url = (
        "https://openapi.naver.com/v1/search/news.json?"
        + urllib.parse.urlencode({"query": query, "display": 100, "sort": "date"})
    )
    try:
        raw = http_get(
            url,
            headers={
                "X-Naver-Client-Id": client_id,
                "X-Naver-Client-Secret": client_secret,
            },
        )
    except RuntimeError as exc:
        log(f"네이버 API 실패({query}): {exc}")
        return []

    items = json.loads(raw.decode("utf-8")).get("items", [])
    results = []
    for item in items:
        try:
            published = parsedate_to_datetime(item["pubDate"]).astimezone(KST)
        except (KeyError, ValueError, TypeError):
            continue
        results.append(
            {
                "title": strip_tags(item.get("title", "")),
                "summary": strip_tags(item.get("description", "")),
                "link": item.get("originallink") or item.get("link", ""),
                "published": published,
                "source": "네이버뉴스",
            }
        )
    return results


# ---------------------------------------------------------------------------
# 소스 2: 구글 뉴스 RSS
# ---------------------------------------------------------------------------


def fetch_google_news(query: str) -> list[dict]:
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": f"{query} when:2d", "hl": "ko", "gl": "KR", "ceid": "KR:ko"}
    )
    try:
        raw = http_get(url)
        root = ET.fromstring(raw)
    except (RuntimeError, ET.ParseError) as exc:
        log(f"구글 뉴스 RSS 실패({query}): {exc}")
        return []

    results = []
    for item in root.iterfind(".//item"):
        title = strip_tags(item.findtext("title", ""))
        link = (item.findtext("link", "") or "").strip()
        pub = item.findtext("pubDate", "")
        source = strip_tags(item.findtext("source", "")) or "구글뉴스"
        try:
            published = parsedate_to_datetime(pub).astimezone(KST)
        except (ValueError, TypeError):
            continue

        # 구글 뉴스 제목은 "제목 - 언론사" 형태라 언론사명을 분리한다.
        if title.endswith(f" - {source}"):
            title = title[: -len(f" - {source}")]

        results.append(
            {
                "title": title,
                "summary": strip_tags(item.findtext("description", "")),
                "link": link,
                "published": published,
                "source": source,
            }
        )
    return results


# ---------------------------------------------------------------------------
# 수집 → 필터 → 발송
# ---------------------------------------------------------------------------


def collect(window_hours: int) -> list[dict]:
    now = datetime.now(KST)
    since = now - timedelta(hours=window_hours)

    raw_items: list[dict] = []
    for query in QUERIES:
        raw_items.extend(fetch_naver(query))
        raw_items.extend(fetch_google_news(query))

    log(f"원본 수집: {len(raw_items)}건")

    matched: dict[str, dict] = {}
    for item in raw_items:
        if not item["link"] or not item["title"]:
            continue
        if item["published"] < since:
            continue

        blob = f"{item['title']} {item['summary']}"
        verdict = match_rental_launch(blob)
        if verdict is None:
            continue

        key = normalize_key(item["link"], item["title"])
        item = {**item, "region": verdict["region"], "key": key}

        # 같은 기사면 네이버(원문 링크)를 우선한다.
        existing = matched.get(key)
        if existing is None or (
            existing["source"] != "네이버뉴스" and item["source"] == "네이버뉴스"
        ):
            matched[key] = item

    results = sorted(matched.values(), key=lambda x: x["published"], reverse=True)
    log(f"조건 통과(민간임대 + 분양시작 + 서울/경기): {len(results)}건")
    return results


def build_message(items: list[dict], window_hours: int) -> str:
    now = datetime.now(KST)
    since = now - timedelta(hours=window_hours)

    header = (
        f"🏢 <b>민간임대 분양 소식</b> (서울·경기)\n"
        f"<i>{since:%m/%d %H:%M} ~ {now:%m/%d %H:%M} KST · {len(items)}건</i>\n"
    )

    lines = [header]
    for idx, item in enumerate(items, start=1):
        lines.append(
            f"\n<b>{idx}. [{item['region']}]</b> {escape_html(item['title'])}\n"
            f"　{escape_html(item['source'])} · {item['published']:%m/%d %H:%M}\n"
            f"　🔗 {escape_html(item['link'])}"
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="발송하지 않고 결과만 출력")
    parser.add_argument(
        "--window-hours",
        type=int,
        default=int(os.environ.get("WINDOW_HOURS", DEFAULT_WINDOW_HOURS)),
        help=f"조회 기간(시간). 기본 {DEFAULT_WINDOW_HOURS}",
    )
    parser.add_argument(
        "--notify-empty",
        action="store_true",
        help="결과가 없어도 '없음' 메시지를 보낸다",
    )
    args = parser.parse_args()

    items = collect(args.window_hours)

    state = load_state()
    fresh = [item for item in items if not already_sent(state, f"news:{item['key']}")]
    log(f"신규(미발송): {len(fresh)}건")

    if args.dry_run:
        for item in fresh:
            print(f"[{item['region']}] {item['title']}\n  {item['link']}\n")
        print(f"총 {len(fresh)}건 (dry-run, 발송 안 함)")
        return 0

    if not fresh:
        if args.notify_empty:
            send_telegram(
                f"🏢 <b>민간임대 분양 소식</b> (서울·경기)\n"
                f"<i>{datetime.now(KST):%m/%d}</i> — 새 소식 없음"
            )
        log("발송할 신규 기사 없음")
        return 0

    send_telegram(build_message(fresh, args.window_hours), disable_preview=True)

    for item in fresh:
        mark_sent(state, f"news:{item['key']}")
    save_state(state)

    log(f"텔레그램 발송 완료: {len(fresh)}건")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
