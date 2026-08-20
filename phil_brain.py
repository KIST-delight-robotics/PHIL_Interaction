# phil_robot/phil_brain.py

import os
import time

from runtime.console_log import init_log, console

# 무거운 import(whisper/MeloTTS)의 경고까지 로그로 보내려고 리디렉션을 가장 먼저 건다
if __name__ == "__main__":
    LOG_PATH = init_log()
    console("📁 실행 로그: {}".format(LOG_PATH))

import numpy as np
import psutil
import whisper

from config import PLANNER_MODEL, CLASSIFIER_MODEL
from pipeline.exec_thread import Executor
from pipeline.robot_fsm import build_run_turn
from pipeline.session import SessionContext, update_session
from runtime.melo_engine import TTS_Engine
from runtime.mic_listener import MicListener
from runtime.phil_client import RobotClient

HOST = "127.0.0.1"
PORT = 1951


def get_mem_usage():
    """현재 프로세스의 메모리 사용량을 MB 단위로 반환"""
    process = psutil.Process(os.getpid())
    return process.memory_info().rss / (1024 * 1024)


def warm_up_stt_model(stt_model):
    """초기 추론 지연을 줄이기 위해 무음 샘플로 모델을 예열"""
    print("🔥 모델 예열 중... (잠시만 기다려주세요)")
    try:
        dummy_audio = np.zeros(16000, dtype=np.float32)
        stt_model.transcribe(dummy_audio, fp16=False, language="ko")
    except Exception:
        pass


def load_runtime():
    """대화 루프 런타임(bot/TTS/STT) 준비."""
    bot = RobotClient(host=HOST, port=PORT)
    if not bot.connect():
        console("연결 실패")
        return None, None, None

    console("⏳ 모델 로딩 중... (TTS/STT, 잠시 걸립니다)")

    base_mem = get_mem_usage()
    print(f"[{time.strftime('%H:%M:%S')}] 초기 메모리: {base_mem:.2f} MB")

    tts = TTS_Engine()
    tts_mem = get_mem_usage()
    print(f"[{time.strftime('%H:%M:%S')}] TTS 로드 후: {tts_mem:.2f} MB (증가량: {tts_mem - base_mem:.2f} MB)")

    print("[STT] Whisper 모델 로딩 중...")
    stt_model = whisper.load_model("small", device="cuda")

    stt_mem = get_mem_usage()
    print(f"[{time.strftime('%H:%M:%S')}] STT 로드 후: {stt_mem:.2f} MB (증가량: {stt_mem - tts_mem:.2f} MB)")
    print(f"\n✅ 모델 로딩 완료! 총 점유: {stt_mem:.2f} MB")

    warm_up_stt_model(stt_model)
    print("✅[STT] 준비 완료!")
    console("✅ 준비 완료. 말씀해 주세요.")
    return bot, tts, stt_model


def transcribe_user_speech(stt_model, audio_data):
    """STT 단계만 별도 함수로 분리해 메인 루프 가독성을 높인다."""
    stt_start_time = time.time()
    print("텍스트 변환 중...")
    result = stt_model.transcribe(audio_data, fp16=False, language="ko")
    user_text = result["text"].strip()
    print(f"🗣️ User: {user_text}")
    print(f"⏱️ STT 처리 시간: {time.time() - stt_start_time:.2f}초")
    return user_text


def format_metric(value):
    if not isinstance(value, (int, float)):
        return "N/A"
    return f"{value:.2f}s"


def print_llm_metrics(label, metric_obj):
    if not metric_obj:
        return

    prompt_tok = metric_obj.get("prompt_tokens")
    eval_tok = metric_obj.get("eval_tokens")
    wall = metric_obj.get("wall_sec")

    if metric_obj.get("backend") == "openai":
        total_tok = metric_obj.get("total_tokens")
        tps = metric_obj.get("eval_tps")
        tps_str = f"{tps:.1f}t/s" if isinstance(tps, float) else "N/A"
        print(
            f"[{label} LLM/API] wall={format_metric(wall)}, "
            f"prompt_tok={prompt_tok if isinstance(prompt_tok, int) else 'N/A'}, "
            f"eval_tok={eval_tok if isinstance(eval_tok, int) else 'N/A'}, "
            f"total_tok={total_tok if isinstance(total_tok, int) else 'N/A'}, "
            f"~{tps_str}"
        )
    else:
        print(
            f"[{label} LLM] wall={format_metric(wall)}, "
            f"load={format_metric(metric_obj.get('load_sec'))}, "
            f"prompt_eval={format_metric(metric_obj.get('prompt_sec'))}, "
            f"eval={format_metric(metric_obj.get('eval_sec'))}, "
            f"prompt_tok={prompt_tok if isinstance(prompt_tok, int) else 'N/A'}, "
            f"eval_tok={eval_tok if isinstance(eval_tok, int) else 'N/A'}, "
            f"overhead={format_metric(metric_obj.get('overhead_sec'))}"
        )


def debug_from_state(final_state):
    """PhilState 에서 LLM 입력, 결과, validator 경고를 한 곳에서 출력한다."""
    debug = final_state.get("debug", {})
    classifier_input = debug.get("classifier_input", "")
    planner_input = debug.get("planner_input", "")

    if classifier_input:
        print(f"🧭 [Classifier 입력]\n{classifier_input}")
    print(f"🧭 [Classifier 결과] {final_state.get('classifier_output', {})}")
    print(f"🧭 [Planner Domain] {final_state.get('planner_domain', '')}")
    if planner_input:
        print(f"🧐 [Planner 입력]\n{planner_input}")
    print(f"🗺️ [Planner 결과] {final_state.get('planner_output', {})}")

    classifier_dur = debug.get("classifier_duration_sec", 0.0)
    planner_dur = debug.get("planner_duration_sec", 0.0)
    print(
        f"⏱️ LLM 처리 시간: 총 {classifier_dur + planner_dur:.2f}초 "
        f"(classifier {classifier_dur:.2f}초 + planner {planner_dur:.2f}초)"
    )
    print_llm_metrics("Classifier", debug.get("classifier_metrics", {}))
    print_llm_metrics("Planner", debug.get("planner_metrics", {}))

    validated = final_state.get("validated")
    if validated is None:
        return

    if validated.reason:
        print(f"\n[Planner Reason]\n{validated.reason}\n")
    if validated.expanded_op_cmds:
        print(f"[Planner Expanded] {validated.expanded_op_cmds}")
    if validated.resolved_op_cmds:
        print(f"[Planner Resolved] {validated.resolved_op_cmds}")
    if validated.valid_op_cmds:
        print(f"[Validator Accepted] {validated.valid_op_cmds}")
    if validated.rejected_op_cmds:
        print(f"[Validator Rejected] {validated.rejected_op_cmds}")
    for warning in validated.warnings:
        print(f"[Validator] {warning}")


def main():
    bot, tts, stt_model = load_runtime()
    if not all([bot, tts, stt_model]):
        return

    tts.speak("대화 준비가 되었습니다. 말씀해 주세요.")

    session = SessionContext()
    executor = Executor(bot)

    # session 은 매 턴 재할당되므로 getter 로 최신 객체를 넘긴다
    def get_session():
        return session

    run_turn = build_run_turn(
        bot=bot,
        executor=executor,
        get_session=get_session,
        get_state_fn=bot.fetch_state_snapshot,   # 배경 폴링 없음 — 턴 시작 1회 fetch
        classifier_model=CLASSIFIER_MODEL,
        planner_model=PLANNER_MODEL,
    )

    # VAD 리스너 — 확정된 발화만 큐로 넘어온다
    listener = MicListener()
    listener.start()

    try:
        while True:
            # 확정된 발화가 올 때까지 대기한다(없으면 None 후 재시도).
            audio_data = listener.read_utterance(timeout=1.0)
            if audio_data is None:
                continue

            user_text = transcribe_user_speech(stt_model, audio_data)
            if not user_text:
                continue

            console("")
            console(" 나: {}".format(user_text))

            # cross-turn 복구(되묻기) 진행 중인지 안내한다.
            if session.pending_intent:
                print(f"💬 [복구 대기 회차 {session.recovery_count}] 원래 요청: {session.pending_intent}")

            print("🧠 생각 중...")

            final_state = run_turn(user_text)

            # MeloTTS 는 스레드 비안전 — TTS 는 메인 스레드에서 (명령은 executor 백그라운드)
            speech = final_state.get("speech", "")
            if speech:
                console(" 필: {}".format(speech))
                # TTS 재생 구간 동안 리스너를 막아 self-echo 를 차단한다.
                listener.set_speaking(True)
                tts.speak(speech, stream=True)
                listener.set_speaking(False)

            debug_from_state(final_state)
            print(f"[FSM] plan_type={final_state.get('plan_type')}")

            validated = final_state.get("validated")
            if validated is not None:
                session = update_session(
                    session,
                    user_text,
                    final_state.get("classifier_output", {}),
                    validated,
                )

    except KeyboardInterrupt:
        console("\n종료합니다.")
    finally:
        listener.close()
        bot.close()


if __name__ == "__main__":
    main()
