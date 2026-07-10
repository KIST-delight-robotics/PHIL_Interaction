# phil_robot/runtime/console_log.py
"""
콘솔/로그 분리 계층.

init_log() 가 프로세스의 fd 1(stdout)/fd 2(stderr)를 runtime_log/brainlog_*.txt 로
리디렉션한다. 이후 모든 print, 경고, tqdm 진행바, 서드파티(C 레벨 포함) 출력은
로그 파일로만 가고, 터미널에는 console()/console_input() 을 거친 줄만 나타난다.

- init_log(): 로그 파일 생성 + fd 리디렉션. 엔트리포인트에서 무거운 import 전에 1회 호출.
- console(msg): 터미널과 로그 파일 양쪽에 출력.
- console_input(prompt): 터미널에 프롬프트를 띄우고 stdin 입력을 받는다 (로그에도 기록).
- mark_cmd_sent()/take_cmd_flag(): LLM 명령 전송 후 첫 의미 있는 상태 변화 1회를
  터미널에 보여주기 위한 플래그 (Executor 가 세우고 phil_client 가 소비).
"""

import os
import sys
import time
import threading

_terminal = None      # 원래 터미널 stdout (init_log 이후에만 유효)
_log_path = None
_flag_lock = threading.Lock()
_cmd_sent = False


def init_log(log_dir=None):
    """
    runtime_log/brainlog_<월일>_<시분>.txt 를 만들고 stdout/stderr 를 그리로 돌린다.
    파일명 시각은 생성 순간 기준이며, 프로세스 종료까지 같은 파일에 이어 쓴다.
    이미 초기화됐으면 기존 로그 경로를 그대로 반환한다.
    """
    global _terminal, _log_path

    if _terminal is not None:
        return _log_path

    if log_dir is None:
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        log_dir = os.path.join(base_dir, "runtime_log")
    os.makedirs(log_dir, exist_ok=True)

    stamp = time.strftime("%m%d_%H%M")
    _log_path = os.path.join(log_dir, "brainlog_{}.txt".format(stamp))

    # 원래 터미널 stdout 을 복제해 보존한 뒤, fd 1/2 를 로그 파일로 교체한다.
    # fd 레벨로 바꿔야 Python print 뿐 아니라 C 라이브러리/서브프로세스 출력까지 잡힌다.
    terminal_fd = os.dup(1)
    _terminal = os.fdopen(terminal_fd, "w", buffering=1, encoding="utf-8", errors="replace")

    log_file = open(_log_path, "a", buffering=1, encoding="utf-8", errors="replace")
    sys.stdout.flush()
    sys.stderr.flush()
    os.dup2(log_file.fileno(), 1)
    os.dup2(log_file.fileno(), 2)

    # 로그 파일 대상이어도 줄 단위로 즉시 남도록 line-buffered wrapper 로 교체한다.
    sys.stdout = os.fdopen(os.dup(1), "w", buffering=1, encoding="utf-8", errors="replace")
    sys.stderr = os.fdopen(os.dup(2), "w", buffering=1, encoding="utf-8", errors="replace")

    print("[console_log] 로그 시작: {} ({})".format(
        _log_path, time.strftime("%Y-%m-%d %H:%M:%S")))
    return _log_path


def console(message=""):
    """터미널과 로그 파일 양쪽에 한 줄을 출력한다. init_log 전이면 일반 print 와 같다."""
    if _terminal is None:
        print(message)
        return
    try:
        _terminal.write(message + "\n")
        _terminal.flush()
    except OSError:
        pass  # 터미널이 끊겨도(원격 세션 종료 등) 로그 기록과 본 처리는 계속한다
    print(message)


def console_input(prompt):
    """터미널에 프롬프트를 띄우고 입력을 받는다. 프롬프트와 입력 내용은 로그에도 남긴다."""
    if _terminal is None:
        return input(prompt)
    try:
        _terminal.write(prompt)
        _terminal.flush()
    except OSError:
        pass
    reply = input()
    print("{}{}".format(prompt, reply))
    return reply


def mark_cmd_sent():
    """LLM 명령을 로봇에 전송했음을 표시한다. 다음 상태 변화 1회가 터미널에 표시된다."""
    global _cmd_sent
    with _flag_lock:
        _cmd_sent = True


def take_cmd_flag():
    """명령 전송 플래그를 소비한다. 세워져 있었으면 True 를 반환하고 내린다."""
    global _cmd_sent
    with _flag_lock:
        was_sent = _cmd_sent
        _cmd_sent = False
        return was_sent


def cmd_flag_pending():
    """플래그를 소비하지 않고 상태만 본다 (Executor 의 상태 반영 대기 루프용)."""
    with _flag_lock:
        return _cmd_sent
