"""
phil_robot per-turn FSM — 한 사용자 턴을 고정 step 체인 + repair 루프로 처리한다.

  preprocess → classify → state → direct_answer → (planner ⇄ validator) → execute → notify

- prefilter/direct_answer 적중 시 _shortcut 으로 LLM step 을 건너뛴다.
  단 validator/execute 는 항상 거친다 (rule-base 결과도 최종 명령/발화로 변환).
- validator 거부 시 사유(repair_hint)를 실어 repair 도메인 planner 로 재호출한다.
- 턴 사이 미해결 요청은 session 의 recovery 상태로 잇는다.
- 상세 설계/전환 기록: docs/LANGGRAPH_STATE_MACHINE_KR.md
"""

import threading
import time
from typing import TYPE_CHECKING, Callable, Dict, List, TypedDict

from .brain_pipeline import (
    build_direct_answer_plan,
    build_prefilter_plan,
    classify_step,
    planner_step,
)
from .command_validator import has_actionable_motion_command
from .exec_thread import Executor
from .failure import FALLBACK_MESSAGE
from .planner import PLANNER_DOMAIN_NOTIFY, PLANNER_DOMAIN_REPAIR, select_planner_domain
from .skills import get_skill_categories
from .state_adapter import adapt_robot_state
from .validator import build_validated_plan

if TYPE_CHECKING:
    from .validator import ValidatedPlan

# 턴 내 repair 재호출 한도 — 소진 시 fallback
MAX_REPAIR = 2

# cross-turn 복구 한도 — 초과 턴은 planner 없이 giveup 리셋
MAX_RECOVERY = 4
_GIVEUP_MESSAGE = "죄송해요, 잘 이해하지 못했어요. 처음부터 다시 말씀해 주세요."
_CANCEL_WORDS = {"취소", "취소해", "아니", "아니야", "됐어", "관둬", "안해", "안 해"}

# 빈 계획으로 끝나면 되묻기(repair/recovery) 대상인 intent
_ACTIONABLE_INTENTS = {"motion_request", "play_request"}


def _needs_action(classifier_output: dict) -> bool:
    intent = classifier_output.get("intent", "")
    return intent in _ACTIONABLE_INTENTS or bool(classifier_output.get("needs_motion"))


class PhilState(TypedDict, total=False):
    """한 턴의 step 사이를 굴러다니는 데이터 묶음 — 각 step 이 필요한 키만 갱신한다."""
    user_text: str

    _shortcut: bool        # prefilter/direct_answer 가 planner_output 을 이미 만듦
    _recovery: bool        # 복구 continuation (classify 건너뜀)
    repair_attempt: int    # repair 재호출 횟수 (디버그용)
    repair_hint: dict      # validator 거부 사유 — 비어있지 않으면 repair 호출

    classifier_output: dict
    planner_domain: str
    robot_state: dict
    planner_output: dict

    plan_type: str        # "motion" | "play" | "stop" | "chat" | "none"
    speech: str
    commands: List[str]
    validated: object     # ValidatedPlan
    debug: dict


# ── plan_type 판단 ──────────────────────────────────────────────────

# 실행 후 홈 복귀 대상인 skill 카테고리
_HOME_RETURN_CATEGORIES = {"social", "posture"}

# 시선 명령만 있으면 홈 복귀 제외
_LOOK_ONLY_PREFIXES = ("LOOK|",)


def _infer_plan_type(validated_plan: "ValidatedPlan", classifier_intent: str) -> str:
    """plan_type 결정 — motion(홈 복귀 대상) / play / stop / chat / none."""
    # skill 기반 판단
    skill_names = list(getattr(validated_plan, "skills", []) or [])
    if skill_names:
        categories = get_skill_categories(skill_names)
        if "play" in categories:
            return "play"
        if _HOME_RETURN_CATEGORIES & categories:
            return "motion"

    # 직접 op_cmd 기반 판단
    cmds = list(getattr(validated_plan, "valid_op_cmds", []) or [])
    if not cmds:
        return "chat" if classifier_intent in ("chat", "status_question") else "none"

    has_play = any(c.startswith("PLAY|") for c in cmds)
    has_stop = any(c in ("PAUSE", "RESUME") or c.startswith("PLAY_CTRL|") for c in cmds)
    has_move = any(c.startswith(("MOVE|", "GESTURE|", "POSE|", "HIT|")) for c in cmds)
    has_look_only = all(c.startswith(_LOOK_ONLY_PREFIXES) for c in cmds)

    if has_play:
        return "play"
    if has_stop:
        return "stop"
    if has_move:
        return "motion"
    if has_look_only:
        return "none"   # 시선만 바꾼 것은 홈 복귀 제외

    return classifier_intent if classifier_intent in ("chat", "stop") else "none"


def _extract_commands(validated_plan: "ValidatedPlan") -> List[str]:
    """전송 목록 = 검증 통과 명령 전부."""
    return list(getattr(validated_plan, "valid_op_cmds", []) or [])


# ── step 빌더 (클로저로 의존성 주입) ─────────────────────────────────

def make_preprocess_step(get_state_fn: Callable):
    """preprocess: prefilter 적중 시 _shortcut. 턴당 GET_STATUS 는 여기 1회뿐 — 이후 step 이 재사용."""
    def preprocess(state: PhilState) -> PhilState:
        robot_state = adapt_robot_state(get_state_fn())
        prefilter = build_prefilter_plan(state["user_text"], robot_state)
        if prefilter is None:
            return {**state, "robot_state": robot_state}

        classifier_output, planner_output, planner_domain = prefilter
        return {
            **state,
            "robot_state": robot_state,
            "classifier_output": classifier_output,
            "planner_output": planner_output,
            "planner_domain": planner_domain,
            "_shortcut": True,
        }

    return preprocess


def make_classify_step(classifier_model: str):
    """classify: classifier LLM 호출. shortcut/recovery continuation 이면 통과."""
    def classify(state: PhilState) -> PhilState:
        if state.get("_shortcut") or state.get("_recovery"):
            return state

        classifier_output, diag = classify_step(
            state["user_text"], classifier_model, capture_metrics=True
        )
        planner_domain = select_planner_domain(classifier_output)

        debug = dict(state.get("debug", {}))
        debug["classifier_input"] = diag["classifier_input"]
        debug["classifier_duration_sec"] = diag["duration_sec"]
        debug["classifier_metrics"] = diag["metrics"]

        return {
            **state,
            "classifier_output": classifier_output,
            "planner_domain": planner_domain,
            "debug": debug,
        }

    return classify


def make_state_step(get_state_fn: Callable):
    """state: preprocess 스냅샷 재사용. preprocess 를 건너뛴 턴(giveup)에서만 fetch."""
    def fetch_state(state: PhilState) -> PhilState:
        if state.get("robot_state"):
            return state
        robot_state = adapt_robot_state(get_state_fn())
        return {**state, "robot_state": robot_state}

    return fetch_state


def make_direct_answer_step():
    """direct_answer: 상태/정체/레퍼토리 직답 shortcut. shortcut/recovery 면 건너뜀."""
    def direct_answer(state: PhilState) -> PhilState:
        if state.get("_shortcut") or state.get("_recovery"):
            return state

        direct_plan = build_direct_answer_plan(
            state["user_text"], state["classifier_output"], state["robot_state"]
        )
        if direct_plan is not None:
            return {**state, "planner_output": direct_plan, "_shortcut": True}
        return state

    return direct_answer


def make_planner_step(planner_model: str, get_session: Callable):
    """planner: repair_hint 있으면 repair 도메인 — 거부된 shortcut 도 설명이 필요해 shortcut 보다 우선."""
    def planner(state: PhilState) -> PhilState:
        repair_hint = state.get("repair_hint") or {}

        if repair_hint:
            domain = PLANNER_DOMAIN_REPAIR
            repair_attempt = state.get("repair_attempt", 0) + 1
        elif state.get("_shortcut"):
            return state
        else:
            domain = state["planner_domain"]
            repair_attempt = state.get("repair_attempt", 0)

        session = get_session()
        planner_output, diag = planner_step(
            state["robot_state"],
            state["user_text"],
            state["classifier_output"],
            domain,
            session,
            planner_model,
            capture_metrics=True,
            repair_hint=(repair_hint or None),
        )

        debug = dict(state.get("debug", {}))
        debug["planner_input"] = diag["planner_input"]
        # repair 로 여러 번 부르면 planner 시간을 누적해 둔다.
        debug["planner_duration_sec"] = debug.get("planner_duration_sec", 0.0) + diag["duration_sec"]
        debug["planner_metrics"] = diag["metrics"]

        return {
            **state,
            "planner_output": planner_output,
            "planner_domain": domain,
            "repair_attempt": repair_attempt,
            "repair_hint": {},   # 소비됨. validator 가 또 거부하면 새로 채운다.
            "debug": debug,
        }

    return planner


def make_validator_step():
    """validator: build_validated_plan 으로 speech/commands/plan_type 확정 + repair_hint 추출."""
    def validator(state: PhilState) -> PhilState:
        validated = build_validated_plan(
            user_text=state["user_text"],
            robot_state=state["robot_state"],
            classifier_output=state["classifier_output"],
            planner_output=state["planner_output"],
        )
        classifier_intent = state["classifier_output"].get("intent", "none")
        plan_type = _infer_plan_type(validated, classifier_intent)
        commands = _extract_commands(validated)

        # 거부 사유가 있으면 repair 루프로 보낼 hint 를 PhilState 에 싣는다.
        hint = getattr(validated, "repair_hint", None)
        repair_hint: Dict = {}
        if hint is not None:
            repair_hint = {
                "failure_code": hint.failure_code,
                "reason": hint.reason,
                "rejected": list(hint.rejected),
            }

        # missing-info: actionable 인데 빈 계획이면 repair 로 되묻는다.
        # repair 도메인 출력은 빈 계획이 정상이라 제외 (무한 루프 방지).
        if (
            not repair_hint
            and state.get("planner_domain") != PLANNER_DOMAIN_REPAIR
            and _needs_action(state["classifier_output"])
            and not has_actionable_motion_command(getattr(validated, "valid_op_cmds", []))
        ):
            repair_hint = {
                "failure_code": "missing_info",
                "reason": "요청한 동작에 필요한 정보(목표 각도/대상/곡)가 부족합니다.",
                "rejected": [],
            }

        return {
            **state,
            "validated": validated,
            "speech": validated.speech or "",
            "commands": commands,
            "plan_type": plan_type,
            "repair_hint": repair_hint,
        }

    return validator


def make_fallback_step():
    """fallback: repair 소진 — 명령을 버리고 안전 문구만 남긴다."""
    def fallback(state: PhilState) -> PhilState:
        return {
            **state,
            "speech": FALLBACK_MESSAGE,
            "commands": [],
            "plan_type": "none",
            "repair_hint": {},
        }

    return fallback


def home(bot, get_state_fn: Callable) -> None:
    """
    [비활성] is_fixed 폴링 홈 복귀 워처 — 신 서버는 "움직이는 중" 신호가 없어 감지 불가.
    필요해지면 서버 상태 노출 또는 시간 기반으로 재설계한다.
    """
    def _watch():
        # 1. 움직임 시작 대기 (is_fixed=False, 최대 1.5초). 미감지면 홈 복귀 건너뜀.
        deadline = time.monotonic() + 1.5
        started = False
        while time.monotonic() < deadline:
            if not get_state_fn().get("is_fixed", True):
                started = True
                break
            time.sleep(0.05)
        if not started:
            print("[Executor] 움직임 시작 미감지 → 홈 복귀 건너뜀")
            return

        # 2. 정지 대기 (is_fixed=True 첫 회, 최대 20초)
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            if get_state_fn().get("is_fixed", True):
                break
            time.sleep(0.05)

        # 3. 홈 복귀 전송
        print("[Executor] 동작 완료 확인 → 홈 자세로 복귀")
        try:
            bot.send_command("POSE|home")
        except Exception as exc:
            print(f"⚠️ 홈 복귀 명령 전송 실패: {exc}")

    threading.Thread(target=_watch, daemon=True).start()


def make_execute_step(executor: Executor, bot, get_state_fn: Callable):
    """execute: 명령을 Executor 로 비동기 전송 (Home Watcher 비활성 — home() 참조)."""
    def execute(state: PhilState) -> PhilState:
        commands = state.get("commands", [])

        if not commands:
            return state

        def on_done():
            pass  # 홈 복귀 워처 비활성 (재설계 전까지)

        executor.exec_cmd(commands=commands, on_done=on_done)
        return state

    return execute


def make_notify_step(planner_model: str):
    """
    notify: prefilter 가 전송까지 끝낸 연주 제어의 대사만 LLM 으로 다시 만든다 (execute 뒤라 제어 지연 없음).
    repair 를 탔으면 play_ctrl 이 사라져 건너뛰고, LLM 실패/빈 문장이면 고정 문구를 유지한다.
    notify 출력의 명령은 enforce_intent_constraints 가 항상 비운다.
    """
    def notify(state: PhilState) -> PhilState:
        play_ctrl = dict((state.get("planner_output") or {}).get("play_ctrl") or {})
        if not play_ctrl:
            return state

        planner_output, diag = planner_step(
            state["robot_state"],
            state["user_text"],
            state["classifier_output"],
            PLANNER_DOMAIN_NOTIFY,
            None,
            planner_model,
            capture_metrics=True,
            play_ctrl=play_ctrl,
        )

        debug = dict(state.get("debug", {}))
        debug["notify_input"] = diag["planner_input"]
        debug["notify_duration_sec"] = diag["duration_sec"]
        debug["notify_metrics"] = diag["metrics"]

        notify_speech = (planner_output.get("speech") or "").strip()
        if not notify_speech or notify_speech == FALLBACK_MESSAGE:
            return {**state, "debug": debug}  # 고정 문구 유지

        validated = state.get("validated")
        if validated is not None:
            validated.speech = notify_speech  # session 히스토리에도 최종 대사가 남게 한다

        return {**state, "speech": notify_speech, "debug": debug}

    return notify


# ── run_turn 빌드 ────────────────────────────────────────────────────

def build_run_turn(
    bot,
    executor: Executor,
    get_session: Callable,
    get_state_fn: Callable,
    classifier_model: str,
    planner_model: str,
):
    """run_turn(user_text) 를 만들어 반환한다 — phil_brain 이 startup 에 한 번 호출."""
    preprocess = make_preprocess_step(get_state_fn)
    classify = make_classify_step(classifier_model)
    fetch_state = make_state_step(get_state_fn)
    direct_answer = make_direct_answer_step()
    planner = make_planner_step(planner_model, get_session)
    validator = make_validator_step()
    fallback = make_fallback_step()
    execute = make_execute_step(executor, bot, get_state_fn)
    notify = make_notify_step(planner_model)

    def run_turn(user_text: str) -> PhilState:
        # cross-turn 복구 상태를 turn 진입에서 읽는다 (지속 상태는 session 이 보관).
        session = get_session()
        recovery_count = session.recovery_count if session is not None else 0
        pending_intent = session.pending_intent if session is not None else None
        pending_classifier = session.pending_classifier if session is not None else None

        state: PhilState = {
            "user_text": user_text,
            "_shortcut": False,
            "_recovery": False,
            "repair_attempt": 0,
            "repair_hint": {},
            "classifier_output": {},
            "planner_domain": "",
            "planner_output": {},
            "debug": {},
        }

        if pending_intent and recovery_count >= MAX_RECOVERY:
            # 복구 한도 초과 → planner 없이 결정적 리셋 (chat 분류라 update_session 이 리셋)
            state["classifier_output"] = {"intent": "chat"}
            state["planner_output"] = {"skills": [], "op_cmd": [], "speech": _GIVEUP_MESSAGE, "reason": "recovery 한도 초과"}
            state["_shortcut"] = True
        else:
            state = preprocess(state)   # prefilter (pause/resume/인사)
            if not state["_shortcut"] and pending_intent:
                # 복구 진행 중이고 prefilter 도 아니다 → 원래 요청을 이어간다.
                if user_text.strip() in _CANCEL_WORDS:
                    state["classifier_output"] = {"intent": "chat"}
                    state["planner_output"] = {"skills": [], "op_cmd": [], "speech": "알겠습니다. 그 요청은 취소할게요.", "reason": "사용자 취소"}
                    state["_shortcut"] = True
                else:
                    # classify 건너뛰고 원래 classifier/도메인 재사용 (pending_intent 는 session_summary 로 전달)
                    state["classifier_output"] = dict(pending_classifier or {})
                    state["planner_domain"] = select_planner_domain(pending_classifier or {})
                    state["_recovery"] = True

        state = classify(state)        # shortcut/recovery 면 통과
        state = fetch_state(state)
        state = direct_answer(state)   # shortcut/recovery 면 통과

        # repair 루프 — 거부되면 사유 싣고 재호출, 소진 시(else) fallback
        for _ in range(MAX_REPAIR + 1):
            state = planner(state)
            state = validator(state)
            if not state["repair_hint"]:
                break
        else:
            state = fallback(state)

        state = execute(state)
        state = notify(state)   # play_ctrl 있을 때만 대사 재생성
        return state

    return run_turn
