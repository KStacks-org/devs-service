from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


TOOL_DIR = Path(__file__).resolve().parents[1]


def load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, TOOL_DIR / filename)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load {filename}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


builder = load_module("build_manifest", "build_manifest.py")
encoder = load_module("encode_and_upload", "encode_and_upload.py")


class ManifestMappingTests(unittest.TestCase):
    def test_maps_approved_series_sections(self) -> None:
        self.assertEqual(
            builder.section_for("CPCS-202", "CPCS-202 Slides/example.MOV"),
            (1, "slides", "Slides"),
        )
        self.assertEqual(
            builder.section_for("CPCS-203", "FinalLab_203/example.MP4"),
            (4, "final-lab", "Final Lab"),
        )
        self.assertEqual(
            builder.section_for("CPIT-201", "CPIT-201_Ch10 #2.MOV"),
            (8, "chapter-10", "Chapter 10"),
        )
        self.assertEqual(
            builder.section_for("CPIT-201", "CPIT-201_Final2023.MOV"),
            (11, "exams-review", "Exams and Review"),
        )

    def test_rejects_unreviewed_sections(self) -> None:
        with self.assertRaises(ValueError):
            builder.section_for("CPIT-201", "CPIT-201_Ch9 #1.MOV")

    def test_natural_sort_keeps_numbered_lessons_in_order(self) -> None:
        values = ["lesson #10.MOV", "lesson #2.MOV", "lesson #1.MOV"]
        self.assertEqual(
            sorted(values, key=builder.natural_key),
            ["lesson #1.MOV", "lesson #2.MOV", "lesson #10.MOV"],
        )

    def test_cpcs_202_puts_numbered_chapters_before_supplements_and_finals(self) -> None:
        values = [
            "CPCS-202_Final2023.MOV",
            "Conv For to While.MOV",
            "CPCS-202_Ch2 #1.MOV",
            "CPCS-202_Ch1 #1.MOV",
        ]
        self.assertEqual(
            sorted(
                values,
                key=lambda path: builder.lesson_sort_key(
                    "CPCS-202", "slides", path
                ),
            ),
            [
                "CPCS-202_Ch1 #1.MOV",
                "CPCS-202_Ch2 #1.MOV",
                "Conv For to While.MOV",
                "CPCS-202_Final2023.MOV",
            ],
        )


class PlaylistTests(unittest.TestCase):
    def test_bandwidth_uses_valid_target_duration_windows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            segments = []
            for index, (duration, size) in enumerate(((6.0, 750), (6.0, 1500), (0.1, 100))):
                path = root / f"seg_{index:05d}.m4s"
                path.write_bytes(b"x" * size)
                segments.append(encoder.Segment(duration, path))
            average, peak, duration = encoder.bandwidth_metrics(6.0, segments)
            self.assertEqual(duration, 12.1)
            self.assertEqual(average, 1554)
            self.assertEqual(peak, 2099)


class ManifestValidationTests(unittest.TestCase):
    def test_loads_safe_manifest(self) -> None:
        payload = {
            "schemaVersion": 1,
            "source": {"remote": "devs-drive:"},
            "destination": {
                "remote": "devs-r2:bucket/pilots",
                "publicBaseUrl": "https://video.example.test/pilots",
                "corsOrigin": "https://devs.example.test",
            },
            "series": [
                {
                    "slug": "cpcs-202",
                    "sections": [
                        {
                            "position": 1,
                            "lessons": [
                                {
                                    "id": "cpcs-202-s01-l001",
                                    "position": 1,
                                    "title": {"en": "Lesson 1", "ar": None},
                                    "sourcePath": "CPCS-202/example.MOV",
                                    "sourceBytes": 100,
                                    "destinationPath": "cpcs-202/slides/lesson-001/2026-08-28-v1",
                                }
                            ],
                        }
                    ],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            config, lessons = encoder.load_manifest(path)
        self.assertEqual(config["source_remote"], "devs-drive:")
        self.assertEqual(lessons[0].item_id, "cpcs-202-s01-l001")

    def test_rejects_parent_path_traversal(self) -> None:
        with self.assertRaises(ValueError):
            encoder.safe_relative("../master.m3u8", "path")


if __name__ == "__main__":
    unittest.main()
