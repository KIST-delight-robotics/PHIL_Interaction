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


# 일시정지(재개 가능)와 중지(재개 지점 폐기)는 다른 명령이다 — 신 서버 PAUSE / PLAY_CTRL|stop.
# "일시정지"가 "정지"를 부분 문자열로 포함하므로 pause 키워드를 먼저 검사한다.
_PAUSE_KEYWORDS = {"멈춰", "멈춰봐", "잠깐", "일시정지", "일시 정지", "pause"}
_STOP_KEYWORDS = {"그만", "정지", "중지", "꺼줘", "꺼버려", "스톱", "stop"}
_RESUME_KEYWORDS = {"다시", "계속", "이어서", "재개", "resume"}
_SPEED_UP_KEYWORDS = {"빨리", "빠르게", "속도 올려", "템포 올려"}
_SPEED_DOWN_KEYWORDS = {"천천히", "느리게", "속도 내려", "템포 내려"}

_SPEED_STEP = 0.1
_SPEED_MIN = 0.5   # 서버 PLAY_CTRL|speed 클램프 범위와 동일
_SPEED_MAX = 2.0


def _resolve_resume(user_text: str, robot_state: Dict) -> Optional[Tuple[list, str]]:
    """
    resume 발화를 해석한다. 재개 지점 유무 판단은 서버(RESUME 처리) 몫이다.

    반환값:
        ([op_cmds], speech) — 빈 op_cmds 는 안내만 한다는 뜻
        None — prefilter 를 포기하고 planner 로 넘긴다 (예: "그 노래 다시 틀어줘" 처음부터 재생)
    """
    if robot_state.get("state", 0) == 2:
        return [], "이미 연주 중이에요."

    # 곡을 지목했으면 재개가 아니라 그 곡 재생 요청으로 본다.
    if detect_song_request_code(user_text) is not None:
        return None

    return ["RESUME"], "멈췄던 부분부터 이어서 연주할게요."


def _detect_play_control(user_text: str, robot_state: Dict) -> Optional[Tuple[list, str]]:
    """
    연주 제어(pause/stop/speed/resume) 발화를 감지한다.
    LLM 없이 키워드 매칭 + 상태 스냅샷으로 직접 처리해 지연을 줄인다.

    반환값: ([op_cmds], speech) 또는 None. 빈 op_cmds 는 안내만 한다는 뜻.
    """
    text = user_text.strip()
    is_playing = robot_state.get("state", 0) == 2

    if any(kw in text for kw in _PAUSE_KEYWORDS):
        if not is_playing:
            return [], "지금 연주 중인 곡이 없어요."
        return ["PAUSE"], "잠깐 멈출게요. 이어서 하려면 다시 틀어달라고 말씀해 주세요."

    if any(kw in text for kw in _STOP_KEYWORDS):
        if not is_playing:
            return [], "지금 연주 중인 곡이 없어요."
        return ["PLAY_CTRL|stop"], "연주를 중지할게요."

    # 속도 조절은 서버가 연주 중에만 받으므로 PLAYING 일 때만 감지한다.
    # (연주 시작 전 "빠르게 연주해줘"는 planner 로 흘러가 일반 연주 요청으로 처리된다.)
    if is_playing:
        speed_delta = 0.0
        if any(kw in text for kw in _SPEED_UP_KEYWORDS):
            speed_delta = _SPEED_STEP
        elif any(kw in text for kw in _SPEED_DOWN_KEYWORDS):
            speed_delta = -_SPEED_STEP

        if speed_delta != 0.0:
            current_speed = robot_state.get("play_speed", 1.0)
            if not isinstance(current_speed, (int, float)):
                current_speed = 1.0
            target_speed = round(min(_SPEED_MAX, max(_SPEED_MIN, current_speed + speed_delta)), 2)
            return [f"PLAY_CTRL|speed|{target_speed:.2f}"], f"연주 속도를 {target_speed:.1f}배로 조절할게요."

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
    # 연주 제어(pause/stop/speed/resume) 발화는 LLM latency 없이 즉시 처리한다.
    # 상태와 맞지 않는 제어(IDLE 에서 멈춰, 재개 지점 없는 재개 등)는 빈 op_cmds + 안내만 나온다.
    play_control = _detect_play_control(user_text, robot_state)
    if play_control is not None:
        op_cmds, control_speech = play_control
        classifier_output = {"intent": "stop_request", "needs_motion": False}
        planner_output = {
            "skills": [],
            "op_cmd": list(op_cmds),
            "speech": control_speech,
            "reason": f"연주 제어 키워드 감지 → 직접 처리 ({op_cmds or 'speech-only'})",
        }
        return classifier_output, planner_output, "stop"

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
    repair_hint: Dict = None,
) -> Tuple[Dict, Dict]:
    """
    2차 planner 단계. domain-specific planner LLM 을 호출해 plan 후보를 만든다.
    repair_hint 가 있으면(repair 도메인 호출) 직전 거부 사유를 입력에 함께 싣는다.

    반환값:
        (planner_output, diag)
        diag = {planner_input, raw_response_text, duration_sec, metrics}
    """
    planner_system_prompt = get_planner_system_prompt(planner_domain)
    session_summary = build_session_summary(session) if session is not None else None
    planner_input = build_planner_input(
        robot_state, user_text, classifier_output, planner_domain, session_summary, repair_hint
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
