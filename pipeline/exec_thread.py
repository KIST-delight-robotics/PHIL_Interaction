"""
Executor — 로봇 명령을 백그라운드 스레드에서 순서대로 전송한다.

전송 자체는 거의 즉시라 사실 동기로 해도 무방하다(보내도 로봇이 실제로 움직이는 데
시간이 걸려 TTS 와의 체감 순서 차이가 없다). 비동기로 두는 건 큰 이득이라기보다
나중을 위한 여지 정도다.
"""

import threading
import time
from typing import Callable, List, Optional

from runtime.console_log import cmd_flag_pending, console, mark_cmd_sent

# 명령 전송 후 상태 반영을 확인하는 조회 시점(초). 첫 조회에서 변화가 안 보이면
# 한 번 더 기다렸다 본다. MOVE/POSE/GESTURE 는 동작 시간(기본 ~3초)이 지나야
# 최종 각도가 잡히므로 첫 조회를 더 늦게 잡는다.
_MOTION_FETCH_DELAYS = (3.5, 2.0)
_QUICK_FETCH_DELAYS = (1.0, 2.5)
_MOTION_CMD_PREFIXES = ("MOVE|", "POSE|", "GESTURE|", "HIT|")


class Executor:

    def __init__(self, bot):
        self._bot = bot
        self._thread: Optional[threading.Thread] = None
        self._lock: threading.Lock = threading.Lock()

    def exec_cmd(self, commands: List[str], on_done: Callable[[], None]) -> None:
        """commands 를 백그라운드 스레드에서 순서대로 전송하고, 끝나면 on_done() 을 부른다."""
        with self._lock:
            self._thread = threading.Thread(
                target=self._run_commands,
                args=(commands, on_done),
                daemon=True,
            )
            self._thread.start()

    def is_running(self) -> bool:
        """백그라운드 전송 스레드가 아직 살아있으면 True."""
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def _run_commands(self, commands: List[str], on_done: Callable[[], None]) -> None:
        """백그라운드 스레드 본체: 명령을 순서대로 전송한 뒤 on_done() 을 호출한다."""
        sent_ok = False
        for cmd in commands:
            console(f" 명령: {cmd}")
            try:
                if self._bot.send_command(cmd) is not False:
                    sent_ok = True
                    # 다음 상태 갱신 1회를 터미널에 보여주기 위한 플래그 (phil_client 가 소비)
                    mark_cmd_sent()
                else:
                    console(f"⚠️ 명령 전송 실패: {cmd}")
            except Exception as exc:
                console(f"⚠️ 명령 전송 실패: {cmd} ({exc})")

        if sent_ok:
            self._report_state(commands)

        try:
            on_done()
        except Exception as exc:
            print(f"⚠️ [Executor] on_done 콜백 오류: {exc}")

    def _report_state(self, commands: List[str]) -> None:
        """
        명령이 로봇 상태에 반영될 때쯤 상태를 조회해, "변한 상태 한 줄"이 다음 턴
        시작까지 밀리지 않고 명령 직후 터미널에 뜨게 한다. 실제 출력은 phil_client 의
        fetch_state_snapshot 이 cmd 플래그를 소비하며 수행하고, 여기서 변화를 못 잡으면
        플래그가 남아 다음 턴 시작 조회가 대신 잡는다.
        """
        fetch_state = getattr(self._bot, "fetch_state_snapshot", None)
        if fetch_state is None:
            return  # eval/test 의 fake bot 은 상태 조회가 없다

        motion_like = any(cmd.startswith(_MOTION_CMD_PREFIXES) for cmd in commands)
        fetch_delays = _MOTION_FETCH_DELAYS if motion_like else _QUICK_FETCH_DELAYS

        for delay_sec in fetch_delays:
            time.sleep(delay_sec)
            try:
                fetch_state()
            except Exception:
                return
            if not cmd_flag_pending():
                return  # 변화가 관측돼 터미널 출력까지 끝났다
