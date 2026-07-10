"""
재사용 가능한 상위 동작(skill)을 정의한다.

LLM이 매번 low-level command 문자열을 직접 조립하지 않아도 되도록,
자주 쓰는 동작 시퀀스를 코드에 고정해 안정성을 높인다.
"""

from typing import Dict, List, Set, Tuple


# 명령 문법은 Phil-drum-robot 와이어 프로토콜(`|` opcode)을 그대로 쓴다.
# LOOK 좌표: 정면 pan 0 / tilt 0, pan 은 왼쪽 양수, tilt 는 아래 양수.
# 팔 자세는 다중 (관절,각) 쌍 하나의 MOVE 로 보내 동시에 움직인다 (마지막 인자는 이동시간).
SKILL_LIBRARY: Dict[str, Dict[str, object]] = {
    # 사회적 반응 / 인사
    "wave_hi": {
        "category": "social",
        "description": "손을 흔들며 밝게 인사한다.",
        "op_cmd": ["GESTURE|wave"],
    },
    "nod_yes": {
        "category": "social",
        "description": "가볍게 끄덕이며 긍정 반응을 보인다.",
        "op_cmd": ["GESTURE|nod"],
    },
    "shake_no": {
        "category": "social",
        "description": "고개를 좌우로 흔들며 부정 반응을 보인다.",
        "op_cmd": ["GESTURE|shake"],
    },
    "happy_react": {
        "category": "social",
        "description": "기쁜 제스처와 표정으로 반응한다.",
        "op_cmd": ["GESTURE|happy"],
    },
    "celebrate": {
        "category": "social",
        "description": "만세 동작으로 크게 기쁨을 표현한다.",
        "op_cmd": ["GESTURE|hurray"],
    },
    # 시선 계열
    "look_forward": {
        "category": "visual",
        "description": "정면을 바라본다.",
        "op_cmd": ["LOOK|0|0"],
    },
    "look_left": {
        "category": "visual",
        "description": "왼쪽을 바라본다.",
        "op_cmd": ["LOOK|30|0"],
    },
    "look_right": {
        "category": "visual",
        "description": "오른쪽을 바라본다.",
        "op_cmd": ["LOOK|-30|0"],
    },
    "look_up": {
        "category": "visual",
        "description": "위쪽을 바라본다.",
        "op_cmd": ["LOOK|0|-20"],
    },
    "look_down": {
        "category": "visual",
        "description": "아래쪽을 바라본다.",
        "op_cmd": ["LOOK|0|20"],
    },
    # 자세 / 전환
    "arm_up": {
        "category": "posture",
        "description": "양팔을 들어 올린다.",
        "op_cmd": [
            "MOVE|right_shoulder_2|58|left_shoulder_2|58|right_elbow|95|left_elbow|95|right_wrist|0|left_wrist|0|3.0",
        ],
    },
    "arm_down": {
        "category": "posture",
        "description": "양팔을 아래 자세로 내린다.",
        "op_cmd": [
            "MOVE|right_shoulder_2|0|left_shoulder_2|0|right_elbow|20|left_elbow|20|3.0",
        ],
    },
    "left_arm_up": {
        "category": "posture",
        "description": "왼팔을 들어 올린다.",
        "op_cmd": ["MOVE|left_shoulder_2|58|left_elbow|95|left_wrist|0|3.0"],
    },
    "right_arm_up": {
        "category": "posture",
        "description": "오른팔을 들어 올린다.",
        "op_cmd": ["MOVE|right_shoulder_2|58|right_elbow|95|right_wrist|0|3.0"],
    },
    "left_arm_down": {
        "category": "posture",
        "description": "왼팔을 아래 자세로 내린다.",
        "op_cmd": ["MOVE|left_shoulder_2|0|left_elbow|20|3.0"],
    },
    "right_arm_down": {
        "category": "posture",
        "description": "오른팔을 아래 자세로 내린다.",
        "op_cmd": ["MOVE|right_shoulder_2|0|right_elbow|20|3.0"],
    },
    "arms_out": {
        "category": "posture",
        "description": "양팔을 옆으로 벌린다.",
        "op_cmd": [
            "MOVE|right_shoulder_1|30|left_shoulder_1|150|right_shoulder_2|10|left_shoulder_2|10"
            "|right_elbow|95|left_elbow|95|right_wrist|0|left_wrist|0|3.0",
        ],
    },
    "left_arm_out": {
        "category": "posture",
        "description": "왼팔을 옆으로 벌린다.",
        "op_cmd": ["MOVE|left_shoulder_1|150|left_shoulder_2|10|left_elbow|95|left_wrist|0|3.0"],
    },
    "right_arm_out": {
        "category": "posture",
        "description": "오른팔을 옆으로 벌린다.",
        "op_cmd": ["MOVE|right_shoulder_1|30|right_shoulder_2|10|right_elbow|95|right_wrist|0|3.0"],
    },
    "ready_pose": {
        "category": "posture",
        "description": "ready 자세로 전환한다.",
        "op_cmd": ["POSE|ready"],
    },
    "idle_home": {
        "category": "posture",
        "description": "휴식 자세로 돌아간다.",
        "op_cmd": ["POSE|home"],
    },
    # 연주 묶음 — 서버 PLAY 가 준비 자세를 내부 처리하므로 연주 명령 하나만 낸다.
    "play_ti": {
        "category": "play",
        "description": "This Is Me 연주를 시작한다.",
        "op_cmd": ["PLAY|TI"],
    },
    "play_ty": {
        "category": "play",
        "description": "그대에게 연주를 시작한다.",
        "op_cmd": ["PLAY|TY"],
    },
    "play_bi": {
        "category": "play",
        "description": "Baby I Need You 연주를 시작한다.",
        "op_cmd": ["PLAY|BI"],
    },
    "play_bf": {
        "category": "play",
        "description": "기본 필인 패턴을 연주한다.",
        "op_cmd": ["PLAY|BF"],
    },
    "play_ds": {
        "category": "play",
        "description": "드럼 솔로를 연주한다.",
        "op_cmd": ["PLAY|DS"],
    },
    "play_ws": {
        "category": "play",
        "description": "왜그래 연주를 시작한다.",
        "op_cmd": ["PLAY|WS"],
    },
    # 시스템
    "shutdown_system": {
        "category": "system",
        "description": "종료 자세로 이동한다.",
        "op_cmd": ["POSE|shutdown"],
    },
}


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
    """
    planner가 고른 skill 이름을 실제 로봇 명령 시퀀스로 펼친다.

    반환값:
    - op_cmds: 전송 가능한 문자열 명령 목록
    - warnings: 알 수 없는 skill 등 디버그용 경고
    """
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
    """
    planner 가 같은 skill 을 반복하거나 skill 과 동등한 low-level 을 겹쳐 내면 한 번만 남긴다.
    예: look_forward + LOOK|0|0 -> LOOK|0|0, LOOK|0|0 -> LOOK|0|0
    """
    deduped: List[str] = []
    for op_cmd in op_cmds:
        if deduped and deduped[-1] == op_cmd:
            continue
        deduped.append(op_cmd)
    return deduped
