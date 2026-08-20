"""plan 단위 validator — skill 전개 → 상대 동작 해석 → 명령 검증 → 메시지 보정."""

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .command_validator import (
    has_actionable_motion_command,
    validate_commands,
)
from .motion_resolver import resolve_motion_commands
from .skills import expand_skills
from .state_adapter import block_reason_of


@dataclass
class RepairHint:
    """거부 사유 — repair planner 입력에 실린다. failure_code 는 흐름 제어용, reason 은 설명 재료."""

    failure_code: str = ""
    reason: str = ""
    rejected: List[str] = field(default_factory=list)


@dataclass
class ValidatedPlan:
    """validator 를 통과한 최종 실행 단위 — executor 가 바로 소비한다."""

    skills: List[str] = field(default_factory=list)
    raw_op_cmds: List[str] = field(default_factory=list)
    expanded_op_cmds: List[str] = field(default_factory=list)
    resolved_op_cmds: List[str] = field(default_factory=list)
    valid_op_cmds: List[str] = field(default_factory=list)
    rejected_op_cmds: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    speech: str = ""
    reason: str = ""
    repair_hint: Optional[RepairHint] = None   # 거부 시 채워짐 — run_turn 이 repair 루프로 보낸다


def _content_failure_code(warnings: List[str]) -> str:
    """상태 차단이 아닌 명령 내용 거부 사유를 거친 코드로 정규화한다."""
    text = " ".join(warnings)
    if "지정되지 않음" in text:
        return "missing_info"
    if "한계" in text or "범위 초과" in text:
        return "joint_limit"
    if "즉흥 장르" in text:
        return "unknown_genre"
    if "즉흥 bpm" in text:
        return "bpm_range"
    if "곡 코드" in text:
        return "unknown_song"
    if "연주 명령 차단" in text:
        return "play_state"
    if "연주 중이 아니" in text:
        return "not_playing"
    if "재개할 수 없" in text:
        return "cannot_resume"
    return "bad_command"


def build_validated_plan(
    user_text: str,
    robot_state: Dict,
    classifier_output: Dict,
    planner_output: Dict,
) -> ValidatedPlan:
    """classifier+planner 결과를 실행 가능한 plan 으로 확정한다."""
    skill_op_cmds, skill_warnings = expand_skills(planner_output.get("skills", []))
    planner_op_cmds = list(planner_output.get("op_cmd", planner_output.get("commands", [])))
    expanded_op_cmds = skill_op_cmds + planner_op_cmds

    resolution = resolve_motion_commands(user_text, expanded_op_cmds, robot_state)
    validation = validate_commands(resolution.op_cmds, robot_state)

    warnings = list(skill_warnings)
    warnings.extend(resolution.warnings)
    warnings.extend(validation.warnings)

    speech = planner_output.get("speech", "")

    # validator 는 speech 작가가 아니라 안전망 — 결정적 해석 결과만 반영하고,
    # 거부 안내는 repair 도메인 planner 가 만든다.
    if resolution.message_override:
        speech = resolution.message_override
    elif resolution.speech_override and has_actionable_motion_command(validation.valid_commands):
        speech = resolution.speech_override

    # 명령이 전부 거부돼 실행할 동작이 없으면 repair 사유를 만든다
    repair_hint = None
    if validation.rejected_commands and not has_actionable_motion_command(validation.valid_commands):
        block = block_reason_of(robot_state)
        failure_code = block if block != "none" else _content_failure_code(validation.warnings)
        repair_hint = RepairHint(
            failure_code=failure_code,
            reason="; ".join(w for w in validation.warnings if w),
            rejected=list(validation.rejected_commands),
        )

    return ValidatedPlan(
        skills=list(planner_output.get("skills", [])),
        raw_op_cmds=planner_op_cmds,
        expanded_op_cmds=expanded_op_cmds,
        resolved_op_cmds=list(resolution.op_cmds),
        valid_op_cmds=list(validation.valid_commands),
        rejected_op_cmds=list(validation.rejected_commands),
        warnings=warnings,
        speech=speech,
        reason=planner_output.get("reason", ""),
        repair_hint=repair_hint,
    )
