# manual_client.py
# LLM/STT 없이 키보드로 명령을 쳐서 로봇(C++ AgentSocket, TCP 9999)에 직접 보내는
# 수동 클라이언트. phil_brain.py(STT+LLM) 대신 빠르게 명령을 테스트할 때 쓴다.
#
# 의존성 없음: Python 표준 라이브러리만 사용한다. conda 불필요.
#
# 사용법:
#   python tests/manual_client.py            # 기본 127.0.0.1:9999
#   python tests/manual_client.py <host> <port>
#
# 계약(Contract A)은 phil_robot/docs/CONTRACTS.md 참고.

import socket
import sys
import threading

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9999

# 사람이 보기 좋은 명령 도움말. 형식은 Contract A의 Brain -> Controller 표와 같다.
HELP_TEXT = """
보낼 수 있는 명령 (한 줄에 하나, Enter로 전송):
  r                      ready pose
  h                      home (게이트 우회)
  s                      즉시 정지 (버퍼 flush)
  p:<song>               곡 연주. song ∈ {TIM, TY_short, BI, test_one}
  move:<joint>,<angle>   단일 관절 절대각(도). 예: move:waist,45
  gesture:<name>         name ∈ {hi, nod, shake, wave, hurray, happy}
  look:<pan>,<tilt>      pan ∈ [-90,90], tilt ∈ [0,120]
  pause                  연주 일시정지 (게이트 우회)
  resume                 중단 위치부터 재개 (게이트 우회)
  tempo_scale:<value>    연주 템포 보정 (게이트 우회)
  velocity_delta:<value> 타격 세기 보정 (게이트 우회)

관절 범위(도):
  waist (-90,90)  R_arm1 (0,150)   L_arm1 (30,180)
  R_arm2 (-60,90) R_arm3 (0,140.1) L_arm2 (-60,90) L_arm3 (0,140.1)
  R_wrist (-108,90) L_wrist (-108,90)

내장 명령:
  help / ?               이 도움말 출력
  state                  마지막으로 받은 로봇 상태 한 줄 출력
  quit / exit            종료

주의: 게이트가 닫혀 있으면 move/gesture/look/p/r 명령은 폐기된다.
      C++ 콘솔에서 'k'를 입력해 게이트를 열어야 통과한다.
"""

# 수신 스레드가 채우는 마지막 상태 한 줄. 화면에는 출력하지 않고 `state` 명령으로만 본다.
last_state = {"line": ""}
state_lock = threading.Lock()


def drain_state(sock):
    """서버가 보내는 상태 JSON을 조용히 읽어 마지막 한 줄만 보관한다 (소켓 버퍼가 차지 않게)."""
    buffer = ""
    while True:
        try:
            data = sock.recv(4096).decode("utf-8")
        except OSError:
            break
        if not data:
            break

        buffer += data
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            if line.strip():
                with state_lock:
                    last_state["line"] = line.strip()


def main():
    host = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_HOST
    port = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_PORT

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.connect((host, port))
    print(f"서버({host}:{port})에 연결되었습니다.")

    recv_thread = threading.Thread(target=drain_state, args=(sock,), daemon=True)
    recv_thread.start()

    print(HELP_TEXT)

    while True:
        try:
            line = input("명령> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not line:
            continue

        lowered = line.lower()
        if lowered in ("quit", "exit"):
            break
        if lowered in ("help", "?"):
            print(HELP_TEXT)
            continue
        if lowered == "state":
            with state_lock:
                print(last_state["line"] or "(아직 받은 상태 없음)")
            continue

        # Contract A: 한 메시지 = 한 줄(\n 종료)
        sock.sendall((line + "\n").encode("utf-8"))
        print(f"   ↳ 전송: {line!r}")

    sock.close()
    print("연결 종료")


if __name__ == "__main__":
    main()
