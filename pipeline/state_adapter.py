import re
from typing import Dict

# 곡 데이터는 songs.py 가 단일 소스다 — 여기서는 가져다 쓰기만 한다.
from .songs import (
    IMPROV_GENRE_LABELS,
    IMPROV_GENRES,
    PLAY_SKILL_BY_SONG,
    SONG_CODES,
    SONG_LABELS,
    SONG_QUERY_ALIASES,
)
WAVE_REQUEST_KEYWORDS = ["손흔들", "손 흔들", "인사", "wave"]
PLAY_REQUEST_SUFFIXES = ["해줘", "해주세요", "해", "줘", "틀어", "연주", "쳐", "시작"]
ROBOT_NAME_ALIASES = {"필", "phil"}

# 관절명은 Phil-drum-robot motors.json 이름을 그대로 쓴다.
JOINT_QUERY_ALIASES = [
    ("왼쪽 손목", "left_wrist", "왼쪽 손목"),
    ("왼손목", "left_wrist", "왼쪽 손목"),
    ("오른쪽 손목", "right_wrist", "오른쪽 손목"),
    ("오른손목", "right_wrist", "오른쪽 손목"),
    ("허리", "waist", "허리"),
    ("왼쪽 팔", "left_shoulder_1", "왼쪽 팔"),
    ("왼팔", "left_shoulder_1", "왼쪽 팔"),
    ("오른쪽 팔", "right_shoulder_1", "오른쪽 팔"),
    ("오른팔", "right_shoulder_1", "오른쪽 팔"),
    ("왼쪽 발", "left_pedal", "왼쪽 발"),
    ("왼발", "left_pedal", "왼쪽 발"),
    ("오른쪽 발", "right_pedal", "오른쪽 발"),
    ("오른발", "right_pedal", "오른쪽 발"),
]

ANGLE_QUERY_PATTERN = re.compile(r"(각도|몇\s*도|몇도)")
REPERTOIRE_QUERY_PATTERNS = [
    re.compile(r"(무슨|어떤)\s*노래.*연주할\s*수\s*있"),
    re.compile(r"(무슨|어떤)\s*곡.*연주할\s*수\s*있"),
    re.compile(r"연주할\s*수\s*있는\s*(노래|곡)"),
    re.compile(r"(노래|곡)\s*(목록|리스트)"),
    re.compile(r"레퍼토리"),
]
AVAILABLE_SONG_CODES = SONG_CODES  # songs.py 파생 (레퍼토리 직답 나열 순서)
IDENTITY_CONFIRMATION_PATTERN = re.compile(
    r"(?:너의\s*)?이름(?:은)?\s*([A-Za-z가-힣]+)\s*(맞(?:지|죠|니|나요)|이니|인가|인가요)"
)


def adapt_robot_state(robot_state):
    """상태 스냅샷을 그대로 전달 (이미 deepcopy 라 재복사 없음)."""
    return robot_state if isinstance(robot_state, dict) else {}


def build_planner_state_summary(robot_state: Dict) -> Dict:
    """planner 용 고수준 상태 요약 (세부 관절각은 resolver/validator 몫)."""
    state_value = robot_state.get("state", 0)
    is_fixed = robot_state.get("is_fixed", True)

    summary = {
        "state": state_value,
        "can_move": robot_state.get("is_lock_key_removed", False),
        "is_fixed": is_fixed,
        "busy": state_value != 0 or not is_fixed,
        "block_reason": block_reason_of(robot_state),
        "current_song": robot_state.get("current_song", "None"),
        "bpm": robot_state.get("bpm", 100),
        "progress": robot_state.get("progress", "unknown"),
        "last_action": robot_state.get("last_action", "None"),
    }

    if "error_message" in robot_state:
        summary["error_message"] = robot_state["error_message"]
    if "current_angles" in robot_state:
        # status/planner 가 현재 자세를 설명할 수 있도록 원본 관절 스냅샷을 전달한다.
        summary["current_angles"] = robot_state["current_angles"]

    return summary


def block_reason_of(robot_state: Dict) -> str:
    """
    동작을 막는 단일 사유 코드: safety_key > playing > error > moving > none.
    (에러 state 표기가 경로마다 4/6 으로 섞여 있어 둘 다 error 로 본다.)
    """
    if not robot_state.get("is_lock_key_removed", False):
        return "safety_key"
    state_value = robot_state.get("state", 0)
    if state_value == 2:
        return "playing"
    if state_value in (4, 6):
        return "error"
    if not robot_state.get("is_fixed", True):
        return "moving"
    return "none"


def detect_joint_angle_query(user_text: str):
    """특정 관절의 현재 각도 질의 감지 — deterministic 직답 대상."""
    text = (user_text or "").strip()
    if not text or not ANGLE_QUERY_PATTERN.search(text):
        return None

    for alias, joint_name, display_name in JOINT_QUERY_ALIASES:
        if alias in text:
            return {
                "joint_name": joint_name,
                "display_name": display_name,
            }

    return None


def build_joint_angle_answer(robot_state: Dict, joint_info: Dict):
    """
    상태 스냅샷에서 현재 관절 각도를 직접 읽어 사용자용 설명 문장으로 만든다.
    """
    if not isinstance(joint_info, dict):
        return None

    current_angles = robot_state.get("current_angles", {})
    angle_value = current_angles.get(joint_info["joint_name"])
    if not isinstance(angle_value, (int, float)):
        return f"{joint_info['display_name']}의 현재 각도는 아직 확인할 수 없습니다."

    return f"현재 {joint_info['display_name']} 각도는 {float(angle_value):.1f}도입니다."


def detect_repertoire_query(user_text: str) -> bool:
    text = (user_text or "").strip()
    if not text:
        return False

    return any(pattern.search(text) for pattern in REPERTOIRE_QUERY_PATTERNS)


def build_repertoire_answer() -> str:
    song_names = [SONG_LABELS[song_code] for song_code in AVAILABLE_SONG_CODES]
    return "저는 " + ", ".join(song_names) + "를 연주할 수 있어요."


def detect_song_request_code(user_text: str):
    text = (user_text or "").strip().lower()
    if not text:
        return None

    for song_code, alias_list in SONG_QUERY_ALIASES.items():
        if any(alias in text for alias in alias_list):
            return song_code

    return None


# 즉흥 연주 감지 — "즉흥" 이 있으면 improv 요청으로 본다 (장르/bpm 은 선택).
IMPROV_TRIGGER_WORD = "즉흥"
# 능력/의미 질문("즉흥 연주 할 수 있어?")은 연주 시작이 아니라 classifier 로 보낸다.
IMPROV_QUESTION_WORDS = ["수있", "가능", "뭐야", "뭐니", "무슨", "어떤"]
IMPROV_BPM_PATTERNS = [
    re.compile(r"(\d+(?:\.\d+)?)\s*(?:비피엠|bpm)"),
    re.compile(r"(?:비피엠|bpm)\s*(\d+(?:\.\d+)?)"),
]


def detect_improv_request(user_text: str):
    """'즉흥' 발화 → {genre, genre_label, bpm} 또는 None. bpm 범위 검증은 command_validator 몫."""
    text = (user_text or "").strip().lower()
    if IMPROV_TRIGGER_WORD not in text:
        return None

    condensed_text = re.sub(r"\s+", "", text)
    if any(word in condensed_text for word in IMPROV_QUESTION_WORDS):
        return None

    # 장르가 여러 개 언급되면 발화에서 먼저 나온 쪽을 쓴다.
    genre_code = None
    genre_pos = len(condensed_text) + 1
    for code, genre_info in IMPROV_GENRES.items():
        for alias in genre_info["aliases"]:
            alias_pos = condensed_text.find(alias)
            if 0 <= alias_pos < genre_pos:
                genre_code = code
                genre_pos = alias_pos

    bpm_value = None
    for pattern in IMPROV_BPM_PATTERNS:
        matched = pattern.search(condensed_text)
        if matched:
            bpm_value = float(matched.group(1))
            break

    return {
        "genre": genre_code,
        "genre_label": IMPROV_GENRE_LABELS.get(genre_code, ""),
        "bpm": bpm_value,
    }


def detect_wave_play_request(user_text: str):
    text = (user_text or "").strip().lower()
    if not text:
        return None

    song_code = detect_song_request_code(text)
    if song_code is None:
        return None

    has_wave = any(keyword in text for keyword in WAVE_REQUEST_KEYWORDS)
    has_play_suffix = any(keyword in text for keyword in PLAY_REQUEST_SUFFIXES)
    if not has_wave or not has_play_suffix:
        return None

    return {
        "song_code": song_code,
        "song_label": SONG_LABELS[song_code],
        "play_skill": PLAY_SKILL_BY_SONG[song_code],
    }


def detect_identity_confirmation_query(user_text: str):
    text = (user_text or "").strip()
    if not text:
        return None

    match_obj = IDENTITY_CONFIRMATION_PATTERN.search(text)
    if match_obj is None:
        return None

    candidate_name = match_obj.group(1).strip()
    normalized_name = candidate_name.lower()
    return {
        "candidate_name": candidate_name,
        "is_robot_name": normalized_name in ROBOT_NAME_ALIASES,
    }
