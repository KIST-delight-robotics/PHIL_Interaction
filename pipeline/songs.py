"""
곡/즉흥 장르 데이터 로더 (원본: data/songs.json — 곡 코드는 서버 play_list.json 의 id).
곡 추가/삭제는 songs.json 만 고치면 되고, 대응 play skill 은 skills.json 에 함께 추가한다.
"""

from typing import Dict, List

from .data_files import load_data_json

_SONG_DATA: Dict = load_data_json("songs.json")

# 곡별 원본 데이터: 라벨(사용자 표시명), 대응 play skill, 발화 별칭(소문자 비교용)
SONGS: Dict[str, Dict] = _SONG_DATA["songs"]

# 즉흥 연주 장르: 코드는 drumrobot_server/data/scores/ 하위 폴더명과 같다.
# 와이어 명령은 PLAY|improv|<장르>[|<bpm>] — drumrobot_client build_play_packet 과 동일 형태.
IMPROV_GENRES: Dict[str, Dict] = _SONG_DATA["improv_genres"]

# ── 파생 테이블 (소비처가 쓰는 모양 그대로) ──────────────────────────────

# 레퍼토리 나열/검증용 곡 코드 목록 (선언 순서 유지)
SONG_CODES: List[str] = list(SONGS.keys())

# 코드 → 사용자 표시명. "None" 은 "연주 중인 곡 없음" 표시용 특수 키.
SONG_LABELS: Dict[str, str] = {code: info["label"] for code, info in SONGS.items()}
SONG_LABELS["None"] = "None"

# 코드 → planner 가 고르는 play skill 이름 (skills.SKILL_LIBRARY 의 키)
PLAY_SKILL_BY_SONG: Dict[str, str] = {code: info["skill"] for code, info in SONGS.items()}

# 코드 → 발화 별칭 목록 (곡 지목 감지용)
SONG_QUERY_ALIASES: Dict[str, List[str]] = {code: list(info["aliases"]) for code, info in SONGS.items()}

# 모든 별칭 평탄화 — "곡 제목이 포함됐는가"류 검사용 (구 PLAY_SONG_KEYWORDS)
SONG_ALIAS_KEYWORDS: List[str] = [
    alias for info in SONGS.values() for alias in info["aliases"]
]

IMPROV_GENRE_CODES: List[str] = list(IMPROV_GENRES.keys())
IMPROV_GENRE_LABELS: Dict[str, str] = {
    code: info["label"] for code, info in IMPROV_GENRES.items()
}


def song_label_of(play_code) -> str:
    """PLAY 코드 → 표시명. improv 코드("improv:jazz:80")는 장르 라벨로 풀어 쓴다."""
    code_text = str(play_code)
    if code_text in SONG_LABELS:
        return SONG_LABELS[code_text]

    parts = [token.strip() for token in code_text.replace("|", ":").split(":")]
    if not parts or parts[0].lower() != "improv":
        return code_text

    genre_label = ""
    if len(parts) >= 2 and parts[1]:
        genre_label = IMPROV_GENRE_LABELS.get(parts[1].lower(), parts[1])
    if genre_label:
        return f"{genre_label} 즉흥 연주"
    return "즉흥 연주"
