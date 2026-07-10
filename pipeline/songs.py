"""
곡 데이터 단일 소스 (single source of truth).

곡 코드는 Phil-drum-robot config/play_list.json 의 id 를 그대로 쓴다.
곡을 추가/삭제할 때는 이 파일의 SONGS 만 고치면 된다 — 아래 파생 테이블과
소비처(state_adapter/intent_classifier/command_validator)는 전부 여기서 갈라진다.
단, 대응하는 play skill 은 skills.py 의 SKILL_LIBRARY 에 함께 추가해야 한다.

과거에는 SONG_LABELS/SONG_QUERY_ALIASES/PLAY_SKILL_BY_SONG/AVAILABLE_SONG_CODES
(state_adapter), PLAY_SONG_KEYWORDS(intent_classifier), PLAY_CODES(command_validator)
가 각 파일에 따로 정의돼 곡 하나 추가에 3~4곳 동기화가 필요했다.
"""

from typing import Dict, List

# 곡별 원본 데이터: 라벨(사용자 표시명), 대응 play skill, 발화 별칭(소문자 비교용)
SONGS: Dict[str, Dict] = {
    "TI": {
        "label": "This Is Me",
        "skill": "play_ti",
        "aliases": ["this is me", "디스 이즈 미", "디스이즈미", "tim"],
    },
    "TY": {
        "label": "그대에게",
        "skill": "play_ty",
        "aliases": ["그대에게"],
    },
    "BI": {
        "label": "Baby I Need You",
        "skill": "play_bi",
        "aliases": ["baby i need you", "베이비 아이 니드 유", "bi"],
    },
    "BF": {
        "label": "필인",
        "skill": "play_bf",
        "aliases": ["필인", "fillin", "fill in"],
    },
    "DS": {
        "label": "드럼 솔로",
        "skill": "play_ds",
        "aliases": ["드럼 솔로", "드럼솔로", "drum solo"],
    },
    "WS": {
        "label": "왜그래",
        "skill": "play_ws",
        "aliases": ["왜그래", "왜 그래", "why so"],
    },
}

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
