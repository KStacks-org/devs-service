#!/usr/bin/env python3
"""Encode and publish reviewed Google Drive lessons as immutable static HLS."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".mkv", ".webm"}
RENDITIONS = (
    ("880p", 1280, 880, 21, "3000k", "6000k", "4.0", "avc1.640028"),
    ("660p", 960, 660, 22, "1800k", "3600k", "3.1", "avc1.64001f"),
)


@dataclass(frozen=True)
class Lesson:
    item_id: str
    series_slug: str
    section_position: int
    lesson_position: int
    title_en: str
    source_path: str
    source_bytes: int
    destination_path: str


@dataclass(frozen=True)
class Segment:
    duration: float
    path: Path


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=Path("/srv/devs-video-import"),
        help="Persistent VPS workspace used for resumable downloads and logs",
    )
    parser.add_argument("--item", help="Process only one manifest item ID")
    parser.add_argument("--limit", type=int, help="Process at most this many pending lessons")
    parser.add_argument("--cpu-set", help="Optional taskset CPU list, for example 0-2")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--keep-work", action="store_true")
    return parser.parse_args()


def required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value.strip()


def safe_relative(value: Any, label: str) -> str:
    text = required_string(value, label).strip("/")
    path = PurePosixPath(text)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError(f"{label} must be a safe relative path")
    return path.as_posix()


def load_manifest(path: Path) -> tuple[dict[str, str], list[Lesson]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schemaVersion") != 1:
        raise ValueError("manifest schemaVersion must be 1")

    source = payload.get("source", {})
    destination = payload.get("destination", {})
    config = {
        "source_remote": required_string(source.get("remote"), "source.remote"),
        "destination_remote": required_string(
            destination.get("remote"), "destination.remote"
        ),
        "public_base_url": required_string(
            destination.get("publicBaseUrl"), "destination.publicBaseUrl"
        ).rstrip("/"),
        "cors_origin": str(destination.get("corsOrigin", "")).strip(),
    }

    lessons: list[Lesson] = []
    seen_ids: set[str] = set()
    seen_destinations: set[str] = set()
    for series in payload.get("series", []):
        series_slug = safe_relative(series.get("slug"), "series.slug")
        if "/" in series_slug:
            raise ValueError("series.slug cannot contain a slash")
        for section in series.get("sections", []):
            section_position = int(section.get("position", 0))
            if section_position < 1:
                raise ValueError("section.position must be positive")
            for lesson in section.get("lessons", []):
                item_id = required_string(lesson.get("id"), "lesson.id")
                if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", item_id):
                    raise ValueError(f"unsafe lesson ID: {item_id}")
                destination_path = safe_relative(
                    lesson.get("destinationPath"), "lesson.destinationPath"
                )
                source_path = safe_relative(lesson.get("sourcePath"), "lesson.sourcePath")
                source_bytes = int(lesson.get("sourceBytes", 0))
                lesson_position = int(lesson.get("position", 0))
                if source_bytes < 1 or lesson_position < 1:
                    raise ValueError(f"invalid size or position for {item_id}")
                if PurePosixPath(source_path).suffix.lower() not in VIDEO_EXTENSIONS:
                    raise ValueError(f"unsupported video extension for {item_id}")
                if item_id in seen_ids or destination_path in seen_destinations:
                    raise ValueError(f"duplicate lesson ID or destination: {item_id}")
                seen_ids.add(item_id)
                seen_destinations.add(destination_path)
                lessons.append(
                    Lesson(
                        item_id=item_id,
                        series_slug=series_slug,
                        section_position=section_position,
                        lesson_position=lesson_position,
                        title_en=required_string(lesson.get("title", {}).get("en"), "title.en"),
                        source_path=source_path,
                        source_bytes=source_bytes,
                        destination_path=destination_path,
                    )
                )

    if not lessons:
        raise ValueError("manifest does not contain any lessons")
    return config, lessons


def remote_path(remote: str, relative: str) -> str:
    return f"{remote.rstrip('/')}/{relative.lstrip('/')}"


def command_exists(name: str) -> None:
    if shutil.which(name) is None:
        raise RuntimeError(f"required command is not installed: {name}")


def run(command: list[str], *, log: Path | None = None, capture: bool = False) -> str:
    printable = " ".join(command)
    print(f"+ {printable}", flush=True)
    if log:
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as target:
            target.write(f"\n$ {printable}\n")
            target.flush()
            result = subprocess.run(command, stdout=target, stderr=subprocess.STDOUT, text=True)
    else:
        result = subprocess.run(
            command,
            capture_output=capture,
            text=True,
        )
    if result.returncode != 0:
        details = result.stderr.strip() if capture and result.stderr else ""
        raise RuntimeError(
            f"command failed with exit code {result.returncode}: {printable}"
            + (f"\n{details}" if details else "")
        )
    return result.stdout if capture else ""


def state_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
            if isinstance(row, dict):
                rows.append(row)
        except json.JSONDecodeError:
            continue
    return rows


def append_state(path: Path, lesson: Lesson, status: str, **details: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "itemId": lesson.item_id,
        "status": status,
        "sourcePath": lesson.source_path,
        "destinationPath": lesson.destination_path,
        "recordedAt": datetime.now(timezone.utc).isoformat(),
        **details,
    }
    with path.open("a", encoding="utf-8") as target:
        target.write(json.dumps(row, ensure_ascii=False) + "\n")
        target.flush()
        os.fsync(target.fileno())


def completed_ids(path: Path) -> set[str]:
    return {
        str(row.get("itemId"))
        for row in state_rows(path)
        if row.get("status") == "published"
    }


def ensure_disk_capacity(work_dir: Path, source_bytes: int) -> None:
    free = shutil.disk_usage(work_dir).free
    required = max(source_bytes * 5, source_bytes + 4 * 1024**3)
    if free < required:
        raise RuntimeError(
            f"insufficient free disk: need at least {required} bytes, have {free}"
        )


def inspect_source(path: Path) -> dict[str, Any]:
    output = run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=index,codec_name,codec_type,width,height,pix_fmt,r_frame_rate:format=duration,size,bit_rate",
            "-of",
            "json",
            str(path),
        ],
        capture=True,
    )
    metadata = json.loads(output)
    streams = metadata.get("streams", [])
    videos = [stream for stream in streams if stream.get("codec_type") == "video"]
    audios = [stream for stream in streams if stream.get("codec_type") == "audio"]
    duration = float(metadata.get("format", {}).get("duration", 0))
    if len(videos) != 1:
        raise RuntimeError(f"expected one video stream, found {len(videos)}")
    if not audios:
        raise RuntimeError("source has no audio stream")
    if duration <= 0:
        raise RuntimeError("source duration is missing or invalid")
    return metadata


def encode(source: Path, output: Path, log: Path, cpu_set: str | None) -> None:
    for name, *_ in RENDITIONS:
        (output / name).mkdir(parents=True, exist_ok=True)

    filters = (
        "[0:v:0]fps=30,split=2[v880][v660];"
        "[v880]scale=1280:880:force_original_aspect_ratio=decrease:force_divisible_by=2,"
        "pad=1280:880:(ow-iw)/2:(oh-ih)/2[v880out];"
        "[v660]scale=960:660:force_original_aspect_ratio=decrease:force_divisible_by=2,"
        "pad=960:660:(ow-iw)/2:(oh-ih)/2[v660out]"
    )
    command: list[str] = []
    if cpu_set:
        command.extend(["taskset", "-c", cpu_set])
    command.extend(
        [
            "nice",
            "-n",
            "10",
            "ffmpeg",
            "-hide_banner",
            "-nostdin",
            "-y",
            "-i",
            str(source),
            "-filter_complex",
            filters,
            "-map",
            "[v880out]",
            "-map",
            "0:a:0",
            "-map",
            "[v660out]",
            "-map",
            "0:a:0",
            "-c:v",
            "libx264",
            "-preset",
            "slow",
            "-pix_fmt",
            "yuv420p",
            "-profile:v:0",
            "high",
            "-level:v:0",
            "4.0",
            "-profile:v:1",
            "high",
            "-level:v:1",
            "3.1",
            "-crf:v:0",
            "21",
            "-maxrate:v:0",
            "3000k",
            "-bufsize:v:0",
            "6000k",
            "-crf:v:1",
            "22",
            "-maxrate:v:1",
            "1800k",
            "-bufsize:v:1",
            "3600k",
            "-g",
            "180",
            "-keyint_min",
            "180",
            "-sc_threshold",
            "0",
            "-force_key_frames:v:0",
            "expr:gte(t,n_forced*6)",
            "-force_key_frames:v:1",
            "expr:gte(t,n_forced*6)",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-ac",
            "2",
            "-ar",
            "48000",
            "-f",
            "hls",
            "-hls_time",
            "6",
            "-hls_playlist_type",
            "vod",
            "-hls_segment_type",
            "fmp4",
            "-hls_flags",
            "independent_segments",
            "-hls_fmp4_init_filename",
            "init.mp4",
            "-hls_segment_filename",
            str(output / "%v" / "seg_%05d.m4s"),
            "-master_pl_name",
            "master.m3u8",
            "-var_stream_map",
            "v:0,a:0,name:880p v:1,a:1,name:660p",
            str(output / "%v" / "playlist.m3u8"),
        ]
    )
    run(command, log=log)


def parse_playlist(path: Path) -> tuple[float, list[Segment]]:
    target_duration = 0.0
    pending_duration: float | None = None
    segments: list[Segment] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line.startswith("#EXT-X-TARGETDURATION:"):
            target_duration = float(line.split(":", 1)[1])
        elif line.startswith("#EXTINF:"):
            pending_duration = float(line.split(":", 1)[1].split(",", 1)[0])
        elif line and not line.startswith("#") and pending_duration is not None:
            segment_path = path.parent / line
            if not segment_path.is_file():
                raise RuntimeError(f"playlist segment is missing: {segment_path}")
            segments.append(Segment(pending_duration, segment_path))
            pending_duration = None
    if target_duration <= 0 or not segments:
        raise RuntimeError(f"playlist has no target duration or segments: {path}")
    if "#EXT-X-ENDLIST" not in path.read_text(encoding="utf-8"):
        raise RuntimeError(f"VOD playlist is missing EXT-X-ENDLIST: {path}")
    return target_duration, segments


def bandwidth_metrics(target: float, segments: list[Segment]) -> tuple[int, int, float]:
    total_duration = sum(segment.duration for segment in segments)
    total_bytes = sum(segment.path.stat().st_size for segment in segments)
    average = math.ceil(total_bytes * 8 / total_duration)
    minimum_window = target * 0.5
    maximum_window = target * 1.5
    peak = 0.0
    for start in range(len(segments)):
        duration = 0.0
        size = 0
        for segment in segments[start:]:
            duration += segment.duration
            size += segment.path.stat().st_size
            if duration > maximum_window + 1e-6:
                break
            if duration + 1e-6 >= minimum_window:
                peak = max(peak, size * 8 / duration)
    if peak <= 0:
        raise RuntimeError("could not calculate RFC peak bandwidth")
    return average, math.ceil(peak), total_duration


def write_master(output: Path) -> dict[str, dict[str, int | float]]:
    lines = ["#EXTM3U", "#EXT-X-VERSION:7", "#EXT-X-INDEPENDENT-SEGMENTS"]
    metrics: dict[str, dict[str, int | float]] = {}
    duration_reference: float | None = None
    segment_reference: int | None = None
    for name, width, height, _crf, _maxrate, _bufsize, _level, codec in RENDITIONS:
        target, segments = parse_playlist(output / name / "playlist.m3u8")
        average, peak, duration = bandwidth_metrics(target, segments)
        if duration_reference is not None and abs(duration - duration_reference) > 0.25:
            raise RuntimeError("rendition durations differ by more than 0.25 seconds")
        if segment_reference is not None and len(segments) != segment_reference:
            raise RuntimeError("rendition segment counts do not match")
        duration_reference = duration
        segment_reference = len(segments)
        metrics[name] = {
            "averageBandwidth": average,
            "peakBandwidth": peak,
            "durationSeconds": duration,
            "segments": len(segments),
        }
        lines.extend(
            [
                (
                    f'#EXT-X-STREAM-INF:BANDWIDTH={peak},AVERAGE-BANDWIDTH={average},'
                    f'RESOLUTION={width}x{height},FRAME-RATE=30.000,'
                    f'CODECS="{codec},mp4a.40.2",CLOSED-CAPTIONS=NONE'
                ),
                f"{name}/playlist.m3u8",
            ]
        )
    (output / "master.m3u8").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return metrics


def validate_decoding(output: Path, log: Path) -> None:
    for name, *_ in RENDITIONS:
        run(
            [
                "ffmpeg",
                "-hide_banner",
                "-v",
                "error",
                "-i",
                str(output / name / "playlist.m3u8"),
                "-f",
                "null",
                "-",
            ],
            log=log,
        )


def write_checksums(output: Path) -> None:
    rows: list[str] = []
    for path in sorted(output.rglob("*")):
        if not path.is_file() or path.name == "SHA256SUMS.txt":
            continue
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        rows.append(f"{digest.hexdigest()}  {path.relative_to(output).as_posix()}")
    (output / "SHA256SUMS.txt").write_text("\n".join(rows) + "\n", encoding="utf-8")


def rclone_destination_args() -> list[str]:
    return ["--s3-no-check-bucket"]


def remote_master_exists(destination: str) -> bool:
    result = subprocess.run(
        ["rclone", "lsf", destination, "--files-only", *rclone_destination_args()],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"unable to inspect destination: {result.stderr.strip()}")
    return "master.m3u8" in result.stdout.splitlines()


def upload(output: Path, destination: str) -> None:
    common = [
        "--immutable",
        "--s3-no-check-bucket",
        "--transfers",
        "8",
        "--checkers",
        "16",
        "--stats",
        "30s",
    ]
    cache = ["--header-upload", "Cache-Control: public, max-age=31536000, immutable"]
    stages = (
        ("*.m4s", "video/iso.segment"),
        ("*.mp4", "video/mp4"),
        ("*/playlist.m3u8", "application/vnd.apple.mpegurl"),
        ("SHA256SUMS.txt", "text/plain; charset=utf-8"),
    )
    for pattern, content_type in stages:
        run(
            [
                "rclone",
                "copy",
                str(output),
                destination,
                "--include",
                pattern,
                "--header-upload",
                f"Content-Type: {content_type}",
                *cache,
                *common,
            ]
        )

    run(
        [
            "rclone",
            "check",
            str(output),
            destination,
            "--size-only",
            "--one-way",
            "--exclude",
            "/master.m3u8",
            *rclone_destination_args(),
        ]
    )
    run(
        [
            "rclone",
            "copyto",
            str(output / "master.m3u8"),
            remote_path(destination, "master.m3u8"),
            "--immutable",
            "--header-upload",
            "Content-Type: application/vnd.apple.mpegurl",
            *cache,
            *rclone_destination_args(),
        ]
    )
    run(
        [
            "rclone",
            "check",
            str(output),
            destination,
            "--size-only",
            "--one-way",
            *rclone_destination_args(),
        ]
    )


def validate_public_url(url: str, cors_origin: str) -> None:
    headers = {"Origin": cors_origin} if cors_origin else {}
    last_error: Exception | None = None
    body = ""
    for attempt, delay in enumerate((0, 5, 15, 30, 60, 120, 240), start=1):
        if delay:
            print(
                f"public URL not ready yet (attempt {attempt}), retrying in {delay}s: {url}",
                flush=True,
            )
            time.sleep(delay)
        try:
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read(4096).decode("utf-8")
                if response.status != 200 or not body.startswith("#EXTM3U"):
                    raise RuntimeError(
                        f"public master validation failed: HTTP {response.status}"
                    )
                if cors_origin:
                    allowed = response.headers.get("Access-Control-Allow-Origin", "")
                    if allowed not in {"*", cors_origin}:
                        raise RuntimeError(
                            "public master response is missing the expected CORS header"
                        )
            last_error = None
            break
        except Exception as error:
            last_error = error
    if last_error:
        raise RuntimeError(f"public master validation failed after retries: {last_error}")
    if "880p/playlist.m3u8" not in body or "660p/playlist.m3u8" not in body:
        raise RuntimeError("public master does not contain both expected renditions")


def process_lesson(
    lesson: Lesson,
    config: dict[str, str],
    work_dir: Path,
    state_file: Path,
    cpu_set: str | None,
    keep_work: bool,
) -> None:
    item_dir = work_dir / "items" / lesson.item_id
    source_dir = item_dir / "source"
    output = item_dir / "output"
    logs = work_dir / "logs"
    source_dir.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    ensure_disk_capacity(work_dir, lesson.source_bytes)

    source_suffix = PurePosixPath(lesson.source_path).suffix.lower()
    source = source_dir / f"lesson{source_suffix}"
    destination = remote_path(config["destination_remote"], lesson.destination_path)
    public_url = remote_path(config["public_base_url"], lesson.destination_path + "/master.m3u8")
    if remote_master_exists(destination):
        raise RuntimeError(
            f"destination is already published and will not be overwritten: {destination}"
        )

    append_state(state_file, lesson, "started")
    run(
        [
            "rclone",
            "copyto",
            remote_path(config["source_remote"], lesson.source_path),
            str(source),
            "--transfers",
            "1",
            "--checkers",
            "4",
            "--stats",
            "30s",
        ]
    )
    if source.stat().st_size != lesson.source_bytes:
        raise RuntimeError(
            f"downloaded size mismatch: expected {lesson.source_bytes}, got {source.stat().st_size}"
        )
    metadata = inspect_source(source)
    (logs / f"{lesson.item_id}-source.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    append_state(state_file, lesson, "downloaded", bytes=source.stat().st_size)

    if (output / "master.m3u8").exists():
        raise RuntimeError(
            "local output already contains a master playlist; remove the unpublished item workspace or use a new version"
        )
    encode(source, output, logs / f"{lesson.item_id}-ffmpeg.log", cpu_set)
    metrics = write_master(output)
    validate_decoding(output, logs / f"{lesson.item_id}-validation.log")
    write_checksums(output)
    append_state(state_file, lesson, "validated", renditions=metrics)

    upload(output, destination)
    validate_public_url(public_url, config["cors_origin"])
    append_state(
        state_file,
        lesson,
        "published",
        manifestUrl=public_url,
        renditions=metrics,
    )
    print(f"published item={lesson.item_id} url={public_url}", flush=True)
    if not keep_work:
        resolved_item = item_dir.resolve()
        expected_parent = (work_dir / "items").resolve()
        if resolved_item.parent != expected_parent:
            raise RuntimeError("refusing to clean an unexpected workspace path")
        shutil.rmtree(resolved_item)


def selected_lessons(
    lessons: Iterable[Lesson], item_id: str | None, limit: int | None
) -> list[Lesson]:
    selected = [lesson for lesson in lessons if not item_id or lesson.item_id == item_id]
    if item_id and not selected:
        raise ValueError(f"manifest item was not found: {item_id}")
    if limit is not None:
        if limit < 1:
            raise ValueError("--limit must be positive")
        selected = selected[:limit]
    return selected


def main() -> int:
    args = arguments()
    for command in ("ffmpeg", "ffprobe", "rclone", "nice"):
        command_exists(command)
    if args.cpu_set:
        command_exists("taskset")

    config, manifest_lessons = load_manifest(args.manifest)
    work_dir = args.work_dir.resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    state_file = work_dir / "state.jsonl"
    done = completed_ids(state_file)
    pending = [lesson for lesson in manifest_lessons if lesson.item_id not in done]
    selected = selected_lessons(pending, args.item, args.limit)

    print(
        f"manifest={args.manifest} total={len(manifest_lessons)} "
        f"completed={len(done)} selected={len(selected)}",
        flush=True,
    )
    for lesson in selected:
        print(
            f"item={lesson.item_id} source={lesson.source_path} "
            f"destination={lesson.destination_path}",
            flush=True,
        )
    if args.dry_run:
        return 0

    for lesson in selected:
        try:
            process_lesson(
                lesson,
                config,
                work_dir,
                state_file,
                args.cpu_set,
                args.keep_work,
            )
        except Exception as error:
            append_state(
                state_file,
                lesson,
                "failed",
                error=str(error),
                traceback=traceback.format_exc(),
            )
            print(f"failed item={lesson.item_id}: {error}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
