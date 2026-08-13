"""민간임대 분양 소식 수집/알림 공통 모듈.

외부 의존성 없이 표준 라이브러리만 사용한다(GitHub Actions에서 pip install 불필요).
"""

from __future__ import annotations

import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# ---------------------------------------------------------------------------
# 키워드 사전
# ---------------------------------------------------------------------------

# 1) '민간임대' 상품군 — 하나라도 걸려야 한다.
RENTAL_KEYWORDS = [
    "민간임대",
    "민간 임대",
    "공공지원민간임대",
    "공공지원 민간임대",
    "장기일반민간임대",
    "민간임대아파트",
    "임대후분양",
    "임대 후 분양",
]

# 2) '분양/모집 시작' 신호 — 하나라도 걸려야 한다.
LAUNCH_KEYWORDS = [
    "분양",
    "청약",
    "입주자 모집",
    "입주자모집",
    "모집공고",
    "임차인 모집",
    "임차인모집",
    "선착순",
    "정당계약",
    "견본주택",
    "모델하우스",
    "홍보관",
    "본격 공급",
    "공급 시작",
    "예약 접수",
    "사전예약",
]

# 3) 서울/경기 지역 신호
SEOUL_KEYWORDS = [
    "서울",
    "강남구", "강동구", "강북구", "강서구", "관악구", "광진구", "구로구", "금천구",
    "노원구", "도봉구", "동대문구", "동작구", "마포구", "서대문구", "서초구",
    "성동구", "성북구", "송파구", "양천구", "영등포구", "용산구", "은평구",
    "종로구", "중랑구",
    "마곡", "위례", "고덕강일", "마곡지구",
]

GYEONGGI_KEYWORDS = [
    "경기",
    "수원", "성남", "고양", "용인", "부천", "안산", "안양", "남양주", "화성",
    "평택", "의정부", "시흥", "파주", "광명", "김포", "군포", "이천", "양주",
    "오산", "구리", "안성", "포천", "의왕", "하남", "여주", "동두천", "과천",
    "양평", "가평", "연천",
    "동탄", "광교", "다산", "운정", "별내", "왕숙", "교산", "삼송", "지축",
    "향동", "덕은", "고촌", "판교", "분당", "일산", "평내", "호평", "옥정",
    "회천", "지제", "고덕국제", "북수원", "봉담", "향남", "진접", "덕정",
    "장현", "은계", "매교", "영통", "기흥", "수지", "처인",
]

# 광주는 '광주광역시'와 충돌 → 별도 처리
GYEONGGI_AMBIGUOUS = ["경기 광주", "경기도 광주", "광주시", "광주 태전", "광주 역동"]

# 4) 수도권이 아님이 명확한 신호 (서울/경기 신호가 전혀 없을 때만 배제에 사용)
NON_CAPITAL_KEYWORDS = [
    "부산", "대구", "인천", "광주광역시", "대전", "울산", "세종",
    "강원", "춘천", "원주", "강릉",
    "충북", "충남", "청주", "천안", "아산", "충주", "당진", "서산", "공주",
    "전북", "전남", "전주", "군산", "익산", "여수", "순천", "목포",
    "경북", "경남", "포항", "구미", "창원", "김해", "양산", "진주", "거제",
    "제주", "서귀포",
]


def _contains_any(text: str, keywords: list[str]) -> list[str]:
    return [kw for kw in keywords if kw in text]


def classify_region(text: str) -> str | None:
    """텍스트에서 서울/경기 여부를 판정한다. 해당 없으면 None."""
    seoul = _contains_any(text, SEOUL_KEYWORDS)
    gyeonggi = _contains_any(text, GYEONGGI_KEYWORDS) + _contains_any(
        text, GYEONGGI_AMBIGUOUS
    )

    if seoul and gyeonggi:
        return "서울/경기"
    if seoul:
        return "서울"
    if gyeonggi:
        return "경기"
    return None


def match_rental_launch(text: str) -> dict | None:
    """민간임대 + 분양시작 + 서울/경기 조건을 모두 만족하면 매칭 정보를 돌려준다."""
    normalized = re.sub(r"\s+", " ", text)

    rental_hits = _contains_any(normalized, RENTAL_KEYWORDS)
    if not rental_hits:
        return None

    launch_hits = _contains_any(normalized, LAUNCH_KEYWORDS)
    if not launch_hits:
        return None

    region = classify_region(normalized)
    if region is None:
        return None

    # 서울/경기 신호가 약한데 지방 지명이 강하게 잡히면 제외
    non_capital = _contains_any(normalized, NON_CAPITAL_KEYWORDS)
    if non_capital and region == "경기" and len(non_capital) > 1:
        # 지방 지명이 2개 이상이고 경기 신호가 지명 하나뿐이면 오탐 가능성이 높다
        gyeonggi_hits = _contains_any(normalized, GYEONGGI_KEYWORDS)
        if len(gyeonggi_hits) <= 1:
            return None

    return {
        "region": region,
        "rental_hits": rental_hits[:3],
        "launch_hits": launch_hits[:3],
    }


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def http_get(url: str, headers: dict | None = None, timeout: int = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    ctx = ssl.create_default_context()
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                return resp.read()
        except Exception as exc:  # noqa: BLE001 - 네트워크 오류는 재시도 대상
            last_error = exc
            time.sleep(2 ** attempt)
    raise RuntimeError(f"GET 실패: {url} ({last_error})")


def http_post_json(url: str, payload: dict, timeout: int = 20) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        method="POST",
    )
    ctx = ssl.create_default_context()
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            last_error = RuntimeError(f"HTTP {exc.code}: {body}")
            if exc.code < 500:
                break
            time.sleep(2 ** attempt)
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            time.sleep(2 ** attempt)
    raise RuntimeError(f"POST 실패: {url} ({last_error})")


# ---------------------------------------------------------------------------
# 텔레그램
# ---------------------------------------------------------------------------

TELEGRAM_LIMIT = 3900  # 4096 여유분


def escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def send_telegram(text: str, *, disable_preview: bool = True) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

    if not token or not chat_id:
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 이 설정되지 않았습니다. "
            "저장소 Settings > Secrets and variables > Actions 에 등록하세요."
        )

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for chunk in split_message(text):
        http_post_json(
            url,
            {
                "chat_id": chat_id,
                "text": chunk,
                "parse_mode": "HTML",
                "disable_web_page_preview": disable_preview,
            },
        )
        time.sleep(0.4)


def split_message(text: str) -> list[str]:
    """텔레그램 길이 제한에 맞춰 줄 단위로 자른다."""
    if len(text) <= TELEGRAM_LIMIT:
        return [text]

    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > TELEGRAM_LIMIT:
            if current:
                chunks.append(current.rstrip())
            current = ""
        current += line + "\n"
    if current.strip():
        chunks.append(current.rstrip())
    return chunks


# ---------------------------------------------------------------------------
# 중복 발송 방지 상태 파일
# ---------------------------------------------------------------------------

STATE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "sent_state.json"
)
STATE_RETENTION_DAYS = 45


def load_state() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as fp:
            state = json.load(fp)
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}

    state.setdefault("sent", {})
    return state


def save_state(state: dict) -> None:
    cutoff = (datetime.now(KST) - timedelta(days=STATE_RETENTION_DAYS)).isoformat()
    state["sent"] = {k: v for k, v in state["sent"].items() if v >= cutoff}
    state["updated_at"] = datetime.now(KST).isoformat()

    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as fp:
        json.dump(state, fp, ensure_ascii=False, indent=2, sort_keys=True)
        fp.write("\n")


def already_sent(state: dict, key: str) -> bool:
    return key in state["sent"]


def mark_sent(state: dict, key: str) -> None:
    state["sent"][key] = datetime.now(KST).isoformat()


def log(message: str) -> None:
    print(f"[{datetime.now(KST):%Y-%m-%d %H:%M:%S}] {message}", file=sys.stderr)
