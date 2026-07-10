import re
import time
from typing import Dict, Optional, Tuple

from .intent_classifier import (
    CLASSIFIER_SYSTEM_PROMPT,           # classifier system prompt
    build_classifier_input,        # classifier 입력 JSON 생성
    normalize_intent_result,            # classifier 결과 후처리/정규화
    parse_intent_response,              # classifier JSON 응답 파싱
)

# JSON 형식 LLM 호출 공통 래퍼
from .llm_interface import call_json_llm
from .planner import (
    PLANNER_DOMAIN_CTRL,           # 연주 제어 도메인 (prefilter shortcut 이 사용)
    build_planner_input,           # planner 입력 JSON 생성
    enforce_intent_constraints,         # planner 결과를 intent/domain 기준으로 한 번 더 정리
    get_planner_system_prompt,          # domain별 planner system prompt 생성
    parse_plan_response,                # planner JSON 응답 파싱
)

# 관절 각도/정체/레퍼토리 등 direct-answer shortcut 감지
from .state_adapter import (
    detect_identity_confirmation_query,
    build_joint_angle_answer,
    build_repertoire_answer,
    detect_joint_angle_query,
    detect_repertoire_query,
    detect_song_request_code,
    detect_wave_play_request,
)

# planner input 에 넣을 session 요약
from .session import build_session_summary

# 곡 코드 → 표시명 (notify 대사 재료)
from .songs import SONG_LABELS

# config 는 패키지 깊이에 따라 경로가 달라 fallback 을 유지한다.
# (phil_brain 모드: pipeline 이 top-level → 'config' / eval·tests 모드: phil_robot.pipeline → '..config')
try:
    from ..config import CLASSIFIER_MODEL, PLANNER_MODEL
except (ImportError, ValueError):
    from config import CLASSIFIER_MODEL, PLANNER_MODEL


# ======================================================================
# 결정적 shortcut 감지 (LLM 없이 user_text/상태로 직접 처리)
# ======================================================================

def _is_greeting_wave(user_text: str) -> bool:
    """'안녕'과 '반가워'가 동시에 포함된 인사 발화를 감지한다."""
    return "안녕" in user_text and "반가워" in user_text


# 멈춤 계열(멈춰/그만/정지/스톱...)은 전부 PAUSE 하나로 보낸다.
# 재개 지점은 항상 저장돼 "그만해" 후 "다시 틀어줘"도 이어서 재생된다 (2026-07-10 결정, CONTRACTS.md 참조).
_PAUSE_KEYWORDS = {
    "멈춰", "멈춰봐", "잠깐", "일시정지", "일시 정지", "pause",
    "그만", "정지", "중지", "꺼줘", "꺼버려", "스톱", "stop",
}
_RESUME_KEYWORDS = {"다시", "계속", "이어서", "재개", "resume"}
_SPEED_UP_KEYWORDS = {"빨리", "빠르게"}
_SPEED_DOWN_KEYWORDS = {"천천히", "느리게"}

# "속도를 줄여줘"/"속도 늘려줘"처럼 조사·동사 변형이 끼면 고정 문구 매칭이 놓친다.
# 속도 명사 + 방향 동사가 함께 있으면 방향을 판정한다.
# ("소리 줄여줘", "팔 내려" 같은 발화는 속도 명사가 없어 여기 걸리지 않는다.)
_SPEED_NOUNS = ("속도", "템포", "빠르기")
_SPEED_UP_VERBS = ("올려", "높여", "늘려", "키워")
_SPEED_DOWN_VERBS = ("내려", "낮춰", "줄여")

_SPEED_STEP = 0.1
_SPEED_MIN = 0.5   # 서버 PLAY_CTRL|speed 클램프 범위와 동일
_SPEED_MAX = 2.0

# "원래 속도로", "정상 속도로" 같은 기본 배속(1.0) 복귀 발화.
# 공백 제거 후 비교하므로 "원래속도로"도 걸린다.
_SPEED_RESET_KEYWORDS = (
    "원래속도", "원래템포", "원래빠르기", "원래대로",
    "정상속도", "기본속도", "기본빠르기",
)

# "1배속으로", "1.3배로", "0.8 속도로", "속도 0.8로" 같은 명시적 배속 지정.
# 매칭 순서: 배/배속 표현 → 숫자+속도 → 속도+숫자.
_SPEED_TARGET_PATTERNS = (
    re.compile(r"(\d+(?:\.\d+)?)\s*배(?:속)?"),
    re.compile(r"(\d+(?:\.\d+)?)\s*(?:의\s*)?속도"),
    re.compile(r"속도\s*(?:를|는)?\s*(\d+(?:\.\d+)?)"),
)


def _current_song_label(robot_state: Dict) -> str:
    """스냅샷의 곡 코드를 사용자 표시명으로 바꾼다. 없으면 'None'."""
    song_code = robot_state.get("current_song", "None")
    return SONG_LABELS.get(song_code, str(song_code))


def _resolve_resume(user_text: str, robot_state: Dict) -> Optional[Tuple[list, str, Dict]]:
    """
    resume 발화를 해석한다. 재개 지점 유무 판단은 서버(RESUME 처리) 몫이다.

    반환값:
        ([op_cmds], speech, play_ctrl) — 빈 op_cmds 는 안내만 한다는 뜻.
        speech 는 notify LLM 실패 시 쓸 고정 fallback, play_ctrl 은 notify 대사 재료다.
        None — prefilter 를 포기하고 planner 로 넘긴다 (예: "그 노래 다시 틀어줘" 처음부터 재생)
    """
    if robot_state.get("state", 0) == 2:
        play_ctrl = {
            "action": "resume",
            "executed": False,
            "result": "already_playing",
            "song_label": _current_song_label(robot_state),
        }
        return [], "이미 연주 중이에요.", play_ctrl

    # 곡을 지목했으면 재개가 아니라 그 곡 재생 요청으로 본다.
    if detect_song_request_code(user_text) is not None:
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
    """
    연주 제어(pause/speed/resume) 발화를 감지한다.
    LLM 없이 키워드 매칭 + 상태 스냅샷으로 직접 처리해 지연을 줄인다.

    반환값: ([op_cmds], speech, play_ctrl) 또는 None. 빈 op_cmds 는 안내만 한다는 뜻.
    speech 는 고정 fallback 대사, play_ctrl 은 notify LLM 이 대사를 만들 때 쓰는 재료다.
    """
    text = user_text.strip()
    is_playing = robot_state.get("state", 0) == 2
    song_label = _current_song_label(robot_state)

    if any(kw in text for kw in _PAUSE_KEYWORDS):
        if not is_playing:
            play_ctrl = {"action": "pause", "executed": False, "result": "not_playing"}
            return [], "지금 연주 중인 곡이 없어요.", play_ctrl
        play_ctrl = {"action": "pause", "executed": True, "result": "ok", "song_label": song_label}
        return ["PAUSE"], "잠깐 멈출게요. 이어서 하려면 다시 틀어달라고 말씀해 주세요.", play_ctrl

    # 속도 조절은 서버가 연주 중에만 받으므로 PLAYING 일 때만 감지한다.
    # (연주 시작 전 "빠르게 연주해줘"는 planner 로 흘러가 일반 연주 요청으로 처리된다.)
    if is_playing:
        # 명시적 배속("1.3배로")이 있으면 그 값을, 없으면 방향 발화로 ±0.1 스텝을 쓴다.
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
            # 요청 배속이 허용 범위 밖이라 클램프됐으면 원래 요청값도 알려 준다.
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
    """
    classifier 호출 없이 user_text(+상태 스냅샷)로 처리 가능한 결정적 shortcut.

    반환값:
        (classifier_output, planner_output, planner_domain) 또는 None
    여기서 만든 planner_output 은 이후 build_validated_plan() 이
    최신 robot_state 로 한 번 검증한다.
    """
    # 연주 제어(pause/speed/resume) 발화는 LLM latency 없이 즉시 처리한다.
    # 상태와 맞지 않는 제어(IDLE 에서 멈춰, 재개 지점 없는 재개 등)는 빈 op_cmds + 안내만 나온다.
    # speech 는 고정 fallback 이고, 명령 전송 뒤 notify LLM 이 play_ctrl 로 최종 대사를 만든다.
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
    """
    1차 classifier 단계. classifier LLM 을 호출해 intent 를 정한 뒤,
    repertoire/wave-play 같은 결정적 intent override 를 적용한다.

    반환값:
        (classifier_output, diag)
        diag = {classifier_input, raw_response_text, duration_sec, metrics}
    """
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
    """
    classifier 결과 + 현재 상태로 planner 없이 직접 답할 수 있는 shortcut.
    planner LLM 호출을 건너뛰고 고정/상태기반 응답을 낸다.

    반환값: planner_output 또는 None
    순서(우선순위)는 기존 동작과 동일하게 유지한다.
    """
    intent = classifier_output.get("intent")

    # ("왜?"/"뭐?" 같은 맥락 없는 초단문 follow-up 은 generic planner 가 직접 되묻으므로
    #  여기서 별도 shortcut 으로 잡지 않는다.)

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
    """
    2차 planner 단계. domain-specific planner LLM 을 호출해 plan 후보를 만든다.
    repair_hint 가 있으면(repair 도메인 호출) 직전 거부 사유를 입력에 함께 싣는다.
    play_ctrl 이 있으면(notify 도메인 호출) 이미 실행된 연주 제어 내용을 입력에 싣는다.

    반환값:
        (planner_output, diag)
        diag = {planner_input, raw_response_text, duration_sec, metrics}
    """
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
