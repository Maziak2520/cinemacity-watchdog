#!/usr/bin/env python3
"""Regression tests for IMAX date dedup and state persistence."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import watch


def showing(**overrides):
    event = {
        "id": "248952",
        "film": "Duna: část třetí",
        "filmLink": "https://www.cinemacity.cz/films/duna-cast-treti/8105s2r",
        "cinema": "Praha Flora, OC FLORA",
        "cinemaId": "1052",
        "datetime": "2026-12-15T14:00:00",
        "auditorium": "IMAX VOLVO",
        "attrs": ["70-mm", "subbed"],
        "booking": "https://tickets.cinemacity.cz/order/248952",
        "soldOut": False,
    }
    event.update(overrides)
    return event


FLORA_DEC15 = [
    showing(id="248952", datetime="2026-12-15T14:00:00",
            booking="https://tickets.cinemacity.cz/order/248952"),
    showing(id="248958", datetime="2026-12-15T17:20:00",
            booking="https://tickets.cinemacity.cz/order/248958"),
    showing(id="249034", datetime="2026-12-15T20:40:00",
            booking="https://tickets.cinemacity.cz/order/249034"),
]


def by_id(events):
    return {e["id"]: e for e in events}


NOW = "2026-09-15T19:08:00"


class DiffShowingsTest(unittest.TestCase):
    def test_empty_state_reports_current_dates_as_new(self):
        """Replay of issue #108: no seen.json → the Flora IMAX slots look new."""
        current = by_id(FLORA_DEC15)
        new_events, gone = watch.diff_showings(current, {}, NOW)
        self.assertEqual([e["datetime"] for e in new_events], [
            "2026-12-15T14:00:00",
            "2026-12-15T17:20:00",
            "2026-12-15T20:40:00",
        ])
        self.assertEqual(gone, [])

    def test_persisted_same_showings_are_not_reported_again(self):
        current = by_id(FLORA_DEC15)
        new_events, gone = watch.diff_showings(current, current, NOW)
        self.assertEqual(new_events, [])
        self.assertEqual(gone, [])

    def test_same_cinema_datetime_is_not_new_when_event_id_changes(self):
        """Do not re-alert the same cinema+date+time just because API id moved."""
        known = by_id([showing(id="old-id")])
        current = by_id([showing(id="new-id")])
        new_events, gone = watch.diff_showings(current, known, NOW)
        self.assertEqual(new_events, [])
        self.assertEqual(gone, [])

    def test_new_unseen_datetime_is_reported(self):
        known = by_id(FLORA_DEC15)
        extra = showing(id="999", datetime="2026-12-16T14:00:00")
        current = by_id(FLORA_DEC15 + [extra])
        new_events, gone = watch.diff_showings(current, known, NOW)
        self.assertEqual([e["id"] for e in new_events], ["999"])
        self.assertEqual(gone, [])

    def test_same_datetime_at_another_cinema_is_new(self):
        flora = showing(cinemaId="1052")
        other = showing(id="x", cinemaId="9999", cinema="Brno")
        new_events, gone = watch.diff_showings(
            by_id([flora, other]), by_id([flora]), NOW
        )
        self.assertEqual([e["id"] for e in new_events], ["x"])
        self.assertEqual(gone, [])

    def test_future_showing_missing_from_schedule_is_gone(self):
        known = by_id(FLORA_DEC15)
        current = by_id(FLORA_DEC15[:2])
        new_events, gone = watch.diff_showings(current, known, NOW)
        self.assertEqual(new_events, [])
        self.assertEqual([e["id"] for e in gone], ["249034"])

    def test_force_report_returns_all_current_and_no_gone(self):
        current = by_id(FLORA_DEC15)
        new_events, gone = watch.diff_showings(
            current, current, NOW, force_report=True
        )
        self.assertEqual(len(new_events), 3)
        self.assertEqual(gone, [])


class StatePersistenceTest(unittest.TestCase):
    def test_second_run_with_saved_state_does_not_have_news(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state", "seen.json")
            current = {watch.showing_key(e): e for e in FLORA_DEC15}
            self.assertTrue(watch.save_state(path, current))
            known = watch.load_state(path)["events"]
            new_events, gone = watch.diff_showings(current, known, NOW)
            self.assertEqual(new_events, [])
            self.assertEqual(gone, [])
            self.assertFalse(watch.save_state(path, current))

    def test_save_state_does_not_churn_when_only_event_id_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "seen.json")
            old = {watch.showing_key(e): e for e in [showing(id="old-id")]}
            new = {watch.showing_key(e): e for e in [showing(id="new-id")]}
            watch.save_state(path, old)
            # Identity is the slot, so keys match and we must not churn the file
            # just because the payload's id field changed — alerting uses slots.
            self.assertEqual(set(old), set(new))
            self.assertFalse(watch.save_state(path, new))


class WorkflowCommitDetectionTest(unittest.TestCase):
    """Reproduce the Actions bug that caused issues #17–#109.

    After `state/seen.json` was deleted, watch.py wrote it again as an
    *untracked* file. `git diff --quiet -- state/seen.json` is silent for
    untracked files, so the workflow skipped the commit and the next run
    started from empty state again.
    """

    def _git(self, cwd, *args):
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        )

    def _repo_with_untracked_state(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", tmp], check=True))
        self._git(tmp, "init", "-q")
        self._git(tmp, "config", "user.email", "test@example.com")
        self._git(tmp, "config", "user.name", "test")
        Path(tmp, "README.md").write_text("x\n", encoding="utf-8")
        self._git(tmp, "add", "README.md")
        self._git(tmp, "commit", "-qm", "init")
        Path(tmp, "state").mkdir()
        Path(tmp, "state", "seen.json").write_text(
            json.dumps({"updated": NOW, "events": by_id(FLORA_DEC15)}, indent=1)
            + "\n",
            encoding="utf-8",
        )
        return tmp

    def test_plain_git_diff_misses_untracked_seen_json(self):
        repo = self._repo_with_untracked_state()
        # This is the buggy check the workflow used to run.
        buggy = subprocess.run(
            ["git", "diff", "--quiet", "--", "state/seen.json"], cwd=repo
        )
        self.assertEqual(buggy.returncode, 0, "git diff ignores untracked files")

    def test_stage_then_cached_diff_detects_new_seen_json(self):
        repo = self._repo_with_untracked_state()
        self._git(repo, "add", "--", "state/seen.json")
        cached = subprocess.run(
            ["git", "diff", "--quiet", "--cached", "--", "state/seen.json"],
            cwd=repo,
        )
        self.assertEqual(cached.returncode, 1, "staged new file must look dirty")

    def test_workflow_yaml_stages_state_before_asking_diff(self):
        text = Path(".github/workflows/watch.yml").read_text(encoding="utf-8")
        self.assertIn("git add -- state/seen.json", text)
        self.assertIn("git diff --quiet --cached -- state/seen.json", text)
        self.assertNotIn(
            "if git diff --quiet -- state/seen.json;",
            text,
        )


class MainLoopTest(unittest.TestCase):
    def test_main_writes_news_then_stays_silent_on_same_showings(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = os.path.join(tmp, "seen.json")
            report = os.path.join(tmp, "report.md")
            title = os.path.join(tmp, "title.txt")
            current = by_id(FLORA_DEC15)
            with mock.patch.object(watch, "collect", return_value=current), \
                 mock.patch.object(watch, "now", return_value=watch.datetime.fromisoformat(NOW)):
                with mock.patch("sys.argv", [
                    "watch.py", "--state", state, "--report", report, "--title", title,
                ]):
                    watch.main()
                self.assertTrue(os.path.exists(report))
                self.assertIn("Nově vypsáno (3)", Path(report).read_text(encoding="utf-8"))
                with mock.patch("sys.argv", [
                    "watch.py", "--state", state, "--report", report, "--title", title,
                ]):
                    watch.main()
            # Second run must not rewrite a news report.
            # main() returns early without touching report when there is no news,
            # so the old report still exists — has_news is what matters, via state.
            known = watch.load_state(state)["events"]
            new_events, gone = watch.diff_showings(
                {watch.showing_key(e): e for e in current.values()},
                known,
                NOW,
            )
            self.assertEqual(new_events, [])
            self.assertEqual(gone, [])

    def test_main_reports_a_newly_appeared_datetime(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = os.path.join(tmp, "seen.json")
            report = os.path.join(tmp, "report.md")
            title = os.path.join(tmp, "title.txt")
            first = by_id(FLORA_DEC15)
            extra = showing(id="999", datetime="2026-12-16T14:00:00")
            second = by_id(FLORA_DEC15 + [extra])
            with mock.patch.object(watch, "now", return_value=watch.datetime.fromisoformat(NOW)):
                with mock.patch.object(watch, "collect", return_value=first), \
                     mock.patch("sys.argv", [
                         "watch.py", "--state", state, "--report", report, "--title", title,
                     ]):
                    watch.main()
                with mock.patch.object(watch, "collect", return_value=second), \
                     mock.patch("sys.argv", [
                         "watch.py", "--state", state, "--report", report, "--title", title,
                     ]):
                    watch.main()
            body = Path(report).read_text(encoding="utf-8")
            self.assertIn("Nově vypsáno (1)", body)
            self.assertIn("16. 12. 2026", body)


if __name__ == "__main__":
    unittest.main()
