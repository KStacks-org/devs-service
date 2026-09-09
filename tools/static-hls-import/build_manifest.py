#!/usr/bin/env python3
"""Build a reviewable static-HLS import manifest from the approved Drive folders."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from datetime import date, datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any


VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".mkv", ".webm"}
SERIES_ORDER = ("CPCS-202", "CPCS-203", "CPIT-201")


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-remote", default="devs-drive:")
    parser.add_argument(
        "--drive-root-folder-id",
        help="Only needed when the rclone remote is not already rooted at the shared folder",
    )
    parser.add_argument(
        "--destination-remote",
        default="devs-r2:devs-video-delivery-test/pilots",
    )
    parser.add_argument(
        "--public-base-url",
        default="https://devs-video-test.fawazabdullah.dev/pilots",
    )
    parser.add_argument(
        "--cors-origin",
        default="https://devs-staging.fawazabdullah.dev",
    )
    parser.add_argument("--version", default=f"{date.today().isoformat()}-v1")
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def natural_key(value: str) -> list[tuple[int, int | str]]:
    return [
        (0, int(part)) if part.isdigit() else (1, part.casefold())
        for part in re.split(r"(\d+)", value)
    ]


def lesson_sort_key(
    series: str, section_slug: str, path: str
) -> tuple[int, int, list[tuple[int, int | str]]]:
    name = PurePosixPath(path).name
    natural = natural_key(path)
    if series == "CPCS-202" and section_slug == "slides":
        chapter = re.search(r"_Ch(\d+)\b", name, re.IGNORECASE)
        if chapter:
            return 0, int(chapter.group(1)), natural
        if "final" in name.casefold():
            return 2, 0, natural
        return 1, 0, natural
    if series == "CPCS-202" and section_slug == "labs":
        lab = re.search(r"_Lab(\d+)\b", name, re.IGNORECASE)
        if lab:
            return 0, int(lab.group(1)), natural
        return 1, 0, natural
    return 0, 0, natural


def title_from_path(series: str, path: str) -> str:
    title = PurePosixPath(path).stem.replace("_", " ")
    title = re.sub(rf"^{re.escape(series)}[\s_-]*", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\s+", " ", title).strip()
    return title or PurePosixPath(path).stem


def section_for(series: str, path: str) -> tuple[int, str, str]:
    if series == "CPCS-202":
        if path.startswith("CPCS-202 Slides/"):
            return 1, "slides", "Slides"
        if path.startswith("CPCS-202 Labs/"):
            return 2, "labs", "Labs"
    elif series == "CPCS-203":
        mappings = (
            ("Slides_203/", 1, "slides", "Slides"),
            ("Mid_203/", 2, "midterm", "Midterm"),
            ("FinalExam_203/", 3, "final-exam", "Final Exam"),
            ("FinalLab_203/", 4, "final-lab", "Final Lab"),
        )
        for prefix, position, section_slug, title in mappings:
            if path.startswith(prefix):
                return position, section_slug, title
    elif series == "CPIT-201":
        chapter = re.search(r"_Ch(\d+)\b", PurePosixPath(path).name, re.IGNORECASE)
        if chapter:
            number = int(chapter.group(1))
            chapter_order = {2: 1, 3: 2, 4: 3, 5: 4, 6: 5, 7: 6, 8: 7, 10: 8}
            if number not in chapter_order:
                raise ValueError(f"unreviewed CPIT-201 chapter in {path}")
            return chapter_order[number], f"chapter-{number}", f"Chapter {number}"
        name = PurePosixPath(path).name.casefold()
        if "appendix" in name:
            return 9, "appendices", "Appendices"
        if "homework" in name:
            return 10, "homework", "Homework"
        if any(term in name for term in ("exam", "final", "dr.nabil")):
            return 11, "exams-review", "Exams and Review"
    raise ValueError(f"video is not covered by the approved section mapping: {series}/{path}")


def list_remote(remote: str, root_folder_id: str | None) -> list[dict[str, Any]]:
    command = ["rclone", "lsjson", remote, "-R", "--files-only"]
    if root_folder_id:
        command.extend(["--drive-root-folder-id", root_folder_id])
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "rclone inventory failed")
    rows = json.loads(result.stdout)
    if not isinstance(rows, list):
        raise RuntimeError("rclone returned an unexpected inventory")
    return rows


def build(args: argparse.Namespace) -> dict[str, Any]:
    sections_by_series: dict[str, dict[tuple[int, str, str], list[dict[str, Any]]]] = {
        series: {} for series in SERIES_ORDER
    }
    for row in list_remote(args.source_remote, args.drive_root_folder_id):
        path = str(row.get("Path", ""))
        parts = PurePosixPath(path).parts
        if len(parts) < 2 or parts[0] not in sections_by_series:
            continue
        if PurePosixPath(path).suffix.lower() not in VIDEO_EXTENSIONS:
            continue
        series = parts[0]
        relative = PurePosixPath(*parts[1:]).as_posix()
        section = section_for(series, relative)
        sections_by_series[series].setdefault(section, []).append(
            {
                "sourcePath": path,
                "sourceBytes": int(row.get("Size", 0)),
                "title": title_from_path(series, relative),
            }
        )

    series_rows: list[dict[str, Any]] = []
    for series in SERIES_ORDER:
        section_rows: list[dict[str, Any]] = []
        for (section_position, section_slug, section_title), lessons in sorted(
            sections_by_series[series].items()
        ):
            lessons.sort(
                key=lambda row: lesson_sort_key(
                    series, section_slug, str(row["sourcePath"])
                )
            )
            lesson_rows: list[dict[str, Any]] = []
            for lesson_position, lesson in enumerate(lessons, start=1):
                item_id = f"{series.casefold()}-s{section_position:02d}-l{lesson_position:03d}"
                lesson_rows.append(
                    {
                        "id": item_id,
                        "position": lesson_position,
                        "title": {"en": lesson["title"], "ar": None},
                        "sourcePath": lesson["sourcePath"],
                        "sourceBytes": lesson["sourceBytes"],
                        "destinationPath": (
                            f"{series.casefold()}/{section_slug}/"
                            f"lesson-{lesson_position:03d}/{args.version}"
                        ),
                    }
                )
            section_rows.append(
                {
                    "position": section_position,
                    "slug": section_slug,
                    "title": {"en": section_title, "ar": None},
                    "lessons": lesson_rows,
                }
            )
        series_rows.append(
            {
                "slug": series.casefold(),
                "title": {"en": series, "ar": None},
                "sections": section_rows,
            }
        )

    return {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "source": {"remote": args.source_remote},
        "destination": {
            "remote": args.destination_remote,
            "publicBaseUrl": args.public_base_url.rstrip("/"),
            "corsOrigin": args.cors_origin,
        },
        "series": series_rows,
    }


def main() -> int:
    args = arguments()
    if args.output.exists():
        raise SystemExit(f"refusing to overwrite existing manifest: {args.output}")
    payload = build(args)
    lesson_count = sum(
        len(section["lessons"])
        for series in payload["series"]
        for section in series["sections"]
    )
    if lesson_count != 258:
        raise SystemExit(f"expected 258 approved videos, found {lesson_count}")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"wrote {lesson_count} lessons to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
