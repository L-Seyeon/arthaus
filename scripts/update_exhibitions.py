"""
ARTHAUS 전시 정보 자동 업데이트 스크립트
- culture.go.kr 공공 API  → 국립·공립 미술관 (정확한 실제 데이터)
- 웹 크롤링               → 사립 갤러리 보완
- Gemini AI              → 크롤링 데이터 정제
- exhibitions.json 저장
"""

import os
import json
import time
import re
import xml.etree.ElementTree as ET
from datetime import datetime, date, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from google import genai

# ── 경로 설정 ──────────────────────────────────────────────────────────────────

ROOT_DIR = Path(__file__).parent.parent
OUTPUT_FILE = ROOT_DIR / "exhibitions.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9",
}

# ── 그라디언트 색상 (미술관별) ─────────────────────────────────────────────────

GRAD_MAP = {
    "국립현대미술관":  "linear-gradient(135deg,#0a1228,#1a2a50)",
    "서울시립미술관":  "linear-gradient(135deg,#1a3050,#2a5070)",
    "예술의전당":     "linear-gradient(135deg,#3a2800,#5a4000)",
    "국립중앙박물관":  "linear-gradient(135deg,#0d1a2e,#1a3055)",
    "대구미술관":     "linear-gradient(135deg,#2a1a08,#3a2810)",
    "부산현대미술관":  "linear-gradient(135deg,#180808,#2a1010)",
    "리움미술관":     "linear-gradient(135deg,#1a0a28,#2a1040)",
    "갤러리현대":     "linear-gradient(135deg,#2a2620,#3a3228)",
    "환기미술관":     "linear-gradient(135deg,#020810,#081838)",
    "아트선재센터":   "linear-gradient(135deg,#080418,#180a30)",
}

DEFAULT_GRAD = "linear-gradient(135deg,#1a2040,#2a3060)"

def get_grad(venue: str) -> str:
    for key, grad in GRAD_MAP.items():
        if key in venue:
            return grad
    return DEFAULT_GRAD


# ════════════════════════════════════════════════════════════════════
#  1. culture.go.kr 공공 API  (국립·공립 기관 — 실제 데이터)
# ════════════════════════════════════════════════════════════════════

CULTURE_API_URL = "http://www.culture.go.kr/openapi/rest/publicperformancedisplays/period"

def fetch_culture_api(service_key: str) -> list:
    """culture.go.kr 공연전시정보 API 호출 → 전시(D) 항목만 반환"""
    today    = date.today()
    end_date = today + timedelta(days=90)

    params = {
        "serviceKey": service_key,
        "from":       today.strftime("%Y%m%d"),
        "to":         end_date.strftime("%Y%m%d"),
        "cPage":      "1",
        "rows":       "50",
        "sortStdr":   "1",
        "realmCode":  "I",    # I = 전시 (D = 무용)
    }

    print("  culture.go.kr API 호출 중...")
    try:
        resp = requests.get(CULTURE_API_URL, params=params, headers=HEADERS, timeout=20)
        resp.encoding = "utf-8"
    except Exception as e:
        print(f"  ❌ API 요청 실패: {e}")
        return []

    print(f"  HTTP 상태: {resp.status_code}")

    # XML 파싱
    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError as e:
        print(f"  ❌ XML 파싱 실패: {e}")
        print(f"  응답 미리보기: {resp.text[:300]}")
        return []

    # 오류 응답 체크 — 여러 경로 시도
    result_code = (
        root.findtext(".//resultCode")
        or root.findtext(".//result/resultCode")
        or root.findtext("resultCode")
        or "00"
    )
    result_msg = (
        root.findtext(".//resultMsg")
        or root.findtext(".//result/resultMsg")
        or ""
    )

    print(f"  API resultCode: {result_code} / {result_msg}")

    if result_code not in ("00", "0000", "정상", ""):
        print(f"  ❌ API 오류 코드: {result_code}")
        return []

    # item 태그 수집 — 여러 경로 시도
    items = root.findall(".//item")
    if not items:
        items = root.findall("item")
    if not items:
        # 전체 XML 구조 출력 (디버깅용)
        print(f"  ⚠️ item 태그 없음. 루트 태그: {root.tag}")
        print(f"  자식 태그: {[c.tag for c in root][:10]}")
        return []

    print(f"  item 태그 {len(items)}개 발견")

    results = []
    for item in items:
        title      = item.findtext("title", "").strip()
        place      = item.findtext("place", "").strip()
        start_date = item.findtext("startDate", "").strip()
        end_date_  = item.findtext("endDate", "").strip()
        url        = item.findtext("url", "").strip()
        thumbnail  = item.findtext("thumbnail", "").strip()
        contents   = item.findtext("contents", "").strip()

        if not title or not place:
            continue

        def fmt(d):
            return f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 else None

        results.append({
            "title":   title,
            "venue":   place,
            "desc":    (contents[:120] + "…") if len(contents) > 120 else contents,
            "start":   fmt(start_date),
            "end":     fmt(end_date_),
            "url":     url or "https://www.culture.go.kr",
            "img":     thumbnail if thumbnail else None,
            "grad":    get_grad(place),
            "_source": "api",
        })

    print(f"  공공 API → {len(results)}개 전시 수집")
    return results


# ════════════════════════════════════════════════════════════════════
#  2. 사립 미술관·갤러리 웹 크롤링
# ════════════════════════════════════════════════════════════════════

PRIVATE_MUSEUMS = [
    {
        "name": "리움미술관",
        "url":  "https://leeum.org/programs/exhibitions/current",
        "home": "https://leeum.org",
    },
    {
        "name": "갤러리현대",
        "url":  "https://galleryhyundai.com/exhibitions",
        "home": "https://galleryhyundai.com",
    },
    {
        "name": "환기미술관",
        "url":  "http://whankimuseum.org",
        "home": "http://whankimuseum.org",
    },
    {
        "name": "아트선재센터",
        "url":  "https://artsonje.org/exhibition/",
        "home": "https://artsonje.org",
    },
    {
        "name": "페이스갤러리 서울",
        "url":  "https://www.pacegallery.com/exhibitions/?location=seoul",
        "home": "https://www.pacegallery.com",
    },
]

def fetch_text(url: str) -> str:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=12)
        resp.encoding = resp.apparent_encoding or "utf-8"
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header", "iframe"]):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text[:2500]
    except Exception as e:
        return f"[크롤링 실패: {e}]"

def crawl_private_museums() -> str:
    parts = []
    for museum in PRIVATE_MUSEUMS:
        print(f"  크롤링 중: {museum['name']} ...")
        text = fetch_text(museum["url"])
        parts.append(f"=== {museum['name']} (홈: {museum['home']}) ===\n{text}")
        time.sleep(1)
    return "\n\n".join(parts)


# ════════════════════════════════════════════════════════════════════
#  3. Gemini AI — 크롤링 데이터 정제 (재시도 로직 포함)
# ════════════════════════════════════════════════════════════════════

def build_gemini_prompt(raw_text: str) -> str:
    today = date.today().isoformat()
    return f"""다음은 한국 사립 미술관·갤러리 홈페이지에서 수집한 텍스트입니다.
오늘 날짜: {today}

{raw_text}

---
위 텍스트에서 현재 진행 중이거나 1개월 이내 예정된 전시만 추출하세요.
JSON 배열 형식으로만 응답하고, 확인되지 않은 내용은 절대 추가하지 마세요.

[
  {{
    "title": "전시 제목",
    "venue": "미술관 이름",
    "desc": "실제 전시 설명 (2문장 이내)",
    "start": "YYYY-MM-DD 또는 null",
    "end": "YYYY-MM-DD 또는 null",
    "url": "공식 전시 페이지 URL",
    "img": null,
    "grad": "linear-gradient(135deg,#1a3050,#2a5070)"
  }}
]"""


def parse_json_from_response(text: str) -> list:
    text = re.sub(r"```(?:json)?", "", text).strip()
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if not match:
        return []
    try:
        data = json.loads(match.group())
        for item in data:
            if not item.get("grad"):
                item["grad"] = get_grad(item.get("venue", ""))
        return data
    except json.JSONDecodeError:
        return []


def refine_with_gemini(raw_text: str, max_retries: int = 3) -> list:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("  ⚠️ GEMINI_API_KEY 없음 — 크롤링 데이터 건너뜀")
        return []

    client = genai.Client(api_key=api_key)

    for attempt in range(1, max_retries + 1):
        print(f"  Gemini API 호출 중... (시도 {attempt}/{max_retries})")
        try:
            response = client.models.generate_content(
                model="gemini-1.5-flash",
                contents=build_gemini_prompt(raw_text),
            )
            results = parse_json_from_response(response.text)
            print(f"  Gemini → {len(results)}개 사립 전시 추출")
            return results

        except Exception as e:
            err_str = str(e)
            print(f"  ⚠️ Gemini 오류 (시도 {attempt}): {err_str[:120]}")

            # 429 (할당량 초과) 또는 503 → 대기 후 재시도
            if "429" in err_str or "quota" in err_str.lower() or "503" in err_str:
                wait = 30 * attempt  # 30초, 60초, 90초
                if attempt < max_retries:
                    print(f"  ⏳ {wait}초 대기 후 재시도...")
                    time.sleep(wait)
                else:
                    print("  ❌ Gemini 재시도 횟수 초과 — 크롤링 결과 건너뜀")
            else:
                # 다른 오류는 재시도 불필요
                break

    return []


# ════════════════════════════════════════════════════════════════════
#  4. 중복 제거 + 검증
# ════════════════════════════════════════════════════════════════════

REQUIRED_KEYS = {"title", "venue", "url"}

def validate(items: list) -> list:
    valid = []
    seen  = set()
    for item in items:
        if not REQUIRED_KEYS.issubset(item.keys()):
            continue
        if not item["title"] or not item["venue"]:
            continue
        key = item["title"].strip()
        if key in seen:
            continue
        seen.add(key)
        item.setdefault("img",   None)
        item.setdefault("grad",  get_grad(item.get("venue", "")))
        item.setdefault("start", None)
        item.setdefault("end",   None)
        item.setdefault("desc",  "")
        item.pop("_source", None)
        valid.append(item)
    return valid


# ════════════════════════════════════════════════════════════════════
#  메인
# ════════════════════════════════════════════════════════════════════

def main():
    print(f"\n{'='*52}")
    print(f"ARTHAUS 전시 정보 업데이트: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*52}\n")

    all_exhibitions = []

    # ── 1. 공공 API (국립·공립) ──────────────────────────────────────
    print("[1/3] culture.go.kr 공공 API (국립·공립 미술관)...")
    culture_key = os.environ.get("CULTURE_API_KEY")
    if culture_key:
        public = fetch_culture_api(culture_key)
        all_exhibitions.extend(public)
    else:
        print("  ⚠️ CULTURE_API_KEY 없음 — 공공 API 건너뜀")
    print()

    # ── 2. 사립 갤러리 크롤링 + Gemini 정제 ─────────────────────────
    print("[2/3] 사립 미술관·갤러리 크롤링...")
    raw_text = crawl_private_museums()
    private  = refine_with_gemini(raw_text)
    all_exhibitions.extend(private)
    print()

    # ── 3. 저장 ──────────────────────────────────────────────────────
    print("[3/3] exhibitions.json 저장...")
    exhibitions = validate(all_exhibitions)

    print(f"  수집된 전시 총 {len(exhibitions)}개")

    if len(exhibitions) < 3:
        print("  ❌ 전시 데이터 부족 (3개 미만). 기존 파일 유지.")
        # 기존 파일이 있으면 내용 출력
        if OUTPUT_FILE.exists():
            with open(OUTPUT_FILE, encoding="utf-8") as f:
                existing = json.load(f)
            print(f"  (기존 파일: {len(existing)}개 유지)")
        return

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(exhibitions, f, ensure_ascii=False, indent=2)
    print(f"  ✅ 저장 완료 — 총 {len(exhibitions)}개 전시")

    print(f"\n{'─'*44}")
    print("업데이트된 전시 목록:")
    for i, exh in enumerate(exhibitions, 1):
        end = exh.get("end") or "미정"
        print(f"  {i:2d}. {exh['title']} — {exh['venue']} (~{end})")
    print(f"{'─'*44}\n")


if __name__ == "__main__":
    main()
