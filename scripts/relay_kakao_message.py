#!/usr/bin/env python3
"""카카오톡 오픈채팅방에서 중계된 메시지를 필터링해 텔레그램으로 전달한다.

카카오톡은 오픈채팅방 메시지를 읽는 공식 API가 없다. 따라서 안드로이드 알림
리스너 자동화 앱(MacroDroid / Tasker / Automate)이 지정한 방의 알림을 잡아
GitHub `repository_dispatch` 로 쏴주면, 이 스크립트가 그것을 받아 처리한다.
설정 방법은 docs/민간임대-알림-설정.md 참고.

입력: 환경변수 KAKAO_PAYLOAD (JSON)
  {"room": "방 이름", "sender": "보낸 사람", "message": "본문", "timestamp": "..."}
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from datetime import datetime

from rental_common import (
    KST,
    LAUNCH_KEYWORDS,
    RENTAL_KEYWORDS,
    already_sent,
    classify_region,
    escape_html,
    load_state,
    log,
    mark_sent,
    save_state,
    send_telegram,
)

URL_PATTERN = re.compile(r"https?://[^\s<>\"']+")


def parse_payload() -> dict:
    raw = os.environ.get("KAKAO_PAYLOAD", "").strip()
    if not raw:
        raise SystemExit("KAKAO_PAYLOAD 환경변수가 비어 있습니다.")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"KAKAO_PAYLOAD JSON 파싱 실패: {exc}") from exc
    if not isinstance(payload, dict):
        raise SystemExit("KAKAO_PAYLOAD 는 JSON 객체여야 합니다.")
    return payload


def room_allowed(room: str) -> bool:
    """감시 대상 방 목록. KAKAO_ROOMS 가 비어 있으면 모든 방을 허용한다."""
    allow_raw = os.environ.get("KAKAO_ROOMS", "").strip()
    if not allow_raw:
        return True
    allowed = [r.strip() for r in allow_raw.split(",") if r.strip()]
    return any(a and a in room for a in allowed)


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


def matches(message: str) -> tuple[dict | None, str]:
    """채팅 메시지는 기사보다 짧으므로 '민간임대' 키워드만 필수로 본다.

    반환값은 (판정결과, 사유). 판정결과가 None 이면 사유가 제외 이유다.
    """
    normalized = re.sub(r"\s+", " ", message)

    rental_hits = [kw for kw in RENTAL_KEYWORDS if kw in normalized]
    if not rental_hits:
        return None, "민간임대 키워드 없음"

    region = classify_region(normalized)
    if region is None and _flag("KAKAO_REQUIRE_REGION"):
        return None, "서울/경기 지역 키워드 없음 (KAKAO_REQUIRE_REGION=true)"

    launch_hits = [kw for kw in LAUNCH_KEYWORDS if kw in normalized]
    if not launch_hits and _flag("KAKAO_REQUIRE_LAUNCH"):
        return None, "분양/모집 키워드 없음 (KAKAO_REQUIRE_LAUNCH=true)"

    return (
        {
            "region": region,
            "launch_hits": launch_hits[:3],
            "links": URL_PATTERN.findall(normalized)[:5],
        },
        "조건 충족",
    )


def build_message(payload: dict, verdict: dict) -> str:
    room = payload.get("room") or "(방 이름 없음)"
    sender = payload.get("sender") or "(보낸이 미상)"
    body = payload.get("message") or ""
    stamp = payload.get("timestamp") or f"{datetime.now(KST):%Y-%m-%d %H:%M}"

    tags = []
    if verdict["region"]:
        tags.append(verdict["region"])
    tags.extend(verdict["launch_hits"])
    tag_line = " · ".join(dict.fromkeys(tags))

    lines = [
        "💬 <b>카카오톡 민간임대 소식</b>",
        f"<i>{escape_html(room)} · {escape_html(sender)} · {escape_html(stamp)}</i>",
    ]
    if tag_line:
        lines.append(f"🏷 {escape_html(tag_line)}")
    lines.append("")
    lines.append(escape_html(body.strip()))

    if verdict["links"]:
        lines.append("")
        lines.extend(f"🔗 {escape_html(url)}" for url in verdict["links"])

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="발송 없이 판정만 출력")
    args = parser.parse_args()

    payload = parse_payload()
    room = str(payload.get("room", ""))
    body = str(payload.get("message", ""))

    if not room_allowed(room):
        log(f"감시 대상 방이 아님 → 무시: {room!r}")
        return 0

    verdict, reason = matches(body)
    if verdict is None:
        log(f"{reason} → 무시")
        return 0

    key = "kakao:" + hashlib.sha256(
        f"{room}|{payload.get('sender', '')}|{body.strip()}".encode()
    ).hexdigest()[:32]

    state = load_state()
    if already_sent(state, key):
        log("이미 발송한 메시지 → 무시")
        return 0

    text = build_message(payload, verdict)

    if args.dry_run:
        print(text)
        return 0

    send_telegram(text, disable_preview=False)
    mark_sent(state, key)
    save_state(state)
    log("텔레그램 발송 완료")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
