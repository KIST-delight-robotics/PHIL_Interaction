# CONTRACTS — 컴포넌트 간 인터페이스 계약 (단일 소스)

> **이 파일은 단일 소스(single source of truth)다.**
> `phil-brain`(LLM) / `phil-controller`(C++ 제어기) / `phil-sil`(SIL) 세 레포에
> **같은 내용으로 복사**되어 들어간다. 한 곳을 고치면 나머지 두 곳도 같이 고친다.
> 여기 적힌 값은 추출 시점의 코드에서 뽑은 것이며, **최종 권위는 항상 각 레포의 코드**다.
> 코드와 이 문서가 어긋나면 코드를 믿고, 이 문서를 갱신하라.

이 프로젝트는 세 개의 독립 프로세스로 나뉜다. 각 프로세스는 아래 두 계약(Contract A, B)으로만
서로 연결된다. **계약만 지키면 각 컴포넌트는 독립적으로 교체·재작성할 수 있다.**

```text
┌─────────────┐   Contract A               ┌────────────────────┐   Contract B           ┌──────────────┐
│  phil-brain │  TCP 1951 (`|` opcode)      │  phil-controller   │  (제어기 하드웨어 계층)  │   하드웨어    │
│  (LLM brain)│ ─────────────────────────▶  │ (Phil-drum-robot   │ ─────────────────────▶ │  (또는 SIL)   │
│             │ ◀── GET_STATUS 폴링 응답 ──  │  drumrobot_server) │                        │              │
└─────────────┘                             └────────────────────┘                        └──────────────┘
```

- **Contract A**: `phil-brain` ↔ `phil-controller`(= `Phil-drum-robot/drumrobot_server`), TCP 포트 `1951`,
  `|` 구분 opcode 명령 + `GET_STATUS` 폴링 응답
- **Contract B**: 제어기 ↔ 하드웨어/SIL. 아래 B 섹션은 **구 DrumRobot2 제어기 기준**의 기록이며,
  신 Phil-drum-robot 제어기의 하드웨어 계층 계약은 추후 문서화한다.

> **2026-07-09 전환**: ground truth 제어기가 `DrumRobot2`(AgentSocket, TCP 9999, NDJSON push)에서
> `Phil-drum-robot/drumrobot_server`(TCP 1951, `|` opcode, GET_STATUS 폴링)로 바뀌었다.
> brain 내부 파이프라인(스킬/planner/validator/resolver)도 **wire 문법을 그대로 쓴다** —
> 구 내부 토큰(`p:TI`, `pause`, `move:` 등)과 번역 계층(`runtime/protocol.py`)은 폐기됐고,
> `phil_client.send_command()`는 명령을 무번역 전송한다. 관절명도 `motors.json` 이름으로 통일.

---

## Contract A — Brain ↔ Controller (TCP 1951)

### A.1 연결

- **서버**: `Phil-drum-robot/drumrobot_server` (`TcpServer`). `INADDR_ANY:1951`, `SOCK_STREAM`.
- **클라이언트**: `phil-brain` (`runtime/phil_client.py`). 기본 `127.0.0.1:1951`로 접속.
- **동시 접속**: 서버는 **한 번에 한 클라이언트만** 처리한다(accept 루프가 단일 연결 blocking).
  brain 이 유일한 장수명 연결이어야 하며, 별도 모니터링 클라이언트를 동시에 붙일 수 없다.
- **인코딩**: UTF-8. **프레이밍**: 줄바꿈(`\n`) 구분, 한 메시지 = 한 줄.
- **opcode 는 대소문자 무시**, 앞뒤 공백/개행은 서버가 trim 한다.
- **시작 절차(필수)**: 접속 후 `START` 전송 → 서버가 home pose 이동 + INIT 상태(사람이 고정 키 제거 대기)
  → `READY` 전송 → IDLE. IDLE 이전에는 START/QUIT 외 명령이 거부된다.
  brain 은 `connect()` 안에서 이 절차를 **키보드 입력**으로 진행한다.
- **응답**: `GET_STATUS`만 즉시 응답(STATUS 한 줄)하고, 나머지 명령은 전부 fire-and-forget 이다.
  효과 확인은 GET_STATUS 폴링으로 한다.

### A.2 Brain → Controller 명령 (한 줄 + `\n`)

| wire 명령 (brain 내부 문법과 동일) | 의미 / 수락 조건 |
|------|------|
| `START` | STANDBY→INIT (home 이동, 키 제거 대기). handshake 전용 |
| `READY` | INIT→IDLE. handshake 전용 |
| `PLAY\|<id>` | 곡 연주. id ∈ `{BI, BF, DS, TI, TY, WS}` (`config/play_list.json`). IDLE 전용. 시작 시 speed 1.0 리셋 |
| `PAUSE` | 일시정지 + 재개 지점(곡 id, 마디) 저장. PLAYING 전용 |
| `RESUME` | 저장된 재개 지점부터 재개(오디오 무음). IDLE + pause_point 필요 |
| `PLAY_CTRL\|stop` | 연주 중지, 재개 지점 폐기. PLAYING 전용. **brain 미사용** — 멈춤은 전부 `PAUSE` 로 보낸다(2026-07-10 결정, 서버 지원은 유지) |
| `PLAY_CTRL\|speed\|<x>` | 연주 속도 배율(0.5~2.0 서버 클램프). PLAYING 전용 |
| `POSE\|<name>` | 사전 정의 포즈. name ∈ `{init, home, ready, shutdown}`. IDLE 전용 |
| `MOVE\|<joint>\|<deg>\|...\|<move_time>` | 관절 절대각(도) 다중 쌍 + 이동시간(생략 시 서버 기본 3.0s). IDLE 전용 |
| `GESTURE\|<name>` | name ∈ `{nod, shake, wave, hi, hurray, happy}`. IDLE 전용 |
| `LOOK\|<pan>\|<tilt>` | 고개 yaw/pitch(도). 정면 0\|0, pan 왼쪽 양수, tilt 아래 양수. IDLE 전용 |
| `HIT\|<target>` | 단일 드럼 타격. IDLE 전용 (음성 미노출 — 추후) |
| `GET_STATUS` | 상태 조회(유일한 응답 명령). phil_client 폴러 전용 |
| `QUIT` | shutdown pose 후 종료. IDLE 전용 (미사용) |

**곡 코드 ↔ 라벨:** `TI`=This Is Me, `TY`=그대에게, `BI`=Baby I Need You, `BF`=필인, `DS`=드럼 솔로, `WS`=왜그래

**관절명 (`motors.json` id 순서 — brain 내부/validator/GET_STATUS 모두 이 이름 사용):**

```text
waist, right_shoulder_1, left_shoulder_1, right_shoulder_2, right_elbow,
left_shoulder_2, left_elbow, right_wrist, left_wrist,
right_pedal, left_pedal, head_yaw, head_pitch
```

> 구 `tempo_scale:`/`velocity_delta:`(사전 속도/세기 보정)는 **폐기**됐다. 신 서버에 대응 명령이 없고,
> 속도 조절은 연주 중 `PLAY_CTRL|speed`만 지원한다.

### A.3 Controller → Brain 상태 (GET_STATUS 폴링 응답, 한 줄 + `\n`)

push broadcast 는 없다. brain 은 배경 폴링 없이 **턴 시작 시점에만** `GET_STATUS` 를 보내
(`phil_client.fetch_state_snapshot`) 아래 형식의 응답을 `ROBOT_STATE` dict 로 번역한다(`parse_status`).

```text
STATUS|<state>|<q0_deg>|...|<q12_deg>|<speed>|<pause_valid>|<pause_id>|<pause_bar>
```

- `<state>` ∈ `{STANDBY, INIT, IDLE, PLAYING, SHUTTINGDOWN}` (연주가 끝나면 서버가 IDLE 로 되돌린다)
- `<q0..q12>`: 13개 관절의 **마지막 명령 목표각**(실측 아님, 도, motors.json id 순서)
- `<speed>`: 현재 연주 속도 배율
- `<pause_valid>|<pause_id>|<pause_bar>`: 재개 지점. 없으면 `0|-|0`

**brain 내부 번역 규칙 (`phil_client.parse_status`):**

- `state`: IDLE/STANDBY/INIT→`0`, PLAYING→`2`, SHUTTINGDOWN→`6` (기존 숫자 게이트 호환) + `state_str` 원문
- `is_lock_key_removed`: STANDBY/INIT→`false`(키 제거 전), 그 외→`true`
- `is_fixed`: 대응 없음 → 항상 `true`
- `current_angles`: motors.json 관절명 키로 채움
- `play_speed`: 신규 필드. **pause 3필드는 서버 내부 판단용이라 brain 은 읽지 않는다** (RESUME 은 서버가 알아서 처리)
- `current_song`: 서버가 주지 않으므로 brain 이 PLAY/RESUME 전송 기록으로 추적
- `bpm`/`progress`/`error_message`: 신 서버 미제공 → 기본값 유지

### A.4 상태 게이트 — 안전망

구 DrumRobot2 의 `isGateOpen`/`k` 입력 게이트는 **서버 상태 기계로 대체**됐다.

- STANDBY/INIT: START/QUIT 외 전부 거부 (= 안전 키 게이트. brain 은 `is_lock_key_removed=false` 로 매핑)
- IDLE: PLAY/MOVE/POSE/LOOK/GESTURE/HIT/RESUME/QUIT 수락
- PLAYING: PAUSE/PLAY_CTRL 만 수락 (motion 계열 전부 거부)
- 서버 거부는 조용히(로그만) 일어나므로, brain validator 가 같은 조건
  (`pause`/`stop`/`speed`=PLAYING 전용, `resume`=IDLE 전용, motion=IDLE 전용)을
  전송 전에 미리 검사해 사용자에게 이유를 말한다.

---

## Contract B — Controller ↔ SIL / 하드웨어 (CAN + DXL)

### B.1 인터페이스 선택 정책

- controller는 실제 `can*` 인터페이스가 **하나라도** 있으면 real CAN만 쓴다. 없으면 `vcan*`로 fallback.
- real CAN bitrate: `1000000`. `vcan`에는 bitrate를 설정하지 않는다.
- DXL은 `/dev/ttyUSB0` 하나의 serial bus. SIL에서는 이 경로가 PTY symlink이며 sim 측 endpoint는 `/tmp/ttyUSB0_sim`.
- **SIL 활성화는 환경변수가 아니라 인터페이스 존재 여부로만 결정된다.**

### B.2 motor ↔ node_id ↔ bus 매핑

| motor | 종류 | node_id | 기본 bus |
|-------|------|---------|----------|
| waist | TMotor | 0x00 | vcan0 |
| L_arm1 | TMotor | 0x02 | vcan0 |
| L_arm2 | TMotor | 0x05 | vcan0 |
| L_arm3 | TMotor | 0x06 | vcan0 |
| R_arm1 | TMotor | 0x01 | vcan1 |
| R_arm2 | TMotor | 0x03 | vcan1 |
| R_arm3 | TMotor | 0x04 | vcan1 |
| R_wrist | Maxon | 0x07 | vcan3 |
| L_wrist | Maxon | 0x08 | vcan3 |
| R_foot | Maxon | 0x0A | vcan2 |
| L_foot | Maxon | 0x0B | vcan2 |
| head_pan | DXL | ID 1 | /dev/ttyUSB0 |
| head_tilt | DXL | ID 2 | /dev/ttyUSB0 |

> frame-level SIL은 command가 들어온 bus를 feedback bus로 동적 바인딩할 수 있다. 디버깅 시 고정 표만
> 보지 말고 실제 `candump`로 어느 bus에 frame이 오가는지 확인하라.

### B.3 TMotor (servo mode) 프레임

**명령 — 위치 (SET_POS):**

```text
CAN ID : (0x04 << 8) | node_id        # 예: waist=0x0400
DLC    : 4
data[0:4] : int32 big-endian = round(position_deg * 10000)
            (입력 radian → degree 변환 후 인코딩)
```

**명령 — 속도 (SET_RPM):**

```text
CAN ID : (0x03 << 8) | node_id
DLC    : 4
data[0:4] : int32 big-endian = erpm (전기 RPM)
```

**피드백 (motor/SIL → controller):**

```text
CAN ID : node_id (0x00..0x06)
DLC    : 8
data[0:2] : int16 BE = position_deg / 0.1   (즉 deg*10)
data[2:4] : int16 BE = velocity * 10
data[4:6] : int16 BE = current / 0.01
data[6]   : int8 temperature
data[7]   : int8 error
```

> TMotor는 명령 수신 시 target echo 피드백을 즉시 보낸다(다음 safety check의 current 기준 갱신).
> 한 번이라도 명령을 받은 모터에는 idle 피드백을 보내지 않는다(echo와 idle이 서로 덮는 것 방지).

### B.4 Maxon (CANopen) 프레임

**COB-ID (node_id 기준):**

```text
SDO 요청   : 0x600 + node_id      SDO 응답   : 0x580 + node_id
TPDO ctrl  : 0x200 + node_id      TPDO pos   : 0x300 + node_id   ← 위치 명령
TPDO vel   : 0x400 + node_id      TPDO torq  : 0x500 + node_id
RPDO state : 0x180 + node_id      ← 피드백
SYNC       : 0x080 (DLC 0)        NMT        : 0x000 (data[0]=cmd, data[1]=node)
```

**위치 명령:**

```text
CAN ID : 0x300 + node_id
DLC    : 4
data[0:4] : uint32 little-endian = round(position_deg * 35.0 * 4096.0 / 360.0)
```

**피드백 (SYNC 0x80 수신 시 Operational Maxon만 응답):**

```text
CAN ID : 0x180 + node_id
DLC    : 8
data[1]   : status byte (예: 0x37)
data[2:6] : int32 little-endian position_enc
            position_deg = position_enc * 360.0 / (35.0 * 4096.0)
data[6:8] : int16 little-endian torque_enc  (torque_Nm = enc/1000 * 31.052)
```

> Maxon은 주기 피드백을 뿌리지 않는다. CANopen `0x80` SYNC를 받았을 때만 TPDO 피드백을 보낸다.

### B.5 Dynamixel Protocol 2.0 (serial)

- 경로 `/dev/ttyUSB0`, baud `4500000`, Protocol `2.0`. ID 1 = head_pan, ID 2 = head_tilt.
- packet 헤더: `FF FF FD 00`, 이후 `ID, len(LE16), instruction, params..., CRC16(LE)`.
- 주요 instruction: `0x01` PING, `0x03` WRITE, `0x55` STATUS(응답), `0x82` SYNC_READ, `0x83` SYNC_WRITE, `0xFE` broadcast.

**goal position 인코딩 (tick ↔ degree):**

```text
tick = round(2048.0 - angle_deg * 4096.0 / 360.0)
angle_deg = (2048.0 - tick) * 360.0 / 4096.0
```

**피드백 규약:**

```text
syncWrite(goal) -> goal 저장, status 응답 없음
syncRead(ID 1, ID 2) -> 마지막 goal(또는 startup pose) 기준 status packet으로 응답
```

> `syncRead` 응답은 PyBullet state가 아니라 마지막 goal/startup 기준으로 빠르게 만든다(응답 지연 시
> controller 로그에 `SyncRead failed`).

### B.6 각도 의미 변환 (SIL 측 책임)

production motor 각도 ↔ URDF joint 각도 변환은 SIL의 `sil/mapping.py`
(`PRODUCTION_TO_URDF_CAN_TRANSFORM`, startup pose 등)가 담당한다. **controller는 production 의미의
각도만 보내고 받으며, URDF 변환은 알 필요가 없다.**

### B.7 safety와 current angle

controller의 TMotor send loop는 명령 전송 전에 current vs desired 차이로 safety check를 한다.
피드백이 밀리거나 오래된 피드백이 current를 덮으면 다음 명령에서
`Set CAN Frame Error : Safety Check`가 날 수 있다. 즉 **피드백 timing도 계약의 일부**다.

---

## 계약 변경 절차

1. 이 `CONTRACTS.md`를 먼저 고친다(어느 레포에서든).
2. 영향받는 두 레포의 코드를 같이 고친다(wire format은 한쪽만 바꾸면 깨진다).
3. 세 레포의 `CONTRACTS.md` 복사본을 동일하게 맞춘다.
4. 각 레포 `log.md`에 변경을 기록한다.
