"""
재사용 가능한 상위 동작(skill)을 정의한다 (카탈로그 원본: data/skills.json).

LLM이 매번 low-level command 문자열을 직접 조립하지 않아도 되도록,
자주 쓰는 동작 시퀀스를 데이터로 고정해 안정성을 높인다.

skills.json 작성 규칙:
- 명령 문법은 Phil-drum-robot 와이어 프로토콜(`|` opcode)을 그대로 쓴다.
- LOOK 좌표: 정면 pan 0 / tilt 0, pan 은 왼쪽 양수, tilt 는 아래 양수.
- 팔 자세는 다중 (관절,각) 쌍 하나의 MOVE 로 보내 동시에 움직인다 (마지막 인자는 이동시간).
"""

from typing import Dict, List, Set, Tuple

from .data_files import load_data_json

SKILL_LIBRARY: Dict[str, Dict[str, object]] = load_data_json("skills.json")["skills"]


def describe_skills_for_prompt() -> str:
    """planner 프롬프트에 넣기 쉬운 skill 카탈로그 문자열을 만든다."""
    lines: List[str] = []
    for skill_name in sorted(SKILL_LIBRARY.keys()):
        metadata = SKILL_LIBRARY[skill_name]
        lines.append(
            f"- {skill_name} ({metadata['category']}): {metadata['description']}"
        )
    return "\n".join(lines)


def get_skill_categories(skill_names: List[str]) -> Set[str]:
    """skill 이름 목록에 포함된 카테고리 집합을 반환한다."""
    categories: Set[str] = set()
    for skill_name in skill_names:
        metadata = SKILL_LIBRARY.get(skill_name)
        if metadata:
            categories.add(str(metadata.get("category", "")))
    return categories


def filter_skills_by_allowed_categories(skill_names: List[str], allowed_categories: Set[str]) -> List[str]:
    """허용 카테고리에 맞는 skill 만 남긴다."""
    filtered: List[str] = []
    for skill_name in skill_names:
        if _is_direct_op_cmd(skill_name):
            filtered.append(skill_name)
            continue
        metadata = SKILL_LIBRARY.get(skill_name)
        if not metadata:
            continue
        if metadata.get("category") in allowed_categories:
            filtered.append(skill_name)
    return filtered


def expand_skills(skill_names: List[str]) -> Tuple[List[str], List[str]]:
    """skill 이름들을 로봇 명령 시퀀스로 펼친다 → (op_cmds, warnings)."""
    op_cmds: List[str] = []
    warnings: List[str] = []

    for skill_name in skill_names:
        metadata = SKILL_LIBRARY.get(skill_name)
        if _is_direct_op_cmd(skill_name):
            op_cmds.append(skill_name)
            continue
        if metadata is None:
            warnings.append(f"알 수 없는 skill 무시: {skill_name}")
            continue
        op_cmds.extend(list(metadata["op_cmd"]))

    return _deduplicate_consecutive_op_cmds(op_cmds), warnings


def _is_direct_op_cmd(skill_name: str) -> bool:
    if skill_name in {"PAUSE", "RESUME"}:
        return True

    direct_prefixes = ("MOVE|", "LOOK|", "GESTURE|", "PLAY|", "POSE|", "PLAY_CTRL|", "HIT|")
    return skill_name.startswith(direct_prefixes)


def _deduplicate_consecutive_op_cmds(op_cmds: List[str]) -> List[str]:
    """연속 중복 명령 제거 (예: look_forward + LOOK|0|0 → LOOK|0|0)."""
    deduped: List[str] = []
    for op_cmd in op_cmds:
        if deduped and deduped[-1] == op_cmd:
            continue
        deduped.append(op_cmd)
    return deduped
