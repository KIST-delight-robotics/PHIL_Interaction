# phil_robot/pipeline/session.py
"""세션 단기 기억 — phil_brain 이 소유하고 매 턴 update_session() 으로 갱신 (LLM 미사용)."""

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .command_validator import has_actionable_motion_command

MAX_HISTORY_TURNS = 5
PLANNER_VISIBLE_TURNS = 3   # planner input 에 노출할 턴 수 (token 예산)

# 실행 명령 없이 끝나면 cross-turn recovery 대상이 되는 intent
_ACTIONABLE_INTENTS = {"motion_request", "play_request"}


def _needs_action(classifier_output: Dict) -> bool:
    intent = classifier_output.get("intent", "")
    return intent in _ACTIONABLE_INTENTS or bool(classifier_output.get("needs_motion"))


@dataclass
class TurnRecord:
    """한 턴의 대화 기록: 사용자 발화 + 필의 응답."""
    user_text: str
    phil_speech: str


@dataclass
class SessionContext:
    """세션 단기 기억: history / last_*(동작 상태) / recovery_*(복구 대기)."""

    history: List[TurnRecord] = field(default_factory=list)

    # 마지막 동작 상태 — "거기서 더", "아까처럼" 해석용
    last_intent: str = ""
    last_joint: Optional[str] = None
    last_angle: Optional[float] = None
    last_look: Optional[str] = None     # 예: "LOOK|30|0"
    last_play: Optional[str] = None     # 곡 코드 (예: "TI")
    last_speech: str = ""

    # cross-turn recovery — 미해결 동작 요청을 다음 턴에 잇는다. 한도는 robot_fsm.MAX_RECOVERY.
    recovery_count: int = 0
    pending_intent: Optional[str] = None       # 원래 발화 ("허리 돌려")
    pending_classifier: Optional[Dict] = None  # 원래 classifier 결과 (continuation 재사용)

    # TODO: pending_task(조건부 작업) / user_name / user_preferences 확장 예정


def update_session(
    ctx: SessionContext,
    user_text: str,
    classifier_output: Dict,
    validated,
) -> SessionContext:
    """턴 종료 후 히스토리/마지막 상태/recovery 를 갱신한다 (validated: ValidatedPlan)."""
    ctx.history.append(TurnRecord(user_text=user_text, phil_speech=validated.speech))
    if len(ctx.history) > MAX_HISTORY_TURNS:
        ctx.history = ctx.history[-MAX_HISTORY_TURNS:]

    ctx.last_intent = classifier_output.get("intent", "")
    ctx.last_speech = validated.speech

    for cmd in validated.valid_op_cmds:
        if cmd.startswith("MOVE|"):
            # 첫 (관절,각) 쌍만 기억한다
            cmd_args = cmd.split("|")
            if len(cmd_args) >= 3:
                try:
                    ctx.last_joint = cmd_args[1]
                    ctx.last_angle = float(cmd_args[2])
                except ValueError:
                    pass
        elif cmd.startswith("LOOK|"):
            ctx.last_look = cmd
        elif cmd.startswith("PLAY|"):
            ctx.last_play = cmd.split("|", 1)[1]

    # actionable 인데 실행 명령이 없으면 미해결 → recovery 진행, 그 외엔 리셋
    unresolved = _needs_action(classifier_output) and not has_actionable_motion_command(
        validated.valid_op_cmds
    )
    if unresolved:
        ctx.recovery_count += 1
        if ctx.pending_intent is None:   # 첫 미해결 턴의 요청을 고정
            ctx.pending_intent = user_text
            ctx.pending_classifier = dict(classifier_output)
    else:
        ctx.recovery_count = 0
        ctx.pending_intent = None
        ctx.pending_classifier = None

    return ctx


def build_session_summary(ctx: SessionContext) -> Optional[Dict]:
    """planner input 에 넣을 session 요약. 빈 세션이면 None."""
    if (
        not ctx.history
        and not ctx.last_joint
        and not ctx.last_look
        and not ctx.last_play
        and not ctx.pending_intent
    ):
        return None

    summary: Dict = {}

    if ctx.history:
        summary["recent_turns"] = [
            {"user": t.user_text, "phil": t.phil_speech}
            for t in ctx.history[-PLANNER_VISIBLE_TURNS:]
        ]

    # 복구 중이면 원래 요청을 실어 planner 가 이번 발화와 합쳐 해석하게 한다
    if ctx.pending_intent:
        summary["pending_intent"] = ctx.pending_intent

    if ctx.last_joint and ctx.last_angle is not None:
        summary["last_joint"] = ctx.last_joint
        summary["last_angle"] = ctx.last_angle
    if ctx.last_look:
        summary["last_look"] = ctx.last_look
    if ctx.last_play:
        summary["last_play"] = ctx.last_play

    return summary if summary else None
