"""
ARTHAUS 전시 정보 자동 업데이트 스크립트
- 주요 한국 미술관 웹페이지 크롤링
- Gemini API로 전시 정보 구조화 (무료)
- exhibitions.json 업데이트
"""

import os
import json
import time
import re
from datetime import datetime, date
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from google import genai

# ── 설정 ─────────────────────────────────────────────────────────────────────

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

MUSEUMS = [
    {
        "name": "서울시립미술관 SeMA",
        "url": "https://sema.seoul.go.kr/kr/whatson/exhibition/list",
        "home": "https://sema.seoul.go.kr",
        "grad": "linear-gradient(135deg,#1a3050,#2a5070)",
    },
    {
        "name": "국립현대미술관",
        "url": "https://www.mmca.go.kr/exhibitions/exhibitionList.do?menuId=1010000000",
        "home": "https://www.mmca.go.kr",
        "grad": "linear-gradient(135deg,#0a1228,#1a2a50)",
    },
    {
        "name": "리움미술관",
        "url": "https://leeum.org/programs/exhibitions/current",
        "home": "https://leeum.org",
        "grad": "linear-gradient(135deg,#1a0a28,#2a1040)",
    },
    {
        "name": "예술의전당 한가람미술관",
        "url": "https://www.sac.or.kr/site/main/show/exhibition",
        "home": "https://www.sac.or.kr",
        "grad": "linear-gradient(135deg,#3a2800,#5a4000)",
    },
    {
        "name": "갤러리현대",
        "url": "https://galleryhyundai.com/exhibitions",
        "home": "https://galleryhyundai.com",
        "grad": "linear-gradient(135deg,#2a2620,#3a3228)",
    },
    {
        "name": "대구미술관",
        "url": "https://daeguartmuseum.or.kr/kor/information/exbList.do",
        "home": "https://daeguartmuseum.or.kr",
        "grad": "linear-gradient(135deg,#2a1a08,#3a2810)",
    },
    {
        "name": "부산현대미술관 MoCA",
        "url": "https://www.busan.go.kr/moca/exhibition01",
        "home": "https://www.busan.go.kr/moca",
        "grad": "linear-gradient(135deg,#180808,#2a1010)",
    },
    {
        "name": "환기미술관",
        "url": "http://whankimuseum.org",
        "home": "http://whankimuseum.org",
        "grad": "linear-gradient(135deg,#020810,#081838)",
    },
    {
        "name": "아트선재센터",
        "url": "https://artsonje.org/exhibition/",
        "home": "https://artsonje.org",
        "grad": "linear-gradient(135deg,#080418,#180a30)",
    },
    {
        "name": "국립중앙박물관",
        "url": "https://www.museum.go.kr/site/main/exhList/special",
        "home": "https://www.museum.go.kr",
        "grad": "linear-gradient(135deg,#0d1a2e,#1a3055)",
    },
]

GRAD_MAP = {m["name"]: m["grad"] for m in MUSEUMS}

# ── 크롤러 ────────────────────────────────────────────────────────────────────

def fetch_text(url: str, timeout: int = 12) -> str:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=timeout)
        resp.encoding = resp.apparent_encoding or "utf-8"
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header", "iframe", "noscript"]):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text[:2500]
    except Exception as e:
        return f"[크롤링 실패: {e}]"


def collect_all_museum_data() -> str:
    parts = []
    for museum in MUSEUMS:
        print(f"  크롤링 중: {museum['name']} ...")
        text = fetch_text(museum["url"])
        parts.append(f"=== {museum['name']} (홈: {museum['home']}) ===\n{text}")
        time.sleep(1)
    return "\n\n".join(parts)


# ── Gemini API ────────────────────────────────────────────────────────────────

def build_prompt(raw_text: str) -> str:
    today = date.today().isoformat()
    return f"""다음은 한국 주요 미술관 홈페이지에서 수집한 텍스트입니다.
오늘 날짜: {today}

{raw_text}

---
위 텍스트에서 현재 진행 중이거나 1개월 이내 예정된 전시를 추출하여
아래 JSON 배열 형식으로만 응답하세요. JSON 외 다른 텍스트는 절대 포함하지 마세요.

규칙:
1. 실제 확인된 전시만 포함 (정보 불분명하면 제외)
2. 최소 5개, 최대 15개
3. 날짜 형식: YYYY-MM-DD (모르면 null)
4. desc는 실제 전시 설명 기반, 한국어 2문장 이내
5. img는 항상 null
6. 첫 번째 항목이 가장 주목할 만한 전시

[
  {{
    "title": "전시 제목",
    "venue": "미술관 이름",
    "desc": "전시 설명",
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
        raise ValueError("JSON 배열을 찾을 수 없음")
    data = json.loads(match.group())
    for item in data:
        if not item.get("grad"):
            item["grad"] = GRAD_MAP.get(
                item.get("venue", ""),
                "linear-gradient(135deg,#1a2040,#2a3060)"
            )
    return data


def call_gemini(raw_text: str) -> list:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise EnvironmentError("GEMINI_API_KEY 환경변수가 설정되지 않았습니다.")

    client = genai.Client(api_key=api_key)

    print("  Gemini API 호출 중...")
    response = client.models.generate_content(
        model="gemini-2.0-flash",
        contents=build_prompt(raw_text),
    )
    return parse_json_from_response(response.text)


# ── 검증 ──────────────────────────────────────────────────────────────────────

REQUIRED_KEYS = {"title", "venue", "desc", "url"}

def validate(exhibitions: list) -> list:
    valid = []
    for item in exhibitions:
        if not REQUIRED_KEYS.issubset(item.keys()):
            continue
        if not item["title"] or not item["venue"]:
            continue
        item.setdefault("img", None)
        item.setdefault("grad", "linear-gradient(135deg,#1a2040,#2a3060)")
        item.setdefault("start", None)
        item.setdefault("end", None)
        valid.append(item)
    return valid


# ── 메인 ──────────────────────────────────────────────────────────────────────

def main():
    print(f"\n{'='*50}")
    print(f"ARTHAUS 전시 정보 업데이트: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*50}\n")

    print("[1/3] 미술관 페이지 크롤링...")
    raw_text = collect_all_museum_data()
    print(f"  수집 완료: {len(raw_text):,}자\n")

    print("[2/3] Gemini API로 전시 정보 분석...")
    try:
        exhibitions = call_gemini(raw_text)
        exhibitions = validate(exhibitions)
        print(f"  추출된 전시: {len(exhibitions)}개\n")
    except Exception as e:
        print(f"  ❌ 오류: {e}")
        print("  기존 exhibitions.json 유지")
        return

    if len(exhibitions) < 3:
        print("  ❌ 전시 데이터 부족 (3개 미만). 기존 파일 유지.")
        return

    print("[3/3] exhibitions.json 저장...")
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(exhibitions, f, ensure_ascii=False, indent=2)
    print(f"  ✅ 저장 완료: {OUTPUT_FILE}")

    print(f"\n{'─'*40}")
    print("업데이트된 전시 목록:")
    for i, exh in enumerate(exhibitions, 1):
        end = exh.get("end") or "미정"
        print(f"  {i:2d}. {exh['title']} — {exh['venue']} (~{end})")
    print(f"{'─'*40}\n")


if __name__ == "__main__":
    main()
