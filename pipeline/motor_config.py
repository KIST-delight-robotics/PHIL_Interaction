"""
관절 이름/순서/한계의 단일 소스 — data/motors.json (drumrobot_server config 사본).
"""

from typing import Dict, List, Tuple

from .data_files import load_data_json

_MOTOR_LIST: List[Dict] = load_data_json("motors.json")["motors"]

# GET_STATUS 응답의 관절각 순서 = motors.json id 순서
JOINT_ORDER: List[str] = [
    motor_entry["name"]
    for motor_entry in sorted(_MOTOR_LIST, key=lambda motor_entry: motor_entry["id"])
]

# 관절 한계 (min_deg, max_deg)
JOINT_LIMITS: Dict[str, Tuple[float, float]] = {
    motor_entry["name"]: (float(motor_entry["min_angle"]), float(motor_entry["max_angle"]))
    for motor_entry in _MOTOR_LIST
}
