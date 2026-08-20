"""
data/*.json, prompts/*.md 로더.

motors.json 은 drumrobot_server/config/motors.json 의 그대로 복사본 — 런타임 결합 금지,
서버 값이 바뀌면 재복사로 동기화. songs/skills.json 은 brain 고유 데이터,
prompts/*.md 는 LLM system prompt 원문.
"""

import json
import os
from typing import Dict

_DATA_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")
)
_PROMPT_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "prompts")
)


def load_data_json(file_name: str) -> Dict:
    """data/<file_name> 을 dict 로 읽는다. 없거나 깨졌으면 즉시 예외 (fail-fast)."""
    data_path = os.path.join(_DATA_DIR, file_name)
    with open(data_path, encoding="utf-8") as data_file:
        return json.load(data_file)


def load_prompt_text(file_name: str) -> str:
    """prompts/<file_name> 프롬프트 원문을 읽는다 (저장 시 덧붙인 마지막 개행 1개는 벗김)."""
    prompt_path = os.path.join(_PROMPT_DIR, file_name)
    with open(prompt_path, encoding="utf-8") as prompt_file:
        raw_text = prompt_file.read()
    if raw_text.endswith("\n"):
        return raw_text[:-1]
    return raw_text
