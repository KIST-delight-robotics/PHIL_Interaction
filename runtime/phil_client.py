# phil_client.py
"""
Phil-drum-robot 서버(TCP 1951, `|` opcode 프로토콜)용 클라이언트.

- 명령: pipeline 이 만든 와이어 명령(PLAY|TI, PAUSE, MOVE|... 등)을 그대로 전송 (fire-and-forget)
- 상태: 서버 push 가 없으므로 GET_STATUS 로 조회한다. 배경 폴링은 하지 않고
  턴 시작 시점에만 on-demand 로 요청한다 (fetch_state_snapshot) — 서버 로그 스팸 방지.
- 시작: 서버는 START -> (사람이 고정 키 제거) -> READY 순서를 요구한다.
  connect() 안에서 키보드 입력으로 이 핸드셰이크를 진행한다.
- 서버는 동시 1 클라이언트만 수락하므로 phil_brain 이 유일한 연결이어야 한다.
"""

import socket
import time
import threading
import json
import copy

from typing import Dict, Optional

ANGLE_UPDATE_DEADBAND_DEG = 0.2
ANGLE_LOG_DEADBAND_DEG = 0.5
STATUS_REPLY_TIMEOUT_SEC = 2.0

# GET_STATUS 응답의 관절각 순서 (= motors.json id 순서, drumrobot_client/main.py JOINTS 와 동일)
JOINT_ORDER = [
    "waist",
    "right_shoulder_1",
    "left_shoulder_1",
    "right_shoulder_2",
    "right_elbow",
    "left_shoulder_2",
    "left_elbow",
    "right_wrist",
    "left_wrist",
    "right_pedal",
    "left_pedal",
    "head_yaw",
    "head_pitch",
]

# 서버 state 문자열 -> 기존 숫자 게이트 호환값
# (0: idle, 2: playing, 6: 차단 상태 — command_validator 의 상태 게이트 유지용)
STATE_CODE_MAP = {
    "STANDBY": 0,
    "INIT": 0,
    "IDLE": 0,
    "PLAYING": 2,
    "SHUTTINGDOWN": 6,
}

# 안전키(고정 키) 제거 완료로 간주하는 상태 — START/READY 핸드셰이크 이후
KEY_REMOVED_STATES = {"IDLE", "PLAYING", "SHUTTINGDOWN"}

# 로봇의 상태를 기억할 전역 변수 (초기값은 robot_poses.json 의 home 포즈)
ROBOT_STATE = {
    "state": 0,          # 0: Idle, 2: Play, 6: 차단 (STATE_CODE_MAP 참조)
    "state_str": "STANDBY",  # 서버 원문 상태 문자열
    "bpm": 100,          # 서버 미제공 — 기본값 유지
    "is_fixed": True,    # 서버 미제공 — 항상 True
    "current_song": "None",
    "last_action": "None",
    "is_lock_key_removed": False,  # STANDBY/INIT 이면 False
    "play_speed": 1.0,   # 연주 속도 배율 (PLAY_CTRL|speed 반영값)
    "current_angles": {
        "waist": 10.0,
        "right_shoulder_1": 90.0, "left_shoulder_1": 90.0,
        "right_shoulder_2": 0.0, "left_shoulder_2": 0.0,
        "right_elbow": 90.0, "left_elbow": 90.0,
        "right_wrist": 70.0, "left_wrist": 70.0,
        "right_pedal": 0.0, "left_pedal": 0.0,
        "head_yaw": 0.0, "head_pitch": 0.0,
    },
}
STATE_LOCK = threading.Lock()


def get_robot_state_json():
    """현재 로봇 상태를 LLM 프롬프트용 JSON 문자열로 반환"""
    return json.dumps(get_robot_state_snapshot(), ensure_ascii=False)


def get_robot_state_snapshot():
    """수신 스레드와 충돌하지 않도록 현재 상태의 스냅샷을 복사해 반환"""
    with STATE_LOCK:
        return copy.deepcopy(ROBOT_STATE)


def parse_status(line: str) -> Optional[Dict]:
    """
    STATUS|<state>|<q0>..<q12>|<speed>|<pause_valid>|<pause_id>|<pause_bar>
    를 ROBOT_STATE 갱신용 dict 로 번역한다. 형식이 아니면 None.
    재개 지점(pause_*) 필드는 서버 내부 판단용이라 brain 은 읽지 않는다.
    (구형 서버의 관절각+speed 만 있는 응답도 허용한다.)
    """
    parts = [p.strip() for p in line.strip().split("|")]
    if not parts or parts[0] != "STATUS" or len(parts) < 2:
        return None

    state_str = parts[1]
    fields = parts[2:]

    play_speed = 1.0
    joint_count = len(JOINT_ORDER)

    if len(fields) == joint_count + 4:
        # 신형: 관절각 13 + speed + pause 3필드(무시)
        try:
            play_speed = float(fields[-4])
        except ValueError:
            play_speed = 1.0
        angle_fields = fields[:-4]
    elif len(fields) == joint_count + 1:
        # 구형: 관절각 13 + speed
        try:
            play_speed = float(fields[-1])
        except ValueError:
            play_speed = 1.0
        angle_fields = fields[:-1]
    else:
        return None

    current_angles: Dict[str, float] = {}
    for joint_name, raw_angle in zip(JOINT_ORDER, angle_fields):
        try:
            current_angles[joint_name] = float(raw_angle)
        except ValueError:
            continue

    return {
        "state": STATE_CODE_MAP.get(state_str, 0),
        "state_str": state_str,
        "is_lock_key_removed": state_str in KEY_REMOVED_STATES,
        "is_fixed": True,
        "current_angles": current_angles,
        "play_speed": play_speed,
    }


def _merge_angle_state(previous_angles, incoming_angles, deadband_deg):
    """
    미세한 각도 노이즈는 이전 값을 유지해 Python 쪽 상태도 덜 흔들리게 만든다.
    """
    merged_angles = copy.deepcopy(previous_angles) if isinstance(previous_angles, dict) else {}
    if not isinstance(incoming_angles, dict):
        return merged_angles

    for joint_name, incoming_value in incoming_angles.items():
        previous_value = merged_angles.get(joint_name)
        if (
            isinstance(previous_value, (int, float))
            and isinstance(incoming_value, (int, float))
            and abs(float(incoming_value) - float(previous_value)) < deadband_deg
        ):
            continue

        merged_angles[joint_name] = incoming_value

    return merged_angles


def _state_changed_meaningfully(previous_state, current_state):
    """
    출력용 비교는 각도 미세 변화에 둔감하게 만들어 로그 채터링을 줄인다.
    """
    if previous_state is None:
        return True

    tracked_keys = [
        "state", "state_str", "is_fixed", "current_song", "last_action",
        "is_lock_key_removed", "play_speed",
    ]
    for key in tracked_keys:
        if previous_state.get(key) != current_state.get(key):
            return True

    if current_state.get("state") == 2:
        return False

    previous_angles = previous_state.get("current_angles", {})
    current_angles = current_state.get("current_angles", {})
    joint_names = set(previous_angles.keys()) | set(current_angles.keys())
    for joint_name in joint_names:
        prev_value = previous_angles.get(joint_name)
        curr_value = current_angles.get(joint_name)
        if not isinstance(prev_value, (int, float)) or not isinstance(curr_value, (int, float)):
            if prev_value != curr_value:
                return True
            continue

        if abs(float(curr_value) - float(prev_value)) >= ANGLE_LOG_DEADBAND_DEG:
            return True

    return False


class RobotClient:

    # 클라이언트 소켓 생성자
    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.sock = None
        self.send_lock = threading.Lock()    # 한 소켓을 여러 스레드가 쓰므로 송신 직렬화
        self.status_lock = threading.Lock()  # GET_STATUS 요청-응답 왕복을 원자적으로 묶는다
        self._recv_buffer = ""               # STATUS 응답 부분 수신 버퍼
        self._last_printed = None            # 마지막으로 출력한 상태 (변경시에만 로그)
        self._sent_song = "None"             # 마지막으로 PLAY 를 보낸 곡 코드 (current_song 추적용)

    # 소켓 연결
    def connect(self):
        """로봇(C++) 서버에 연결될 때까지 재시도 후 START/READY 핸드셰이크 진행"""
        print(f"로봇 서버 ({self.host}:{self.port})에 연결 시도..")

        while True:
            try:
                self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.sock.settimeout(5)  # 타임아웃 5초 설정
                self.sock.connect((self.host, self.port))
                self.sock.settimeout(None)
                print(f"로봇 서버 ({self.host}:{self.port})에 연결되었습니다.")
                break

            except (socket.error, ConnectionRefusedError):
                self.sock.close()
                print("⏳ 로봇 서버 대기 중... (drumrobot_server 를 먼저 실행해주세요)")
                time.sleep(3)

        self._handshake()
        self.fetch_state_snapshot()   # 초기 상태 1회 조회 (이후는 턴 시작마다 on-demand)
        return True

    def _handshake(self):
        """서버 시작 절차: START -> (고정 키 제거) -> READY. 키보드로 진행한다."""
        while input("'start'를 입력하세요 > ").strip().lower() != "start":
            print("  start 를 입력해야 합니다.")
        self._send_line("START")

        while input("고정 키를 모두 제거한 후 'ready'를 입력하세요 > ").strip().lower() != "ready":
            print("  ready 를 입력해야 합니다.")
        self._send_line("READY")
        print("✅ 로봇 준비 완료 (IDLE). 이제 음성으로 명령할 수 있습니다.")

    # 상태 조회 (on-demand)
    def fetch_state_snapshot(self):
        """
        GET_STATUS 를 1회 요청해 ROBOT_STATE 를 갱신하고 스냅샷을 반환한다.
        run_turn 의 preprocess/state step 이 턴 시작 시점에 호출한다 (배경 폴링 없음).
        실패 시 마지막으로 알고 있던 스냅샷을 그대로 반환한다.
        """
        global ROBOT_STATE

        with self.status_lock:
            try:
                self._send_line("GET_STATUS")
                line = self._recv_line()
            except Exception as e:
                print(f"⚠️ 상태 조회 실패 (마지막 스냅샷 사용): {e}")
                return get_robot_state_snapshot()

        status_update = parse_status(line)
        if status_update is None:
            print(f"⚠️ 예기치 않은 상태 응답: {line.strip()}")
            return get_robot_state_snapshot()

        with STATE_LOCK:
            # 연주 중 각도는 계속 흔들리므로 갱신하지 않는다.
            if status_update["state"] == 2:
                status_update.pop("current_angles", None)
            else:
                status_update["current_angles"] = _merge_angle_state(
                    ROBOT_STATE.get("current_angles", {}),
                    status_update["current_angles"],
                    ANGLE_UPDATE_DEADBAND_DEG,
                )

            # current_song: PLAY 전송 기록 기반. 연주 중이 아니면 None 처리.
            if status_update["state_str"] == "PLAYING":
                status_update["current_song"] = self._sent_song
            else:
                status_update["current_song"] = "None"

            ROBOT_STATE.update(status_update)
            current_snapshot = copy.deepcopy(ROBOT_STATE)

        # 이전과 다르게 바뀐 경우에만 상태 갱신 메시지 출력
        if _state_changed_meaningfully(self._last_printed, current_snapshot):
            print(f"\n[상태 갱신] {current_snapshot}")
            self._last_printed = current_snapshot

        return current_snapshot

    def _recv_line(self):
        """STATUS 응답 한 줄(\\n 단위)을 수신한다. 응답은 GET_STATUS 에만 온다."""
        deadline = time.monotonic() + STATUS_REPLY_TIMEOUT_SEC
        while "\n" not in self._recv_buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("STATUS 응답 대기 시간 초과")
            self.sock.settimeout(remaining)
            try:
                data = self.sock.recv(4096).decode("utf-8")
            finally:
                self.sock.settimeout(None)
            if not data:
                raise ConnectionError("서버 연결 종료")
            self._recv_buffer += data

        line, self._recv_buffer = self._recv_buffer.split("\n", 1)
        return line

    def _send_line(self, line):
        """와이어 패킷 한 줄을 전송한다 (\\n 프레이밍 + 송신 직렬화)."""
        with self.send_lock:
            self.sock.sendall((line + "\n").encode())

    # 소켓 송신
    def send_command(self, cmd):
        """와이어 명령(예: 'PLAY|TI', 'PAUSE', 'POSE|home')을 그대로 전송한다."""
        if not self.sock:
            print("⚠️ 연결이 되어있지 않습니다.")
            return False

        wire_packet = (cmd or "").strip()
        if not wire_packet:
            return False

        try:
            self._send_line(wire_packet)
        except Exception as e:
            print(f"⚠️ 전송 실패: {e}")
            return False

        # current_song 추적: 서버 상태에는 연주 중 곡 코드가 없어 클라이언트가 기억한다.
        if wire_packet.startswith("PLAY|"):
            self._sent_song = wire_packet.split("|", 1)[1]

        # last_action: 서버가 알려주지 않으므로 마지막 전송 명령을 기억한다.
        # (motion_resolver 의 "거기서 더" 관절 문맥 추론이 이 값을 읽는다.)
        with STATE_LOCK:
            ROBOT_STATE["last_action"] = wire_packet

        return True

    # 소켓 닫기
    def close(self):
        if self.sock:
            self.sock.close()
        print("연결 종료")
