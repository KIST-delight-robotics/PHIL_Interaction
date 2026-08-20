"""2차 planner LLM 계층 — classifier 가 정한 의도로 speech 와 skill/command plan 을 만든다."""

import json
from typing import Dict, Optional, Set

from .data_files import load_prompt_text
from .failure import FALLBACK_MESSAGE, build_planner_failure_result, sanitize_message
from .state_adapter import build_planner_state_summary
from .skills import describe_skills_for_prompt, filter_skills_by_allowed_categories


PLANNER_RESPONSE_SCHEMA_EXAMPLE = {
    "s": [],
    "c": [],
    "t": "안녕하세요!",
    "r": "simple greeting",
    "q": None,  # clarification 질문 (평소 null)
}

SKILL_CATALOG_TEXT = describe_skills_for_prompt()

PLANNER_DOMAIN_DEFAULT = "generic"
PLANNER_DOMAIN_CHAT = "chat"
PLANNER_DOMAIN_MOTION = "motion"
PLANNER_DOMAIN_PLAY = "play"
PLANNER_DOMAIN_STATUS = "status"
PLANNER_DOMAIN_CTRL = "ctrl"
# repair/notify 는 intent 선택이 아니라 run_turn 이 강제하는 도메인 —
# repair 는 validator 거부 설명, notify 는 이미 전송된 연주 제어의 대사 생성.
PLANNER_DOMAIN_REPAIR = "repair"
PLANNER_DOMAIN_NOTIFY = "notify"

INTENT_TO_DOMAIN = {
    "chat": PLANNER_DOMAIN_CHAT,
    "motion_request": PLANNER_DOMAIN_MOTION,
    "play_request": PLANNER_DOMAIN_PLAY,
    "status_question": PLANNER_DOMAIN_STATUS,
    "ctrl_request": PLANNER_DOMAIN_CTRL,
    "unknown": PLANNER_DOMAIN_DEFAULT,
}

DOMAIN_ALLOWED_SKILL_CATEGORIES: Dict[str, Set[str]] = {
    PLANNER_DOMAIN_CHAT: set(),
    PLANNER_DOMAIN_MOTION: {"social", "visual", "posture"},
    # play 에 posture 를 주면 ready_pose 를 습관적으로 얹는다 — 준비 자세는 서버 PLAY 내부 처리
    PLANNER_DOMAIN_PLAY: {"play"},
    PLANNER_DOMAIN_STATUS: set(),
    PLANNER_DOMAIN_CTRL: {"system"},
    PLANNER_DOMAIN_REPAIR: set(),
    PLANNER_DOMAIN_NOTIFY: set(),
    PLANNER_DOMAIN_DEFAULT: {"social", "visual", "posture", "play", "system"},
}

# 도메인별 지시문과 공통 규칙 원문은 prompts/planner_*.md 에 있다.
DOMAIN_INSTRUCTIONS = {
    domain: load_prompt_text(f"planner_{domain}.md")
    for domain in (
        PLANNER_DOMAIN_CHAT,
        PLANNER_DOMAIN_MOTION,
        PLANNER_DOMAIN_PLAY,
        PLANNER_DOMAIN_STATUS,
        PLANNER_DOMAIN_CTRL,
        PLANNER_DOMAIN_DEFAULT,
        PLANNER_DOMAIN_REPAIR,
        PLANNER_DOMAIN_NOTIFY,
    )
}

# 공통 규칙의 {skill_catalog} 자리에 skill 카탈로그를 치환한다.
# 프롬프트 안에 JSON 예시 중괄호가 있어 str.format 대신 replace 를 쓴다.
PLANNER_SHARED_RULES = load_prompt_text("planner_shared.md").replace(
    "{skill_catalog}", SKILL_CATALOG_TEXT
)


def select_planner_domain(classifier_output: Dict) -> str:
    """classifier intent 를 planner 도메인으로 변환한다."""
    intent = classifier_output.get("intent", "unknown")
    if intent == "chat" and classifier_output.get("needs_motion", False):
        return PLANNER_DOMAIN_MOTION
    return INTENT_TO_DOMAIN.get(intent, PLANNER_DOMAIN_DEFAULT)


def get_planner_system_prompt(planner_domain: str) -> str:
    """도메인별 planner system prompt 를 생성한다."""
    domain_instruction = DOMAIN_INSTRUCTIONS.get(planner_domain, DOMAIN_INSTRUCTIONS[PLANNER_DOMAIN_DEFAULT])
    return f"{PLANNER_SHARED_RULES}\n\n도메인 규칙:\n{domain_instruction}"


def build_planner_input(
    robot_state: Dict,
    user_text: str,
    classifier_output: Dict,
    planner_domain: str,
    session_summary: Optional[Dict] = None,
    repair_hint: Optional[Dict] = None,
    play_ctrl: Optional[Dict] = None,
) -> str:
    """planner 입력 JSON 생성 — session_summary/repair_hint/play_ctrl 이 있으면 포함한다."""
    state_summary = build_planner_state_summary(robot_state)
    needs_motion = bool(classifier_output.get("needs_motion", False))

    payload = {
        "planner_domain": planner_domain,
        "robot_state": state_summary,
        "needs_motion": needs_motion,
        "user_text": user_text,
    }

    # session 정보가 있으면 포함한다.
    # planner 는 recent_turns 로 이전 맥락을, last_joint/look/play 로 상태를 참조한다.
    if session_summary:
        payload["session"] = session_summary

    # repair 도메인 호출이면 직전 거부 사유를 실어 planner 가 설명/되묻게 한다.
    if repair_hint:
        payload["repair_hint"] = repair_hint

    # notify 도메인 호출이면 이미 실행된 연주 제어 내용을 실어 대사만 만들게 한다.
    if play_ctrl:
        payload["play_ctrl"] = play_ctrl

    return json.dumps(payload, ensure_ascii=False, indent=2)


def _sanitize_speech(speech: str) -> str:
    return sanitize_message(speech)


def parse_plan_response(response_text: str) -> Dict:
    """planner JSON 응답 → skill/op_cmd/speech 구조. 실패해도 기본형을 반환한다."""
    result = build_planner_failure_result()

    if not isinstance(response_text, str):
        return result

    try:
        response_data = json.loads(response_text)
    except json.JSONDecodeError:
        return result

    if not isinstance(response_data, dict):
        return result

    raw_skills = response_data.get("skills", response_data.get("s", []))
    raw_op_cmds = response_data.get(
        "op_cmd",
        response_data.get("commands", response_data.get("c", [])),
    )
    raw_speech = response_data.get("speech", response_data.get("t", FALLBACK_MESSAGE))
    raw_reason = response_data.get("reason", response_data.get("r", ""))
    # "q" 필드: clarification 질문. null 또는 생략이면 빈 문자열로 처리한다.
    raw_clarification = response_data.get("clarification_question", response_data.get("q", None))

    if isinstance(raw_skills, list):
        result["skills"] = [skill.strip() for skill in raw_skills if isinstance(skill, str) and skill.strip()]
    if isinstance(raw_op_cmds, list):
        result["op_cmd"] = [cmd.strip() for cmd in raw_op_cmds if isinstance(cmd, str) and cmd.strip()]
    result["speech"] = _sanitize_speech(raw_speech)
    if isinstance(raw_reason, str):
        result["reason"] = raw_reason.strip()
    if isinstance(raw_clarification, str) and raw_clarification.strip():
        result["clarification_question"] = raw_clarification.strip()
    else:
        result["clarification_question"] = ""

    return result


def enforce_intent_constraints(planner_output: Dict, classifier_output: Dict) -> Dict:
    """classifier 결과를 planner 뒤에서 재강제 — 습관적 gesture/look 을 정리한다."""
    normalized = {
        "skills": list(planner_output.get("skills", [])),
        "op_cmd": list(planner_output.get("op_cmd", planner_output.get("commands", []))),
        "speech": sanitize_message(planner_output.get("speech", FALLBACK_MESSAGE)),
        "reason": planner_output.get("reason", ""),
        "clarification_question": planner_output.get("clarification_question", ""),
    }

    intent = classifier_output.get("intent", "unknown")
    needs_motion = classifier_output.get("needs_motion", False)

    if not needs_motion:
        normalized["skills"] = []
        normalized["op_cmd"] = []
        return normalized

    planner_domain = select_planner_domain(classifier_output)
    allowed_categories = DOMAIN_ALLOWED_SKILL_CATEGORIES.get(planner_domain, set())
    if allowed_categories:
        normalized["skills"] = filter_skills_by_allowed_categories(
            normalized["skills"],
            allowed_categories,
        )

    if intent == "play_request":
        # 서버 PLAY 가 준비 자세를 내부 처리하므로 연주 명령만 허용한다.
        allowed_prefixes = ("PLAY|",)
        normalized["op_cmd"] = [
            command for command in normalized["op_cmd"] if command.startswith(allowed_prefixes)
        ]
    elif intent == "ctrl_request":
        # ctrl 도메인 책임 = 멈춤/재개/속도. 홈 복귀는 motion 도메인 몫이다.
        allowed_prefixes = ("PAUSE", "RESUME", "PLAY_CTRL|speed")
        normalized["op_cmd"] = [
            command for command in normalized["op_cmd"] if command.startswith(allowed_prefixes)
        ]
    elif intent == "motion_request":
        allowed_prefixes = ("MOVE|", "LOOK|", "GESTURE|", "POSE|")
        normalized["op_cmd"] = [
            command for command in normalized["op_cmd"] if command.startswith(allowed_prefixes)
        ]
    elif intent == "status_question":
        normalized["skills"] = []
        normalized["op_cmd"] = []
    elif intent == "chat" and not needs_motion:
        normalized["skills"] = []
        normalized["op_cmd"] = []

    return normalized
