import time
import unittest
import sys
from email.message import Message
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "bridge"))
import pc_bridge


class ApiMutationSecurityTests(unittest.TestCase):
    @staticmethod
    def handler(host="127.0.0.1:8765", origin="http://127.0.0.1:8765", token=None):
        handler = object.__new__(pc_bridge.Handler)
        handler.headers = Message()
        if host is not None:
            handler.headers["Host"] = host
        if origin is not None:
            handler.headers["Origin"] = origin
        if token is not None:
            handler.headers["X-A51-Token"] = token
        handler.responses = []
        handler.send_body = lambda code, body, *args, **kwargs: handler.responses.append((code, body))
        return handler

    def test_same_origin_request_with_token_is_allowed(self):
        handler = self.handler(token=pc_bridge.API_TOKEN)
        self.assertFalse(handler.reject_untrusted_mutation())
        self.assertEqual(handler.responses, [])

    def test_cross_origin_request_is_rejected(self):
        handler = self.handler(origin="https://example.com", token=pc_bridge.API_TOKEN)
        self.assertTrue(handler.reject_untrusted_mutation())
        self.assertEqual(handler.responses[0][0], 403)

    def test_missing_token_is_rejected(self):
        handler = self.handler()
        self.assertTrue(handler.reject_untrusted_mutation())
        self.assertEqual(handler.responses[0][0], 403)


def test_config():
    config = pc_bridge._default_automation_config()
    for state in ("GAME", "VIDEO", "WORK", "IDLE"):
        config["detection"]["stability"][state] = {"enter_ms": 0, "exit_ms": 0}
    return config


def foreground(process, large=True):
    return {
        "process": process,
        "pid": 42,
        "title": process,
        "window": {"large": large, "fullscreen": large, "coverage": 1.0 if large else 0.2},
    }


class ConfigValidationTests(unittest.TestCase):
    def test_invalid_pages_and_processes_are_sanitized(self):
        value = pc_bridge._default_automation_config()
        value["detection"]["work_processes"] = ["Code.exe", "CODE.EXE", "../bad.exe", ""]
        value["profiles"]["GAME"]["pages"] = ["unknown", "game_performance", "game_performance"]
        value["profiles"]["IDLE"]["rotation"]["interval_sec"] = 999
        clean = pc_bridge.validate_automation_config(value)
        self.assertEqual(clean["detection"]["work_processes"], ["code.exe", "bad.exe"])
        self.assertEqual(clean["profiles"]["GAME"]["pages"], ["game_performance"])
        self.assertEqual(clean["profiles"]["IDLE"]["rotation"]["interval_sec"], 30)

    def test_empty_profile_falls_back_to_default(self):
        value = pc_bridge._default_automation_config()
        value["profiles"]["VIDEO"]["pages"] = []
        clean = pc_bridge.validate_automation_config(value)
        self.assertEqual(clean["profiles"]["VIDEO"]["pages"], ["now_playing"])

    def test_today_profile_page_is_preserved(self):
        value = pc_bridge._default_automation_config()
        value["profiles"]["DEFAULT"]["pages"] = ["today"]
        clean = pc_bridge.validate_automation_config(value)
        self.assertEqual(clean["profiles"]["DEFAULT"]["pages"], ["today"])


class MediaSelectionTests(unittest.TestCase):
    def test_foreground_paused_video_beats_background_playing_music(self):
        spotify = {"source": "spotify.exe", "status": "playing", "title": "Song", "is_current": False}
        browser = {"source": "msedge.exe", "status": "paused", "title": "Video", "is_current": False}
        selected = max((spotify, browser), key=lambda item: pc_bridge._media_session_rank(item, "msedge.exe"))
        self.assertIs(selected, browser)

    def test_playing_session_wins_without_foreground_match(self):
        playing = {"source": "spotify.exe", "status": "playing", "title": "Song", "is_current": False}
        paused = {"source": "vlc.exe", "status": "paused", "title": "Movie", "is_current": False}
        selected = max((playing, paused), key=lambda item: pc_bridge._media_session_rank(item, "code.exe"))
        self.assertIs(selected, playing)


class ContextEngineTests(unittest.TestCase):
    def evaluate(self, engine, config, fg=None, fps=None, media=None, idle=0):
        return engine.evaluate(
            fg,
            fps or {"frame_samples": [], "sample_age_ms": 999999},
            media or {},
            idle,
            config,
            1,
        )

    def test_game_from_large_foreground_present_activity(self):
        config = test_config()
        engine = pc_bridge.ContextEngine(config)
        result = self.evaluate(
            engine,
            config,
            foreground("examplegame.exe"),
            {"frame_samples": [[16, 16, 16, "Application"]] * 3, "sample_age_ms": 50},
        )
        self.assertEqual(result["state"], "GAME")

    def test_game_wins_over_video_and_idle(self):
        config = test_config()
        config["detection"]["app_classification"]["game.exe"] = "GAME"
        engine = pc_bridge.ContextEngine(config)
        result = self.evaluate(
            engine,
            config,
            foreground("game.exe"),
            media={"status": "playing", "title": "Cutscene", "playback_type": "video", "source": "game.exe"},
            idle=301,
        )
        self.assertEqual(result["state"], "GAME")

    def test_browser_video_is_video_not_game(self):
        config = test_config()
        engine = pc_bridge.ContextEngine(config)
        result = self.evaluate(
            engine,
            config,
            foreground("chrome.exe"),
            {"frame_samples": [[16, 16, 16, "Application"]] * 10, "sample_age_ms": 10},
            {"status": "playing", "title": "Video", "playback_type": "video", "source": "chrome.exe"},
        )
        self.assertEqual(result["state"], "VIDEO")

    def test_fullscreen_browser_video_without_playback_type(self):
        config = test_config()
        engine = pc_bridge.ContextEngine(config)
        fg = foreground("msedge.exe")
        fg["title"] = "Example - YouTube"
        result = self.evaluate(
            engine,
            config,
            fg,
            media={"status": "paused", "title": "Example", "source": "msedge.exe"},
        )
        self.assertEqual(result["state"], "VIDEO")

    def test_video_wins_over_idle(self):
        config = test_config()
        engine = pc_bridge.ContextEngine(config)
        result = self.evaluate(
            engine,
            config,
            foreground("vlc.exe"),
            media={"status": "paused", "title": "Movie", "source": "vlc.exe"},
            idle=301,
        )
        self.assertEqual(result["state"], "VIDEO")

    def test_background_music_does_not_turn_empty_video_app_into_video(self):
        config = test_config()
        engine = pc_bridge.ContextEngine(config)
        result = self.evaluate(
            engine,
            config,
            foreground("vlc.exe"),
            media={"status": "playing", "title": "Song", "source": "spotify.exe"},
        )
        self.assertEqual(result["state"], "DEFAULT")

    def test_foreground_spotify_becomes_video(self):
        config = test_config()
        engine = pc_bridge.ContextEngine(config)
        result = self.evaluate(
            engine,
            config,
            foreground("spotify.exe"),
            media={"status": "playing", "title": "Song", "playback_type": "music", "source": "spotify.exe"},
        )
        self.assertEqual(result["state"], "VIDEO")

    def test_background_spotify_does_not_override_work(self):
        config = test_config()
        engine = pc_bridge.ContextEngine(config)
        result = self.evaluate(
            engine,
            config,
            foreground("code.exe"),
            media={"status": "playing", "title": "Song", "playback_type": "music", "source": "spotify.exe"},
        )
        self.assertEqual(result["state"], "WORK")

    def test_fullscreen_browser_music_does_not_become_video(self):
        config = test_config()
        engine = pc_bridge.ContextEngine(config)
        fg = foreground("chrome.exe")
        fg["title"] = "Spotify - Web Player"
        result = self.evaluate(
            engine,
            config,
            fg,
            media={"status": "playing", "title": "Song", "source": "chrome.exe"},
        )
        self.assertEqual(result["state"], "DEFAULT")

    def test_idle_has_priority_over_work(self):
        config = test_config()
        engine = pc_bridge.ContextEngine(config)
        result = self.evaluate(engine, config, foreground("Code.exe"), idle=301)
        self.assertEqual(result["state"], "IDLE")

    def test_registered_work_app(self):
        config = test_config()
        engine = pc_bridge.ContextEngine(config)
        result = self.evaluate(engine, config, foreground("Code.exe"), idle=0)
        self.assertEqual(result["state"], "WORK")

    def test_game_exit_grace_holds_then_releases(self):
        config = test_config()
        config["detection"]["stability"]["GAME"]["exit_ms"] = 10000
        engine = pc_bridge.ContextEngine(config)
        active_fps = {"frame_samples": [[16, 16, 16, "Application"]] * 3, "sample_age_ms": 10}
        self.assertEqual(self.evaluate(engine, config, foreground("game.exe"), active_fps)["state"], "GAME")
        self.assertEqual(self.evaluate(engine, config, foreground("game.exe"))["state"], "GAME")
        engine.latches["GAME"].false_since = time.monotonic() - 11
        self.assertEqual(self.evaluate(engine, config, foreground("game.exe"))["state"], "DEFAULT")

    def test_game_focus_loss_exits_immediately(self):
        config = test_config()
        config["detection"]["stability"]["GAME"]["exit_ms"] = 10000
        engine = pc_bridge.ContextEngine(config)
        active_fps = {"frame_samples": [[16, 16, 16, "Application"]] * 3, "sample_age_ms": 10}
        self.assertEqual(self.evaluate(engine, config, foreground("game.exe"), active_fps)["state"], "GAME")
        result = self.evaluate(engine, config, foreground("explorer.exe"))
        self.assertEqual(result["state"], "DEFAULT")
        self.assertTrue(result["facts"]["game_focus_left"])


if __name__ == "__main__":
    unittest.main()
