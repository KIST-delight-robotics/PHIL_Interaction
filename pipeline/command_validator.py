from dataclasses import dataclass, field
from typing import List

# 관절 한계 — Phil-drum-robot config/motors.json 과 동일 (drumrobot_client/main.py JOINTS 표)
JOINT_LIMITS = {
    "waist": (-90.0, 90.0),
    "right_shoulder_1": (0.0, 150.0),
    "left_shoulder_1": (30.0, 180.0),
    "right_shoulder_2": (-60.0, 90.0),
    "right_elbow": (0.0, 140.1),
    "left_shoulder_2": (-60.0, 90.0),
    "left_elbow": (0.0, 140.1),
    "right_wrist": (-90.0, 100.0),
    "left_wrist": (-90.0, 100.0),
    "right_pedal": (-90.0, 200.0),
    "left_pedal": (-90.0, 200.0),
    "head_yaw": (-90.0, 90.0),
    "head_pitch": (-100.0, 90.0),
}

# Phil-drum-robot config/play_list.json 의 곡 id
PLAY_CODES = {"TI", "TY", "BI", "BF", "DS", "WS"}
GESTURES = {"hi", "nod", "shake", "wave", "hurray", "happy"}
POSES = {"init", "home", "ready", "shutdown"}
# 연주 속도 배율 허용 범위 — 서버 PLAY_CTRL|speed 클램프와 동일
SPEED_SCALE_MIN = 0.5
SPEED_SCALE_MAX = 2.0
# LOOK 범위 — head_yaw / head_pitch 한계 (정면 0도)
LOOK_PAN_MIN, LOOK_PAN_MAX = JOINT_LIMITS["head_yaw"]
LOOK_TILT_MIN, LOOK_TILT_MAX = JOINT_LIMITS["head_pitch"]

MOTION_KEYWORDS = [
    "손",
    "팔",
    "허리",
    "손목",
    "발",
    "움직",
    "들어",
    "들어줘",
    "돌려",
    "봐",
    "고개",
    "인사",
    "흔들",
    "만세",
    "연주",
    "쳐",
]


@dataclass
class ValidationResult:
    valid_commands: List[str] = field(default_factory=list)
    rejected_commands: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


def validate_commands(commands, robot_state):
    """
    LLM이 만든 명령 문자열을 실행 전에 검증한다.
    parser는 형식 해석, validator는 실행 가능 여부 판단에 집중한다.
    """
    result = ValidationResult()

    for command in commands:
        normalized = normalize_command(command)
        if not normalized:
            continue

        is_valid, reason = validate_command(normalized, robot_state)
        if is_valid:
            result.valid_commands.append(normalized)
        else:
            result.rejected_commands.append(normalized)
            result.warnings.append(reason)

    result.warnings.extend(validate_sequence_rules(result.valid_commands))
    return result


def normalize_command(command):
    """
    opcode 는 대소문자를 표준화한다 (서버도 대소문자 무시).
    예: play|TI -> PLAY|TI, pause -> PAUSE
    """
    normalized = (command or "").strip()
    if not normalized:
        return ""

    opcode, sep, args = normalized.partition("|")
    if sep:
        return f"{opcode.upper()}|{args}"
    return opcode.upper()


def _split_args(command):
    return [token.strip() for token in command.split("|")[1:]]


def validate_command(command, robot_state):
    opcode = command.split("|", 1)[0]

    # 연주 제어 — 서버 수락 조건(PAUSE/PLAY_CTRL 은 PLAYING, RESUME 은 IDLE)을 미리 검사한다.
    if opcode == "PAUSE":
        return validate_playing_only(command, robot_state)
    if opcode == "PLAY_CTRL":
        return validate_play_ctrl_command(command, robot_state)
    if opcode == "RESUME":
        return validate_resume_command(command, robot_state)
    if opcode == "PLAY":
        return validate_play_command(command, robot_state)
    if opcode == "POSE":
        return validate_pose_command(command, robot_state)
    if opcode == "MOVE":
        return validate_move_command(command, robot_state)
    if opcode == "GESTURE":
        return validate_gesture_command(command, robot_state)
    if opcode == "LOOK":
        return validate_look_command(command, robot_state)
    return False, f"알 수 없는 명령 형식 차단: {command}"


def validate_playing_only(command, robot_state):
    """PAUSE / PLAY_CTRL — 서버가 PLAYING 상태에서만 수락하는 명령."""
    if robot_state.get("state", 0) != 2:
        return False, f"지금 연주 중이 아니라서 수행할 수 없음: {command}"
    return True, ""


def validate_play_ctrl_command(command, robot_state):
    """PLAY_CTRL|stop / PLAY_CTRL|speed|<배율> — 연주 중 제어."""
    allowed, reason = validate_playing_only(command, robot_state)
    if not allowed:
        return False, reason

    args = _split_args(command)
    if not args:
        return False, f"PLAY_CTRL 명령 파싱 실패: {command}"

    if args[0] == "stop":
        return True, ""

    if args[0] == "speed":
        if len(args) < 2:
            return False, f"PLAY_CTRL speed 배율 누락: {command}"
        try:
            scale = float(args[1])
        except ValueError:
            return False, f"PLAY_CTRL speed 파싱 실패: {command}"
        if not (SPEED_SCALE_MIN <= scale <= SPEED_SCALE_MAX):
            return False, f"속도 배율 범위({SPEED_SCALE_MIN}~{SPEED_SCALE_MAX}) 초과 차단: {command}"
        return True, ""

    return False, f"알 수 없는 PLAY_CTRL 하위 명령 차단: {command}"


def validate_resume_command(command, robot_state):
    """RESUME — IDLE 에서만 보낸다. 재개 지점 유무 판단은 서버 몫이다."""
    if robot_state.get("state", 0) != 0:
        return False, f"현재 state={robot_state.get('state')} 에서는 재개할 수 없음: {command}"
    return True, ""


def validate_play_command(command, robot_state):
    args = _split_args(command)
    if not args or args[0] not in PLAY_CODES:
        return False, f"알 수 없는 곡 코드 차단: {command}"

    allowed, reason = validate_motion_allowed(command, robot_state)
    if not allowed:
        return False, reason

    if robot_state.get("state", 0) != 0:
        return False, f"현재 state={robot_state.get('state')} 에서는 연주 명령 차단: {command}"

    return True, ""


def validate_pose_command(command, robot_state):
    args = _split_args(command)
    if not args or args[0] not in POSES:
        return False, f"알 수 없는 포즈 차단: {command}"

    return validate_motion_allowed(command, robot_state)


def validate_look_command(command, robot_state):
    allowed, reason = validate_motion_allowed(command, robot_state)
    if not allowed:
        return False, reason

    args = _split_args(command)
    if len(args) < 2:
        return False, f"LOOK 명령 파싱 실패: {command}"

    try:
        pan = float(args[0])
        tilt = float(args[1])
    except ValueError:
        return False, f"LOOK 명령 파싱 실패: {command}"

    if not (LOOK_PAN_MIN <= pan <= LOOK_PAN_MAX and LOOK_TILT_MIN <= tilt <= LOOK_TILT_MAX):
        return False, f"LOOK 범위 초과 차단: {command}"

    return True, ""


def validate_gesture_command(command, robot_state):
    allowed, reason = validate_motion_allowed(command, robot_state)
    if not allowed:
        return False, reason

    args = _split_args(command)
    if not args or args[0] not in GESTURES:
        return False, f"gesture 값 차단: {command}"

    return True, ""


def validate_move_command(command, robot_state):
    """
    MOVE|<joint>|<deg>|<joint>|<deg>|...|<move_time>
    (joint, deg) 쌍을 검증하고, 홀수로 남는 마지막 인자는 move_time 으로 해석한다.
    """
    allowed, reason = validate_motion_allowed(command, robot_state)
    if not allowed:
        return False, reason

    args = _split_args(command)
    if len(args) < 2:
        return False, f"MOVE 명령 파싱 실패: {command}"

    pair_index = 0
    any_pair = False
    while pair_index + 1 < len(args):
        joint_name = args[pair_index]
        angle_raw = args[pair_index + 1]

        if joint_name not in JOINT_LIMITS:
            return False, f"알 수 없는 관절 차단: {command}"

        # 각도 미지정(null) 은 지어낸 값이 아니라 "사용자가 안 알려줌" 신호 → 되묻기 대상.
        if angle_raw.strip().lower() in ("null", "none", ""):
            return False, f"목표 각도가 지정되지 않음: {command}"

        try:
            angle_deg = float(angle_raw)
        except ValueError:
            return False, f"MOVE 명령 파싱 실패: {command}"

        min_angle, max_angle = JOINT_LIMITS[joint_name]
        if not (min_angle <= angle_deg <= max_angle):
            return False, f"관절 한계 초과 차단: {command}"

        any_pair = True
        pair_index += 2

    if not any_pair:
        return False, f"MOVE 명령 파싱 실패: {command}"

    # 홀수로 남은 마지막 인자는 move_time
    if pair_index < len(args):
        try:
            move_time = float(args[pair_index])
        except ValueError:
            return False, f"MOVE 이동시간 파싱 실패: {command}"
        if move_time <= 0:
            return False, f"MOVE 이동시간 값 차단: {command}"

    return True, ""


def validate_motion_allowed(command, robot_state):
    if not robot_state.get("is_lock_key_removed", False):
        return False, f"안전 키 미해제로 동작 명령 차단: {command}"

    current_state = robot_state.get("state", 0)
    if current_state == 2:
        return False, f"연주 중이므로 동작 명령 차단: {command}"
    if current_state == 6:
        return False, f"에러 상태이므로 동작 명령 차단: {command}"

    if not robot_state.get("is_fixed", True):
        return False, f"이동 중이므로 동작 명령 차단: {command}"

    return True, ""


def validate_sequence_rules(commands):
    """
    현재는 강제 차단보다 경고 위주로 둔다.
    """
    warnings = []

    consecutive_moves = 0
    for command in commands:
        if command.startswith("MOVE|"):
            consecutive_moves += 1
        else:
            consecutive_moves = 0

        if consecutive_moves >= 3:
            warnings.append("MOVE 명령이 연속으로 길게 이어집니다.")
            break

    return warnings


def user_text_requests_motion(user_text):
    """아주 가벼운 규칙 기반 감지기로 물리 동작 요청을 추정한다."""
    normalized_text = (user_text or "").strip()
    return any(keyword in normalized_text for keyword in MOTION_KEYWORDS)


def has_actionable_motion_command(commands):
    """
    실제로 로봇 자세/행동을 바꾸는 명령이 하나라도 남아있는지 본다.
    """
    for command in commands:
        if command.startswith(("MOVE|", "GESTURE|", "LOOK|", "PLAY|", "POSE|", "HIT|")):
            return True
    return False
