# -*- coding: utf-8 -*-
"""
국립공원 생태탐방원 잔여 알림 스크립트
=======================================
하는 일:
  1. 선택한 국립공원 생태탐방원의 '이번 달' + '다음 달' 잔여 현황을 확인
  2. 알림 대상 날짜(토요일 및 '연휴 마지막날을 제외한' 휴일) 중 잔여(생활관)가
     0개보다 많은 날짜를 찾음
  3. 지난번 확인 때와 비교해서 "새로 자리가 생긴" 날짜만 텔레그램으로 알림
  4. 확인 결과는 state.json 파일에 저장해서 다음 실행 때 비교 기준으로 사용

파이썬을 잘 몰라도 됩니다 - 이 파일은 수정할 필요 없이 그대로 쓰시면 됩니다.
감시할 공원을 바꾸고 싶으면 아래 PARKS 딕셔너리만 편집하면 됩니다.
"""

import json
import os
import re
from datetime import date, timedelta

import requests
import holidays

# ----------------------------------------------------------------------
# 1) 감시할 공원 목록 (필요하면 이 딕셔너리만 수정하세요)
#    "화면에 표시될 이름": "deptId 코드"
# ----------------------------------------------------------------------
PARKS = {
    "북한산": "B971002",
    "설악산": "B301002",
    "변산반도": "B183001",
    "소백산": "B123002",
    "계룡산": "B163001",
}

# 개발자도구에서 확인한 다음달 조회용 엔드포인트 (POST 방식으로 추정)
URL = "https://reservation.knps.or.kr/eco/searchEcoMonthReservation.do"
STATE_FILE = "state.json"

# 텔레그램 봇 토큰/채팅ID는 코드에 직접 적지 않고, 환경변수(비밀값)로 받습니다.
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

KR_HOLIDAYS = holidays.KR()

# 날짜 블록을 찾는 정규식: "13 월", "25 토" 같은 패턴
DAY_RE = re.compile(r"(\d{1,2})\s*(일|월|화|수|목|금|토)(?!\S)")
# 그 날짜 블록 안에서 "생활관 : 잔여 14 개" 같은 잔여 개수를 찾는 정규식
COUNT_RE = re.compile(r"생활관\s*:\s*잔여\s*\*?\s*(\d+)\s*\*?\s*개")
# 응답에 실제로 어떤 년/월이 표시되고 있는지 확인하기 위한 정규식 (디버그용)
MONTH_HEADER_RE = re.compile(r"(\d{4})년\s*(\d{1,2})월")


def next_year_month(year: int, month: int) -> tuple:
    if month == 12:
        return year + 1, 1
    return year, month + 1


def fetch_month_text(dept_id: str, year: int, month: int) -> str:
    """해당 공원의 특정 연/월 페이지를 가져와서 순수 텍스트로 변환"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "X-Requested-With": "XMLHttpRequest",
    }
    payload = {
        "deptId": dept_id,
        "year": str(year),
        "month": f"{month:02d}",
        "ctgType": "01",
    }
    # POST로 시도 (개발자도구에서 Form Data로 확인된 값들)
    res = requests.post(URL, data=payload, headers=headers, timeout=15)
    res.raise_for_status()

    from bs4 import BeautifulSoup
    soup = BeautifulSoup(res.text, "html.parser")
    return soup.get_text("\n", strip=True)


def parse_availability(text: str) -> dict:
    """
    텍스트에서 '일자 -> 생활관 잔여수' 딕셔너리를 만든다.
    예: {5: 0, 13: 14, 19: 1, ...}
    """
    matches = list(DAY_RE.finditer(text))
    result = {}
    for i, m in enumerate(matches):
        day = int(m.group(1))
        if not (1 <= day <= 31):
            continue
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        segment = text[start:end]
        counts = COUNT_RE.findall(segment)
        if counts:
            result[day] = sum(int(c) for c in counts)
    return result


def detect_rendered_month(text: str):
    m = MONTH_HEADER_RE.search(text)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None


def is_off_day(d: date) -> bool:
    """토/일/공휴일이면 True (그냥 '쉬는 날' 여부만 판단)"""
    return d.weekday() >= 5 or d in KR_HOLIDAYS


def is_alert_day(d: date) -> bool:
    """
    알림 대상 여부 판정.
    규칙: '쉬는 날(토/일/공휴일)' 이면서 '바로 다음날도 쉬는 날'인 경우만 알림.

    이 규칙 하나로 아래가 전부 자동으로 처리됩니다:
      - 토요일: 다음날(일요일)은 항상 쉬는 날 -> 항상 포함
      - 평범한 일요일(다음날 월요일이 근무일): 제외
      - 연휴 중간 날짜(다음날도 연휴): 포함
      - 연휴 마지막 날(다음날이 근무일): 제외 (일요일이 연휴 마지막날인 경우도 포함)
    """
    if not is_off_day(d):
        return False
    return is_off_day(d + timedelta(days=1))


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def send_telegram(message: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[경고] 텔레그램 토큰/챗ID가 설정되지 않아 메시지를 보내지 않습니다.")
        print(message)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    resp = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message}, timeout=15)
    if resp.status_code != 200:
        print(f"[경고] 텔레그램 전송 실패: {resp.status_code} {resp.text}")


def main():
    today = date.today()
    this_ym = (today.year, today.month)
    next_ym = next_year_month(*this_ym)
    target_months = [this_ym, next_ym]

    old_state = load_state()
    new_state = {}
    newly_available = []  # (공원이름, 날짜) 리스트

    for park_name, dept_id in PARKS.items():
        park_state = {}

        for year, month in target_months:
            ym_key = f"{year:04d}-{month:02d}"
            try:
                text = fetch_month_text(dept_id, year, month)
                rendered = detect_rendered_month(text)
                if rendered and rendered != (year, month):
                    # 요청한 달과 실제로 렌더링된 달이 다르면 콘솔에 경고만 남김
                    # (year/month 파라미터가 서버에서 무시되고 있다는 신호)
                    print(
                        f"[알림] {park_name}: {year}-{month:02d} 요청했지만 "
                        f"실제로는 {rendered[0]}-{rendered[1]:02d} 데이터를 받았습니다. "
                        f"(파라미터 반영 안 될 수 있음)"
                    )
                availability = parse_availability(text)
            except Exception as e:
                print(f"[오류] {park_name} {ym_key} 조회 실패: {e}")
                continue

            park_state[ym_key] = availability
            old_availability = old_state.get(dept_id, {}).get(ym_key, {})

            for day, count in availability.items():
                try:
                    d = date(year, month, day)
                except ValueError:
                    continue

                if not is_alert_day(d):
                    continue
                if count <= 0:
                    continue

                old_count = old_availability.get(str(day), old_availability.get(day, 0))
                if old_count in (0, None):
                    newly_available.append((park_name, d, count))

        new_state[dept_id] = park_state

    save_state(new_state)

    if newly_available:
        lines = ["🏕 생태탐방원 빈자리 알림! (토요일 / 연휴 시작~종료 전날)\n"]
        for park_name, d, count in sorted(newly_available, key=lambda x: x[1]):
            weekday_kr = ["월", "화", "수", "목", "금", "토", "일"][d.weekday()]
            lines.append(f"- {park_name} {d.month}월 {d.day}일({weekday_kr}) : 잔여 {count}개")
        lines.append("\n예약 페이지에서 직접 예약해 주세요:")
        lines.append("https://res.knps.or.kr/eco/searchEcoReservation.do")
        send_telegram("\n".join(lines))
        print("알림 전송 완료:")
        print("\n".join(lines))
    else:
        print("새로 생긴 빈자리(토요일/연휴 대상일)가 없습니다.")


if __name__ == "__main__":
    main()
