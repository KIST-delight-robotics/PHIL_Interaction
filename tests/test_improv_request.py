import unittest

from phil_robot.pipeline.brain_pipeline import build_prefilter_plan
from phil_robot.pipeline.command_validator import validate_commands
from phil_robot.pipeline.songs import song_label_of
from phil_robot.pipeline.state_adapter import detect_improv_request

IDLE_STATE = {"state": 0, "is_lock_key_removed": True, "is_fixed": True}
PLAYING_STATE = {"state": 2, "is_lock_key_removed": True, "is_fixed": True}


class DetectImprovRequestTest(unittest.TestCase):
    def test_genre_and_bpm(self) -> None:
        result = detect_improv_request("펑크 즉흥 연주해줘. 80비피엠으로")

        self.assertIsNotNone(result)
        self.assertEqual(result["genre"], "funk")
        self.assertEqual(result["bpm"], 80.0)

    def test_genre_only(self) -> None:
        result = detect_improv_request("재즈로 즉흥 연주 해줘")

        self.assertIsNotNone(result)
        self.assertEqual(result["genre"], "jazz")
        self.assertIsNone(result["bpm"])

    def test_no_genre_no_bpm(self) -> None:
        result = detect_improv_request("즉흥 연주 해줘")

        self.assertIsNotNone(result)
        self.assertIsNone(result["genre"])
        self.assertIsNone(result["bpm"])

    def test_bpm_before_genre(self) -> None:
        result = detect_improv_request("100비피엠으로 힙합 즉흥 부탁해")

        self.assertIsNotNone(result)
        self.assertEqual(result["genre"], "hiphop")
        self.assertEqual(result["bpm"], 100.0)

    def test_bpm_word_first(self) -> None:
        result = detect_improv_request("팝 즉흥 bpm 90 으로")

        self.assertIsNotNone(result)
        self.assertEqual(result["genre"], "pop")
        self.assertEqual(result["bpm"], 90.0)

    def test_rock_spelling_variants(self) -> None:
        for spoken_text in ("락 즉흥 해줘", "록으로 즉흥 해줘"):
            result = detect_improv_request(spoken_text)
            self.assertIsNotNone(result)
            self.assertEqual(result["genre"], "rock")

    def test_all_genres_detected(self) -> None:
        genre_by_word = {
            "컨트리": "country",
            "펑크": "funk",
            "힙합": "hiphop",
            "재즈": "jazz",
            "팝": "pop",
            "락": "rock",
        }
        for spoken_word, genre_code in genre_by_word.items():
            result = detect_improv_request(f"{spoken_word} 즉흥 연주해줘")
            self.assertIsNotNone(result)
            self.assertEqual(result["genre"], genre_code)

    def test_question_is_not_request(self) -> None:
        self.assertIsNone(detect_improv_request("즉흥 연주 할 수 있어?"))
        self.assertIsNone(detect_improv_request("즉흥 연주가 뭐야"))

    def test_no_trigger_word(self) -> None:
        self.assertIsNone(detect_improv_request("그대에게 연주해줘"))
        self.assertIsNone(detect_improv_request("펑크 노래 틀어줘"))


class ImprovPrefilterTest(unittest.TestCase):
    def test_genre_bpm_packet(self) -> None:
        prefilter = build_prefilter_plan("펑크 즉흥 연주해줘 80비피엠으로", IDLE_STATE)

        self.assertIsNotNone(prefilter)
        classifier_output, planner_output, planner_domain = prefilter
        self.assertEqual(classifier_output["intent"], "play_request")
        self.assertEqual(planner_output["op_cmd"], ["PLAY|improv|funk|80"])
        self.assertEqual(planner_domain, "play")

    def test_bare_improv_packet(self) -> None:
        prefilter = build_prefilter_plan("즉흥 연주 시작해줘", IDLE_STATE)

        self.assertIsNotNone(prefilter)
        _, planner_output, _ = prefilter
        self.assertEqual(planner_output["op_cmd"], ["PLAY|improv"])

    def test_bpm_without_genre_packet(self) -> None:
        prefilter = build_prefilter_plan("80비피엠으로 즉흥 연주해줘", IDLE_STATE)

        self.assertIsNotNone(prefilter)
        _, planner_output, _ = prefilter
        self.assertEqual(planner_output["op_cmd"], ["PLAY|improv||80"])

    def test_resume_word_yields_improv(self) -> None:
        prefilter = build_prefilter_plan("다시 재즈 즉흥 해줘", IDLE_STATE)

        self.assertIsNotNone(prefilter)
        _, planner_output, _ = prefilter
        self.assertEqual(planner_output["op_cmd"], ["PLAY|improv|jazz"])

    def test_pause_beats_improv(self) -> None:
        prefilter = build_prefilter_plan("즉흥 그만해", PLAYING_STATE)

        self.assertIsNotNone(prefilter)
        _, planner_output, _ = prefilter
        self.assertEqual(planner_output["op_cmd"], ["PAUSE"])


class ImprovValidationTest(unittest.TestCase):
    def test_valid_improv_commands(self) -> None:
        for command in ("PLAY|improv", "PLAY|improv|funk", "PLAY|improv|funk|80", "PLAY|improv||80"):
            result = validate_commands([command], IDLE_STATE)
            self.assertEqual(result.valid_commands, [command])
            self.assertEqual(result.rejected_commands, [])

    def test_bpm_out_of_range_rejected(self) -> None:
        result = validate_commands(["PLAY|improv|funk|300"], IDLE_STATE)

        self.assertEqual(result.valid_commands, [])
        self.assertEqual(result.rejected_commands, ["PLAY|improv|funk|300"])

    def test_unknown_genre_rejected(self) -> None:
        result = validate_commands(["PLAY|improv|metal"], IDLE_STATE)

        self.assertEqual(result.valid_commands, [])
        self.assertEqual(result.rejected_commands, ["PLAY|improv|metal"])

    def test_playing_state_rejected(self) -> None:
        result = validate_commands(["PLAY|improv|jazz"], PLAYING_STATE)

        self.assertEqual(result.valid_commands, [])
        self.assertEqual(result.rejected_commands, ["PLAY|improv|jazz"])


class SongLabelTest(unittest.TestCase):
    def test_improv_labels(self) -> None:
        self.assertEqual(song_label_of("improv:jazz:80"), "재즈 즉흥 연주")
        self.assertEqual(song_label_of("improv:funk"), "펑크 즉흥 연주")
        self.assertEqual(song_label_of("improv"), "즉흥 연주")

    def test_song_code_label_unchanged(self) -> None:
        self.assertEqual(song_label_of("TI"), "This Is Me")
        self.assertEqual(song_label_of("None"), "None")


if __name__ == "__main__":
    unittest.main()
