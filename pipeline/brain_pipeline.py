import re
import time
from typing import Dict, Optional, Tuple

from .intent_classifier import (
    CLASSIFIER_SYSTEM_PROMPT,
    build_classifier_input,
    normalize_intent_result,
    parse_intent_response,
)
from .llm_interface import call_json_llm
from .planner import (
    PLANNER_DOMAIN_CTRL,
    PLANNER_DOMAIN_PLAY,
    build_planner_input,
    enforce_intent_constraints,
    get_planner_system_prompt,
    parse_plan_response,
)
from .state_adapter import (
    detect_identity_confirmation_query,
    build_joint_angle_answer,
    build_repertoire_answer,
    detect_improv_request,
    detect_joint_angle_query,
    detect_repertoire_query,
    detect_song_request_code,
    detect_wave_play_request,
)
from .session import build_session_summary
from .songs import song_label_of

# config 경로는 실행 모드(phil_brain / 패키지)에 따라 달라 fallback 유지
try:
    from ..config import CLASSIFIER_MODEL, PLANNER_MODEL
except (ImportError, ValueError):
    from config import CLASSIFIER_MODEL, PLANNER_MODEL


# ── 결정적 shortcut (LLM 없이 직접 처리) ──────────────────────────────

def _is_greeting_wave(user_text: str) -> bool:
    """'안녕'과 '반가워'가 동시에 포함된 인사 발화를 감지한다."""
    return "안녕" in user_text and "반가워" in user_text


def _build_improv_packet(improv_request: Dict) -> str:
    """즉흥 요청 → PLAY|improv 패킷 (client "funk_80" 과 동일). bpm 만 있으면 장르 자리를 비운다."""
    genre_code = improv_request.get("genre") or ""
    bpm_value = improv_request.get("bpm")
    bpm_text = ""
    if isinstance(bpm_value, (int, float)):
        bpm_text = f"{bpm_value:g}"

    if genre_code and bpm_text:
        return f"PLAY|improv|{genre_code}|{bpm_text}"
    if bpm_text:
        return f"PLAY|improv||{bpm_text}"
    if genre_code:
        return f"PLAY|improv|{genre_code}"
    return "PLAY|improv"


def _improv_speech(improv_request: Dict) -> str:
    """즉흥 연주 시작 안내 대사 (validator 거부 시에는 repair 가 대사를 다시 만든다)."""
    genre_label = improv_request.get("genre_label") or ""
    bpm_value = improv_request.get("bpm")
    head_text = f"{genre_label} 장르로" if genre_label else "장르 구분 없이"
    if isinstance(bpm_value, (int, float)):
        return f"{head_text} {bpm_value:g} 비피엠 즉흥 연주를 시작할게요."
    return f"{head_text} 즉흥 연주를 시작할게요."


# 멈춤 계열은 전부 PAUSE — 재개 지점이 항상 저장된다 (CONTRACTS.md)
_PAUSE_KEYWORDS = {
    "멈춰", "멈춰봐", "잠깐", "일시정지", "일시 정지", "pause",
    "그만", "정지", "중지", "꺼줘", "꺼버려", "스톱", "stop",
}
_RESUME_KEYWORDS = {"다시", "계속", "이어서", "재개", "resume"}
_SPEED_UP_KEYWORDS = {"빨리", "빠르게"}
_SPEED_DOWN_KEYWORDS = {"천천히", "느리게"}

# 고정 문구가 놓치는 변형("속도 줄여줘")은 속도 명사 + 방향 동사 조합으로 판정
_SPEED_NOUNS = ("속도", "템포", "빠르기")
_SPEED_UP_VERBS = ("올려", "높여", "늘려", "키워")
_SPEED_DOWN_VERBS = ("내려", "낮춰", "줄여")

_SPEED_STEP = 0.1
_SPEED_MIN = 0.5   # 서버 PLAY_CTRL|speed 클램프 범위와 동일
_SPEED_MAX = 2.0

# 기본 배속(1.0) 복귀 발화 — 공백 제거 후 비교
_SPEED_RESET_KEYWORDS = (
    "원래속도", "원래템포", "원래빠르기", "원래대로",
    "정상속도", "기본속도", "기본빠르기",
)

# 명시적 배속 지정("1.3배로", "속도 0.8") — 배속 → 숫자+속도 → 속도+숫자 순 매칭
_SPEED_TARGET_PATTERNS = (
    re.compile(r"(\d+(?:\.\d+)?)\s*배(?:속)?"),
    re.compile(r"(\d+(?:\.\d+)?)\s*(?:의\s*)?속도"),
    re.compile(r"속도\s*(?:를|는)?\s*(\d+(?:\.\d+)?)"),
)


def _current_song_label(robot_state: Dict) -> str:
    """스냅샷의 곡 코드를 사용자 표시명으로 바꾼다. 없으면 'None'."""
    song_code = robot_state.get("current_song", "None")
    return song_label_of(song_code)


def _resolve_resume(user_text: str, robot_state: Dict) -> Optional[Tuple[list, str, Dict]]:
    """resume 해석 (재개 지점 유무는 서버 몫). None 이면 prefilter 포기 → planner 행."""
    if robot_state.get("state", 0) == 2:
        play_ctrl = {
            "action": "resume",
            "executed": False,
            "result": "already_playing",
            "song_label": _current_song_label(robot_state),
        }
        return [], "이미 연주 중이에요.", play_ctrl

    # 곡/즉흥을 지목했으면 재개가 아니라 새 연주 요청으로 본다.
    if detect_song_request_code(user_text) is not None:
        return None
    if detect_improv_request(user_text) is not None:
        return None

    play_ctrl = {"action": "resume", "executed": True, "result": "ok"}
    return ["RESUME"], "멈췄던 부분부터 이어서 연주할게요.", play_ctrl


def _parse_speed_target(text: str) -> Optional[float]:
    """발화에서 명시적 배속 숫자를 찾는다. 없으면 None."""
    condensed_text = text.replace(" ", "")
    if any(kw in condensed_text for kw in _SPEED_RESET_KEYWORDS):
        return 1.0
    for pattern in _SPEED_TARGET_PATTERNS:
        matched = pattern.search(text)
        if matched:
            return float(matched.group(1))
    return None


def _detect_speed_delta(text: str) -> float:
    """발화에서 속도 조절 방향을 판정한다. 속도 발화가 아니면 0.0 을 돌려준다."""
    if any(kw in text for kw in _SPEED_UP_KEYWORDS):
        return _SPEED_STEP
    if any(kw in text for kw in _SPEED_DOWN_KEYWORDS):
        return -_SPEED_STEP
    if any(noun in text for noun in _SPEED_NOUNS):
        if any(verb in text for verb in _SPEED_UP_VERBS):
            return _SPEED_STEP
        if any(verb in text for verb in _SPEED_DOWN_VERBS):
            return -_SPEED_STEP
    return 0.0


def _detect_play_control(user_text: str, robot_state: Dict) -> Optional[Tuple[list, str, Dict]]:
    """연주 제어(pause/speed/resume) 키워드 감지 → ([op_cmds], 고정 speech, notify 재료 play_ctrl) 또는 None."""
    text = user_text.strip()
    is_playing = robot_state.get("state", 0) == 2
    song_label = _current_song_label(robot_state)

    if any(kw in text for kw in _PAUSE_KEYWORDS):
        if not is_playing:
            play_ctrl = {"action": "pause", "executed": False, "result": "not_playing"}
            return [], "지금 연주 중인 곡이 없어요.", play_ctrl
        play_ctrl = {"action": "pause", "executed": True, "result": "ok", "song_label": song_label}
        return ["PAUSE"], "잠깐 멈출게요. 이어서 하려면 다시 틀어달라고 말씀해 주세요.", play_ctrl

    # 속도 조절은 PLAYING 일 때만 — 연주 전 "빠르게"는 planner 의 일반 연주 요청으로 흐른다
    if is_playing:
        # 명시적 배속("1.3배로") 우선, 없으면 방향 발화로 ±0.1 스텝
        requested_speed = _parse_speed_target(text)
        speed_delta = 0.0
        if requested_speed is None:
            speed_delta = _detect_speed_delta(text)

        if requested_speed is not None or speed_delta != 0.0:
            current_speed = robot_state.get("play_speed", 1.0)
            if not isinstance(current_speed, (int, float)):
                current_speed = 1.0
            if requested_speed is not None:
                target_speed = requested_speed
            else:
                target_speed = current_speed + speed_delta
            target_speed = round(min(_SPEED_MAX, max(_SPEED_MIN, target_speed)), 2)

            if target_speed != round(float(current_speed), 2):
                speed_result = "ok"
            elif requested_speed is not None:
                speed_result = "unchanged"
            else:
                speed_result = "at_limit"

            play_ctrl = {
                "action": "speed",
                "executed": True,
                "result": speed_result,
                "song_label": song_label,
                "speed_from": round(float(current_speed), 2),
                "speed_to": target_speed,
            }
            # 범위 밖 요청이 클램프됐으면 원래 요청값도 싣는다
            if requested_speed is not None and round(requested_speed, 2) != target_speed:
                play_ctrl["speed_req"] = round(requested_speed, 2)
            return (
                [f"PLAY_CTRL|speed|{target_speed:.2f}"],
                f"연주 속도를 {target_speed:.1f}배로 조절할게요.",
                play_ctrl,
            )

    if any(kw in text for kw in _RESUME_KEYWORDS):
        return _resolve_resume(user_text, robot_state)

    return None


def build_prefilter_plan(user_text: str, robot_state: Dict) -> Optional[Tuple[Dict, Dict, str]]:
    """LLM 없이 처리하는 결정적 shortcut → (classifier_output, planner_output, domain) 또는 None. 결과도 validator 를 거친다."""
    # 연주 제어 — 상태와 안 맞으면 빈 op_cmds + 안내. 최종 대사는 notify 가 만든다
    play_control = _detect_play_control(user_text, robot_state)
    if play_control is not None:
        op_cmds, control_speech, play_ctrl = play_control
        classifier_output = {"intent": "ctrl_request", "needs_motion": False}
        planner_output = {
            "skills": [],
            "op_cmd": list(op_cmds),
            "speech": control_speech,
            "reason": f"연주 제어 키워드 감지 → 직접 처리 ({op_cmds or 'speech-only'})",
            "play_ctrl": play_ctrl,
        }
        return classifier_output, planner_output, PLANNER_DOMAIN_CTRL

    # 즉흥 연주 — 실행 불가(상태/bpm 범위)는 validator 거부 → repair 가 설명
    improv_request = detect_improv_request(user_text)
    if improv_request is not None:
        classifier_output = {"intent": "play_request", "needs_motion": True}
        planner_output = {
            "skills": [],
            "op_cmd": [_build_improv_packet(improv_request)],
            "speech": _improv_speech(improv_request),
            "reason": "즉흥 연주 키워드 감지 → improv 직접 처리",
        }
        return classifier_output, planner_output, PLANNER_DOMAIN_PLAY

    # '안녕'과 '반가워'가 동시에 포함된 인사는 손 흔들기로 직접 처리한다.
    if _is_greeting_wave(user_text):
        classifier_output = {"intent": "motion_request", "needs_motion": True}
        planner_output = {
            "skills": ["wave_hi"],
            "op_cmd": [],
            "speech": "안녕하세요! 반가워요.",
            "reason": "'안녕'과 '반가워'가 동시에 포함된 인사는 손 흔들기로 직접 처리",
        }
        return classifier_output, planner_output, "motion"

    return None


def classify_step(
    user_text: str,
    classifier_model_name: str = CLASSIFIER_MODEL,
    capture_metrics: bool = False,
) -> Tuple[Dict, Dict]:
    """classifier LLM 호출 + 결정적 intent override → (classifier_output, diag)."""
    classifier_input = build_classifier_input(user_text)
    start_time = time.time()
    if capture_metrics:
        raw_response_text, metrics = call_json_llm(
            model_name=classifier_model_name,
            system_prompt=CLASSIFIER_SYSTEM_PROMPT,
            user_input_json=classifier_input,
            capture_metrics=True,
        )
    else:
        raw_response_text = call_json_llm(
            model_name=classifier_model_name,
            system_prompt=CLASSIFIER_SYSTEM_PROMPT,
            user_input_json=classifier_input,
        )
        metrics = {}
    duration_sec = time.time() - start_time

    classifier_output = parse_intent_response(raw_response_text)
    classifier_output = normalize_intent_result(classifier_output, user_text)

    # 곡 목록/레퍼토리 질문은 chat 으로 고정한다.
    if detect_repertoire_query(user_text):
        classifier_output["intent"] = "chat"
        classifier_output["needs_motion"] = False
    # 손 인사 후 곡 재생 복합 요청은 play_request 로 고정한다.
    if detect_wave_play_request(user_text) is not None:
        classifier_output["intent"] = "play_request"
        classifier_output["needs_motion"] = True

    diag = {
        "classifier_input": classifier_input,
        "raw_response_text": raw_response_text,
        "duration_sec": duration_sec,
        "metrics": metrics,
    }
    return classifier_output, diag


def build_direct_answer_plan(
    user_text: str,
    classifier_output: Dict,
    robot_state: Dict,
) -> Optional[Dict]:
    """planner 없이 직접 답하는 shortcut → planner_output 또는 None."""
    intent = classifier_output.get("intent")

    # 초단문 follow-up("왜?")은 generic planner 가 되묻는다 — 여기서 안 잡음

    # 1. 지원 곡 목록 질문은 안전 키 상태와 무관하게 고정 repertoire 로 답한다.
    if detect_repertoire_query(user_text):
        return {
            "skills": [],
            "op_cmd": [],
            "speech": build_repertoire_answer(),
            "reason": "지원 곡 목록 질문은 고정 repertoire 응답으로 직접 처리",
        }

    # 3. 이름 확인형 질문은 필의 정체성이 고정돼 있으므로 yes/no 몸짓을 고정한다.
    identity_query = detect_identity_confirmation_query(user_text)
    if identity_query is not None and intent == "motion_request":
        if identity_query["is_robot_name"]:
            return {
                "skills": ["nod_yes"],
                "op_cmd": [],
                "speech": "네, 제 이름은 필이에요.",
                "reason": "이름 확인 질문은 필의 정체성을 기준으로 직접 응답",
            }
        return {
            "skills": ["shake_no"],
            "op_cmd": [],
            "speech": "아니요, 제 이름은 필이에요.",
            "reason": "이름 확인 질문은 필의 정체성을 기준으로 직접 응답",
        }

    # 4. 손 인사 후 특정 곡 재생 복합 요청은 고정 skill 시퀀스로 처리한다.
    wave_play_request = detect_wave_play_request(user_text)
    if wave_play_request is not None:
        return {
            "skills": ["wave_hi", wave_play_request["play_skill"]],
            "op_cmd": [],
            "speech": f"손을 흔들며 인사하고, {wave_play_request['song_label']}를 연주할게요.",
            "reason": "손 인사 후 곡 재생 복합 요청은 고정 skill 시퀀스로 직접 처리",
        }

    # 5. 특정 관절 각도 질문은 현재 상태 스냅샷에서 직접 답한다.
    if intent == "status_question":
        joint_angle_query = detect_joint_angle_query(user_text)
        if joint_angle_query:
            return {
                "skills": [],
                "op_cmd": [],
                "speech": build_joint_angle_answer(robot_state, joint_angle_query),
                "reason": "현재 관절 각도 조회는 상태 스냅샷에서 직접 응답",
            }

    return None


def planner_step(
    robot_state: Dict,
    user_text: str,
    classifier_output: Dict,
    planner_domain: str,
    session=None,
    planner_model_name: str = PLANNER_MODEL,
    capture_metrics: bool = False,
    repair_hint: Optional[Dict] = None,
    play_ctrl: Optional[Dict] = None,
) -> Tuple[Dict, Dict]:
    """domain planner LLM 호출 — repair_hint(거부 사유)/play_ctrl(제어 결과)을 입력에 싣는다 → (planner_output, diag)."""
    planner_system_prompt = get_planner_system_prompt(planner_domain)
    session_summary = build_session_summary(session) if session is not None else None
    planner_input = build_planner_input(
        robot_state, user_text, classifier_output, planner_domain, session_summary, repair_hint, play_ctrl
    )

    start_time = time.time()
    if capture_metrics:
        raw_response_text, metrics = call_json_llm(
            model_name=planner_model_name,
            system_prompt=planner_system_prompt,
            user_input_json=planner_input,
            capture_metrics=True,
        )
    else:
        raw_response_text = call_json_llm(
            model_name=planner_model_name,
            system_prompt=planner_system_prompt,
            user_input_json=planner_input,
        )
        metrics = {}
    duration_sec = time.time() - start_time

    planner_output = parse_plan_response(raw_response_text)
    planner_output = enforce_intent_constraints(planner_output, classifier_output)

    diag = {
        "planner_input": planner_input,
        "raw_response_text": raw_response_text,
        "duration_sec": duration_sec,
        "metrics": metrics,
    }
    return planner_output, diag
