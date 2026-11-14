"""Still-anchor (定妆图) support in the locked queue: bundle, task row, completion.

Needs the ai-short-drama skill checkout: set AI_SHORT_DRAMA_SKILL, or it is
found under ~/.codex/skills or ./.claude/skills. Skipped otherwise.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

try:
    from drama_pipeline.planner import locate_short_drama_skill
    locate_short_drama_skill()
    SKILL_AVAILABLE = True
except FileNotFoundError:
    SKILL_AVAILABLE = False

if SKILL_AVAILABLE:
    from drama_pipeline import media_contract_bridge as bridge
    from drama_pipeline.planner import locate_short_drama_skill
    from PIL import Image

JOB = {
    "contract_version": "1.0",
    "job_kind": "still",
    "job_id": "still_char_lin_front",
    "entity_id": "char_lin_ya",
    "entity_kind": "character",
    "view": "front_full",
    "generation_target": {"provider": "qwen", "entrypoint": "comfyui", "model_id": "qwen-image-2.1", "aspect_ratio": "3:4"},
    "prompt": "Character identity sheet for 林雅. Locked identity: 二十七歲，短黑髮，灰色舊外套。",
    "reference_files": ["refs/lin_front_ref.png"],
    "outputs": {
        "image_path": "assets/anchors/char_lin_ya_front_full.png",
        "receipt_path": "media-jobs/stills/still_char_lin_front.receipt.json",
        "prompt_path": "media-jobs/stills/still_char_lin_front.prompt.txt",
    },
    "retry_policy": {"max_attempts": 3},
    "locks": ["二十七歲", "短黑髮", "灰色舊外套"],
}


class FakeStore:
    """The few ProjectStore methods the still path uses."""

    def __init__(self, root: Path, pack: dict):
        self.root = root.resolve()
        self._pack = pack
        self.queue_saved = None
        self.events = []

    def pack(self):
        return self._pack

    def relative(self, path: Path) -> str:
        return Path(path).resolve().relative_to(self.root).as_posix()

    def artifact(self, relative: str) -> Path:
        return (self.root / relative).resolve()

    def save_queue(self, data):
        self.queue_saved = data

    def log(self, event, payload=None):
        self.events.append(event)


@unittest.skipUnless(SKILL_AVAILABLE, "ai-short-drama skill checkout not found")
class StillAnchorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.pack = {"project": {"max_reference_assets": 8}}
        self.store = FakeStore(self.root, self.pack)

    def tearDown(self):
        self.tmp.cleanup()

    def write_bundle(self, job=None, manifest_hash=None):
        job = job if job is not None else JOB
        stills = self.root / "media-jobs" / "stills"
        stills.mkdir(parents=True, exist_ok=True)
        job_file = stills / "still_char_lin_front.json"
        job_file.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
        digest = manifest_hash or bridge._contract_module().canonical_sha256(job)
        manifest = {
            "contract_version": "1.0",
            "jobs": [{"job_id": job["job_id"], "file": job_file.name, "canonical_sha256": digest}],
        }
        (stills / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        return stills

    def test_valid_still_bundle_loads_in_manifest_order(self):
        self.write_bundle()
        items = bridge.load_still_bundle(self.store)
        self.assertEqual([item["index"] for item in items], [1])
        self.assertEqual(items[0]["job"]["job_id"], "still_char_lin_front")

    def test_project_without_stills_has_no_still_bundle(self):
        self.assertEqual(bridge.load_still_bundle(self.store), [])
        self.assertEqual(bridge.still_json_paths(self.store), set())

    def test_changed_still_job_is_refused(self):
        self.write_bundle(manifest_hash="0" * 64)
        with self.assertRaisesRegex(ValueError, "changed after the manifest"):
            bridge.load_still_bundle(self.store)

    def test_still_job_with_video_output_fails_its_contract(self):
        bad = json.loads(json.dumps(JOB))
        bad["outputs"]["video_path"] = "videos/x.mp4"
        self.write_bundle(job=bad)
        with self.assertRaisesRegex(ValueError, "fails its contract"):
            bridge.load_still_bundle(self.store)

    def test_still_task_row_is_an_image_task_with_no_dependencies(self):
        self.write_bundle()
        item = bridge.load_still_bundle(self.store)[0]
        task = bridge.still_task(self.store, item)
        self.assertEqual(task["kind"], bridge.STILL_KIND)
        self.assertEqual(task["id"], "locked_still_char_lin_front")
        self.assertEqual(task["expected_path"], "assets/anchors/char_lin_ya_front_full.png")
        self.assertEqual(task["depends_on"], [])
        self.assertEqual(task["payload"]["media"], "image")
        self.assertNotIn(".mp4", task["accepted_extensions"])

    def test_closed_world_counts_still_files(self):
        stills = self.write_bundle()
        paths = bridge.still_json_paths(self.store)
        self.assertIn((stills / "manifest.json").resolve(), paths)
        self.assertIn((stills / "still_char_lin_front.json").resolve(), paths)

    def _png(self, name="still.png"):
        path = self.root / name
        Image.new("RGB", (64, 96), (120, 110, 100)).save(path)
        return path

    def test_completion_persists_the_image_and_marks_complete(self):
        self.write_bundle()
        item = bridge.load_still_bundle(self.store)[0]
        task = bridge.still_task(self.store, item)
        queue = {"tasks": [task]}
        bridge.complete_still_task(self.store, queue, task, self._png(), qc_passed=True, note="ok", cost=None)
        self.assertEqual(task["status"], "complete")
        self.assertEqual(task["result"]["width"], 64)
        self.assertTrue((self.root / task["expected_path"]).is_file())
        bridge.verify_completed_still_task(self.store, task)

    def test_completion_refuses_failed_qc_bad_image_and_foreign_receipt(self):
        self.write_bundle()
        task = bridge.still_task(self.store, bridge.load_still_bundle(self.store)[0])
        queue = {"tasks": [task]}
        with self.assertRaisesRegex(ValueError, "Visual QC"):
            bridge.complete_still_task(self.store, queue, task, self._png(), qc_passed=False, note="", cost=None)
        junk = self.root / "junk.png"
        junk.write_bytes(b"not an image")
        with self.assertRaisesRegex(ValueError, "not a readable image"):
            bridge.complete_still_task(self.store, queue, task, junk, qc_passed=True, note="", cost=None)
        receipt = self.root / "receipt.json"
        receipt.write_text(json.dumps({"job_id": "other", "job_sha256": task["job_sha256"]}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "does not bind"):
            bridge.complete_still_task(
                self.store, queue, task, self._png(), qc_passed=True, note="", cost=None, receipt_path=receipt
            )
        self.assertNotEqual(task["status"], "complete")

    def test_skill_compiled_stills_load_in_the_kit(self):
        """The skill's compiler output must satisfy the kit's loader, unchanged."""
        import subprocess

        skill = locate_short_drama_skill()
        example = skill / "examples" / "production-pack.example.json"
        out = self.root / "media-jobs" / "stills"
        subprocess.run(
            [sys.executable, str(skill / "scripts" / "still_job_compile.py"), str(example),
             "--out", str(out), "--provider", "qwen", "--entrypoint", "comfyui",
             "--model-id", "qwen-image-2.1", "--aspect-ratio", "3:4"],
            check=True, capture_output=True,
        )
        self.pack = json.loads(example.read_text(encoding="utf-8"))
        self.store = FakeStore(self.root, self.pack)
        items = bridge.load_still_bundle(self.store)
        self.assertEqual(len(items), 8)
        tasks = [bridge.still_task(self.store, item) for item in items]
        self.assertTrue(all(task["kind"] == bridge.STILL_KIND for task in tasks))
        self.assertEqual(len({task["id"] for task in tasks}), 8)


if __name__ == "__main__":
    unittest.main()
