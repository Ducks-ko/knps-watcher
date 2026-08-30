# -*- coding: utf-8 -*-
"""
국립공원 야영장(카라반/특화야영장) 잔여 알림 스크립트
=======================================================
하는 일:
  1. 지정한 야영장의 예약 현황(campsiteList.do)을 조회
  2. 알림 대상 날짜(토요일 및 '연휴 마지막날을 제외한' 휴일) 중
     예약가능(icon-reservation) 호실이 있는 날짜를 찾음
  3. 지난번 확인 때와 비교해서 "새로 생긴" 자리만 텔레그램으로 알림
  4. 결과는 camp_state.json에 저장해서 다음 실행 때 비교 기준으로 사용

★ 카테고리(카라반/특화야영장)는 prd_ctg_id로 이미 필터링해서 요청합니다.
★ 감시할 야영장을 늘리려면 아래 CAMPSITES 리스트에 항목을 추가하면 됩니다.
"""

import json
import os
import re
from datetime import date, datetime, timedelta

import requests
from bs4 import BeautifulSoup
import holidays

# ----------------------------------------------------------------------
# 1) 감시할 야영장 목록
#    dept_id / dept_name / parent_dept_name / prd_ctg_id 는 개발자도구에서 확인한 값
#    prd_ctg_id: "02032"=카라반, "02021"=특화야영장 (둘 다 원하면 "02032,02021")
# ----------------------------------------------------------------------
CAMPSITES = [
    {"dept_id": "B051006", "dept_name": "덕유대3", "parent_dept_name": "덕유산", "prd_ctg_id": "02032,02021"},
    {"dept_id": "B181004", "dept_name": "고사포2", "parent_dept_name": "변산반도", "prd_ctg_id": "02021"},
    {"dept_id": "B181002", "dept_name": "고사포1", "parent_dept_name": "변산반도", "prd_ctg_id": "02021"},
    {"dept_id": "B031005", "dept_name": "설악동", "parent_dept_name": "설악산", "prd_ctg_id": "02032"},
    {"dept_id": "B061001", "dept_name": "소금강산", "parent_dept_name": "오대산", "prd_ctg_id": "02032,02021"},
    {"dept_id": "B071001", "dept_name": "상의", "parent_dept_name": "주왕산", "prd_ctg_id": "02032,02021"},
    {"dept_id": "B101001", "dept_name": "구룡", "parent_dept_name": "치악산", "prd_ctg_id": "02032,02021"},
    {"dept_id": "B221004", "dept_name": "소도", "parent_dept_name": "태백산", "prd_ctg_id": "02032"},
]

URL = "https://reservation.knps.or.kr/reservation/campsiteList.do"
STATE_FILE = "camp_state.json"

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

HEADERS = {
    "Accept": "text/html, */*; q=0.01",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "Origin": "https://reservation.knps.or.kr",
    "Referer": (
        "https://reservation.knps.or.kr/"
        "reservation/searchSimpleCampReservation.do"
    ),
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/151.0.0.0 Safari/537.36"
    ),
    "X-Requested-With": "XMLHttpRequest",
}

WEEKDAY_KR = ["월", "화", "수", "목", "금", "토", "일"]

today = date.today()
# 올해~내후년까지 넉넉히 잡아서 매년 코드 수정 안 해도 되게 함
KR_HOLIDAYS = holidays.KR(years=range(today.year, today.year + 3))

# 클래스 이름 안에 박힌 날짜 패턴: "20260905_C", "20260907_N", "20260905_R" 등
DATE_CLASS_RE = re.compile(r"^(\d{8})_[A-Z]$")


# ----------------------------------------------------------------------
# 공휴일 판정
# ----------------------------------------------------------------------
def is_off_day(d: date) -> bool:
    return d.weekday() >= 5 or d in KR_HOLIDAYS


def is_alert_day(d: date) -> bool:
    """토요일 및 '연휴 마지막날을 제외한' 휴일만 알림 대상."""
    if not is_off_day(d):
        return False
    return is_off_day(d + timedelta(days=1))


def get_holiday_reason(d: date) -> str:
    if d in KR_HOLIDAYS:
        return f"공휴일: {KR_HOLIDAYS[d]}"
    if d.weekday() == 5:
        return "토요일"
    if d.weekday() == 6:
        return "일요일"
    return "평일"


# ----------------------------------------------------------------------
# 날짜 추출 (★ 수정된 부분 - data-use_df가 없는 경우 클래스명에서 추출)
# ----------------------------------------------------------------------
def extract_date_from_element(item) -> str:
    # 1순위: data-use_df 속성
    use_date = item.get("data-use_df", "")
    if use_date:
        return use_date.strip()

    # 2순위: 클래스 이름 안의 날짜 토큰 (예: "20260905_C" -> "20260905")
    for cls in item.get("class", []):
        m = DATE_CLASS_RE.match(cls)
        if m:
            return m.group(1)

    return ""


def parse_use_date(value: str):
    if not value:
        return None
    value = value.strip()
    for fmt in ("%Y%m%d", "%Y-%m-%d", "%Y.%m.%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    return None


# ----------------------------------------------------------------------
# 야영장 데이터 조회
# ----------------------------------------------------------------------
def fetch_campsite_availability(dept_id: str, dept_name: str, parent_dept_name: str, prd_ctg_id: str):
    """
    반환값: {date_str: [예약가능 호실명, ...]} 형태의 딕셔너리
            NetFunnel 대기열 등으로 정상 응답이 아니면 None 반환 (이번 회차 스킵용)
    """
    payload = {
        "dept_id": dept_id,
        "dept_name": dept_name,
        "parent_dept_name": parent_dept_name,
        "prd_ctg_id": prd_ctg_id,
        "isGreenpoint": "N",
    }

    session = requests.Session()
    response = session.post(URL, headers=HEADERS, data=payload, timeout=30)

    # 비정상 응답(대기열 등)이면 이번 회차는 조용히 건너뜀
    if response.status_code != 200 or len(response.text) < 1000:
        print(
            f"[스킵] {parent_dept_name} {dept_name}: 비정상 응답 "
            f"(status={response.status_code}, size={len(response.text)})"
        )
        return None, None

    soup = BeautifulSoup(response.text, "html.parser")
    items = soup.select("i.icon-reservation, i.icon-none-reservation, i.icon-end")

    if not items:
        print(f"[스킵] {parent_dept_name} {dept_name}: 데이터 항목을 찾지 못함 (구조 변경 가능성)")
        return None, None

    availability = {}  # {"YYYYMMDD": [호실명, ...]}  (예약가능만)
    all_dates_seen = set()  # 상태 무관, 실제로 서버가 응답에 포함시킨 모든 날짜

    for item in items:
        class_list = item.get("class", [])
        use_date = extract_date_from_element(item)
        if use_date:
            all_dates_seen.add(use_date)

        if "icon-reservation" not in class_list:
            continue  # 예약가능만 관심 대상

        if not use_date:
            continue

        title = item.get("title", "")
        room_name = title.split(" : ")[0] if " : " in title else title

        availability.setdefault(use_date, []).append(room_name)

    return availability, all_dates_seen


# ----------------------------------------------------------------------
# 상태 저장/로드
# ----------------------------------------------------------------------
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


# ----------------------------------------------------------------------
# 메인
# ----------------------------------------------------------------------
def main():
    old_state = load_state()
    new_state = {}
    all_ranges = {}  # 진단용: 사이트별 전체 조회 날짜 범위(매진 포함)
    newly_available = []  # (공원, 야영장, 날짜, 호실목록)

    for site in CAMPSITES:
        key = f"{site['parent_dept_name']}-{site['dept_name']}"

        availability, all_dates_seen = fetch_campsite_availability(
            site["dept_id"], site["dept_name"], site["parent_dept_name"], site["prd_ctg_id"]
        )

        if availability is None:
            # 이번 회차 조회 실패 -> 이전 상태를 그대로 유지해서 다음 회차에 재시도
            new_state[key] = old_state.get(key, {})
            continue

        new_state[key] = availability
        old_availability = old_state.get(key, {})

        parsed_all = sorted(d for d in (parse_use_date(ds) for ds in all_dates_seen) if d)
        all_ranges[key] = (parsed_all[0], parsed_all[-1]) if parsed_all else (None, None)

        for date_str, rooms in availability.items():
            d = parse_use_date(date_str)
            if d is None:
                continue
            if not is_alert_day(d):
                continue

            old_rooms = old_availability.get(date_str, [])
            if len(old_rooms) == 0 and len(rooms) > 0:
                newly_available.append(
                    (site["parent_dept_name"], site["dept_name"], d, rooms)
                )

    save_state(new_state)

    # ---------- 진단용 요약 출력 ----------
    print()
    print("=" * 60)
    print("사이트별 데이터 확보 현황")
    print("=" * 60)
    for site in CAMPSITES:
        key = f"{site['parent_dept_name']}-{site['dept_name']}"
        avail = new_state.get(key, {})
        total_dates = len(avail)
        total_rooms = sum(len(v) for v in avail.values())
        alert_dates = [
            ds for ds in avail.keys()
            if parse_use_date(ds) and is_alert_day(parse_use_date(ds)) and len(avail[ds]) > 0
        ]
        parsed_dates = sorted(d for d in (parse_use_date(ds) for ds in avail.keys()) if d)
        date_range = f"{parsed_dates[0]} ~ {parsed_dates[-1]}" if parsed_dates else "없음"
        full_start, full_end = all_ranges.get(key, (None, None))
        full_range = f"{full_start} ~ {full_end}" if full_start else "없음"
        print(
            f"  {key}: 예약가능날짜 {total_dates}개(범위 {date_range}) / "
            f"전체조회범위(매진포함) {full_range} / 총 호실수 {total_rooms} / "
            f"알림대상일 중 가능 {len(alert_dates)}개 -> {alert_dates[:5]}"
        )
    print("=" * 60)
    # ---------- 진단용 요약 출력 끝 ----------

    if newly_available:
        lines = ["🏕 야영장(카라반/특화야영장) 빈자리 알림!\n"]
        for park, camp, d, rooms in sorted(newly_available, key=lambda x: x[2]):
            room_preview = ", ".join(rooms[:3]) + (" 외" if len(rooms) > 3 else "")
            lines.append(
                f"- {park} {camp} | {d.month}월 {d.day}일({WEEKDAY_KR[d.weekday()]}) "
                f"| {get_holiday_reason(d)} | {len(rooms)}개 호실 ({room_preview})"
            )
        lines.append("\n예약 페이지에서 직접 예약해 주세요:")
        lines.append("https://reservation.knps.or.kr/reservation/searchSimpleCampReservation.do")
        send_telegram("\n".join(lines))
        print("알림 전송 완료:")
        print("\n".join(lines))
    else:
        print("새로 생긴 빈자리(토요일/연휴 대상일)가 없습니다.")


if __name__ == "__main__":
    main()
