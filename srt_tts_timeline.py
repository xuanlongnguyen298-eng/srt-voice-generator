"""Generate a WAV voice track from SRT cues using Blaze TTS timestamps.

Run ``python srt_tts_timeline.py --help`` for options. FFmpeg must be installed.
Each subtitle cue is synthesized separately; generated clips are cached so a
failed run can resume without repeating completed API requests.
"""
from __future__ import annotations

import argparse
import array
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import wave
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_SRT = BASE_DIR / "Dự án 4_translated.srt"
DEFAULT_OUTPUT = BASE_DIR / "Dự án 4_translated_voice.wav"
SAMPLE_RATE = 24_000
CHANNELS = 1
SAMPLE_WIDTH = 2
POLL_INTERVAL = 2.5
POLL_ATTEMPTS = 40
TIMESTAMP_RE = re.compile(
    r"^\s*(\d{2}:\d{2}:\d{2},\d{3})\s+-->\s+"
    r"(\d{2}:\d{2}:\d{2},\d{3})(?:\s+.*)?$"
)
THREAD_LOCAL = threading.local()
RATE_LIMIT_LOCK = threading.Lock()
RATE_LIMIT_UNTIL = 0.0
NEXT_TTS_REQUEST_AT = 0.0
TTS_REQUEST_INTERVAL = 1.0
MAX_RATE_LIMIT_RETRIES = 10
PUBLIC_TTS_MODELS = {"blaze-tts-3", "blaze-tts-2-max", "blaze-tts-2-mini"}


@dataclass(frozen=True)
class Cue:
    index: int
    start_ms: int
    end_ms: int
    text: str


def cue_duration_ms(cue: Cue) -> int:
    """Timeline length of one cue. Zero-length cues still count as 1 ms."""
    return max(cue.end_ms - cue.start_ms, 1)


def parse_timestamp(value: str) -> int:
    hours, minutes, remainder = value.split(":")
    seconds, milliseconds = remainder.split(",")
    return (
        ((int(hours) * 60 + int(minutes)) * 60 + int(seconds)) * 1000
        + int(milliseconds)
    )


def parse_srt(path: Path) -> list[Cue]:
    content = path.read_text(encoding="utf-8-sig")
    blocks = re.split(r"\r?\n\s*\r?\n", content.strip())
    cues: list[Cue] = []

    for block_number, block in enumerate(blocks, start=1):
        lines = block.splitlines()
        timing_line = next(
            (line_index for line_index, line in enumerate(lines) if TIMESTAMP_RE.match(line)),
            None,
        )
        if timing_line is None:
            raise ValueError(f"SRT block {block_number} has no valid timestamp line.")

        match = TIMESTAMP_RE.match(lines[timing_line])
        assert match is not None
        start_ms = parse_timestamp(match.group(1))
        end_ms = parse_timestamp(match.group(2))
        text = "\n".join(lines[timing_line + 1 :]).strip()
        if end_ms <= start_ms:
            raise ValueError(f"SRT block {block_number} ends before it starts.")
        if not text:
            raise ValueError(f"SRT block {block_number} has no subtitle text.")

        cues.append(Cue(len(cues) + 1, start_ms, end_ms, text))

    if not cues:
        raise ValueError(f"No subtitle cues found in {path}.")

    for previous, current in zip(cues, cues[1:]):
        if current.start_ms < previous.start_ms:
            raise ValueError(
                f"Subtitle cue {current.index} starts before cue {previous.index}; "
                "cues must be in chronological order."
            )

    return cues


def run_ffmpeg(command: list[str]) -> None:
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode:
        raise RuntimeError(
            f"FFmpeg failed: {result.stderr[-1200:].strip() or result.returncode}"
        )


def normalize_wav(ffmpeg: str, source: Path, destination: Path) -> None:
    run_ffmpeg(
        [
            ffmpeg,
            "-y",
            "-i",
            str(source),
            "-vn",
            "-ac",
            str(CHANNELS),
            "-ar",
            str(SAMPLE_RATE),
            "-c:a",
            "pcm_s16le",
            str(destination),
        ]
    )


def read_wav_frames(path: Path) -> tuple[bytes, int]:
    try:
        with wave.open(str(path), "rb") as audio:
            params = (
                audio.getnchannels(),
                audio.getsampwidth(),
                audio.getframerate(),
                audio.getcomptype(),
            )
            expected = (CHANNELS, SAMPLE_WIDTH, SAMPLE_RATE, "NONE")
            if params != expected:
                raise RuntimeError(f"Unexpected WAV format in {path.name}: {params}")
            return audio.readframes(audio.getnframes()), audio.getnframes()
    except (wave.Error, EOFError) as exc:
        raise RuntimeError(f"Could not read generated WAV {path}: {exc}") from exc


def tempo_filter(speed: float) -> str:
    filters: list[str] = []
    while speed > 2:
        filters.append("atempo=2")
        speed /= 2
    filters.append(f"atempo={speed:.8f}")
    return ",".join(filters)


def fit_audio_to_slot(
    ffmpeg: str,
    normalized: Path,
    fitted: Path,
    cue: Cue,
    slot_frames: int,
    frames: bytes,
    frame_count: int,
) -> tuple[bytes, int]:
    if slot_frames <= 0:
        raise RuntimeError(
            f"Cue {cue.index} has no exclusive time before the next cue starts; "
            "its audio cannot fit without overlapping another cue."
        )

    if frame_count <= slot_frames:
        return frames, frame_count

    required_speedup = frame_count / slot_frames
    speed = required_speedup * 1.01
    for _ in range(3):
        run_ffmpeg(
            [
                ffmpeg,
                "-y",
                "-i",
                str(normalized),
                "-af",
                tempo_filter(speed),
                "-ac",
                str(CHANNELS),
                "-ar",
                str(SAMPLE_RATE),
                "-c:a",
                "pcm_s16le",
                str(fitted),
            ]
        )
        frames, frame_count = read_wav_frames(fitted)
        if frame_count <= slot_frames:
            return frames, frame_count
        speed *= frame_count / slot_frames * 1.01

    raise RuntimeError(
        f"Cue {cue.index} still exceeds its timestamp slot after adjusting "
        "playback speed; no audio was cut or allowed to overlap."
    )


def download_tts_job(
    session: requests.Session, base_url: str, job_id: str, destination: Path
) -> None:
    last_status = "no response"
    for attempt in range(1, POLL_ATTEMPTS + 1):
        time.sleep(POLL_INTERVAL)
        for action in ("play", "download"):
            url = f"{base_url}/v1/tts/{job_id}/{action}"
            try:
                response = session.get(url, timeout=(15, 120))
            except requests.RequestException as exc:
                last_status = str(exc)
                continue

            content_type = response.headers.get("content-type", "").lower()
            if response.status_code == 200 and "json" not in content_type:
                if len(response.content) <= 44:
                    last_status = f"{action} returned an empty audio response"
                    continue
                temporary = destination.with_suffix(destination.suffix + ".tmp")
                temporary.write_bytes(response.content)
                temporary.replace(destination)
                return

            if response.status_code in (401, 403):
                raise RuntimeError(
                    f"TTS download was rejected ({response.status_code}); "
                    "check BLAZE_API_KEY."
                )
            last_status = f"{action} returned HTTP {response.status_code}"
            if response.status_code >= 500:
                last_status += f": {response.text[:200]}"

        if attempt % 5 == 0:
            print(f"  Waiting for TTS job {job_id} ({attempt}/{POLL_ATTEMPTS})...")

    raise RuntimeError(f"Timed out downloading TTS job {job_id}: {last_status}")


def download_speech_job(
    session: requests.Session, base_url: str, job_id: str, destination: Path
) -> None:
    status_url = f"{base_url}/v1/audio/speech/jobs/{job_id}"
    delay = 1.0
    for _ in range(120):
        try:
            response = session.get(status_url, timeout=(15, 60))
        except requests.RequestException as exc:
            raise RuntimeError(
                f"Could not check Blaze speech job {job_id}: {exc}"
            ) from exc
        if response.status_code != 200:
            raise RuntimeError(
                f"Blaze speech job status returned HTTP {response.status_code}: "
                f"{response.text[:300]}"
            )
        try:
            payload = response.json()
        except requests.JSONDecodeError as exc:
            raise RuntimeError("Blaze speech job status was not valid JSON.") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("Blaze speech job status had an unexpected response.")

        status = str(payload.get("status", "")).lower()
        if status in {"failed", "error", "cancelled", "canceled"}:
            raise RuntimeError(
                f"Blaze speech job {job_id} ended with status {status}: "
                f"{payload.get('error', payload.get('message', 'no error details'))}"
            )
        if status in {"completed", "succeeded", "success"}:
            content_url = payload.get("content_url")
            if not isinstance(content_url, str) or not content_url:
                content_url = f"/v1/audio/speech/jobs/{job_id}/content"
            if not content_url.startswith("/"):
                raise RuntimeError("Blaze returned an invalid speech content URL.")
            try:
                audio = session.get(
                    f"{base_url}{content_url}", timeout=(15, 120)
                )
            except requests.RequestException as exc:
                raise RuntimeError(
                    f"Could not download Blaze speech job {job_id}: {exc}"
                ) from exc
            content_type = audio.headers.get("content-type", "").lower()
            if audio.status_code != 200 or "json" in content_type or len(audio.content) <= 44:
                raise RuntimeError(
                    f"Blaze speech content download failed (HTTP {audio.status_code}, "
                    f"{content_type or 'unknown content type'})."
                )
            temporary = destination.with_suffix(destination.suffix + ".tmp")
            temporary.write_bytes(audio.content)
            temporary.replace(destination)
            return

        time.sleep(delay)
        delay = min(delay * 1.5, 10.0)

    raise RuntimeError(f"Timed out waiting for Blaze speech job {job_id}.")


def synthesize_cue(
    session: requests.Session,
    base_url: str,
    cue: Cue,
    destination: Path,
    settings: dict[str, Any],
    cache_key: str,
) -> None:
    metadata_path = destination.with_suffix(".json")
    if destination.exists() and metadata_path.exists():
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            metadata = {}
        if metadata.get("cache_key") == cache_key:
            print(f"  [{cue.index}] using cached audio")
            return

    is_public_model = settings["model"] in PUBLIC_TTS_MODELS
    if is_public_model:
        body = {
            "model": settings["model"],
            "input": cue.text,
            "voice": settings["speaker_id"],
            "language": settings["language"],
            "response_format": "wav",
            "speed": settings["audio_speed"],
            "normalization": "basic",
        }
        endpoint = "/v1/audio/speech/jobs"
    else:
        body = {
            "query": cue.text,
            "language": settings["language"],
            "audio_speed": settings["audio_speed"],
            "audio_quality": 64,
            "audio_format": "wav",
            "speaker_id": settings["speaker_id"],
            "normalization": "basic",
            "model": settings["model"],
        }
        endpoint = "/v1/tts"
    response: requests.Response | None = None
    for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
        wait_for_tts_request_slot()
        try:
            response = session.post(
                f"{base_url}{endpoint}", json=body, timeout=(15, 90)
            )
        except requests.RequestException as exc:
            raise RuntimeError(
                f"Blaze TTS request failed for cue {cue.index}: {exc}"
            ) from exc
        if response.status_code != 429:
            break
        retry_after = register_rate_limit(response)
        if retry_after is None or attempt == MAX_RATE_LIMIT_RETRIES:
            break
        print(
            f"  [{cue.index}] Blaze rate limited the request; "
            f"retrying after {retry_after:.0f}s.",
            flush=True,
        )

    assert response is not None

    if response.status_code not in (200, 202):
        raise RuntimeError(
            f"Blaze TTS rejected cue {cue.index} "
            f"(HTTP {response.status_code}): {response.text[:500]}"
        )
    try:
        payload = response.json()
    except requests.JSONDecodeError as exc:
        raise RuntimeError(f"Blaze returned invalid JSON for cue {cue.index}.") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"Blaze returned an unexpected response for cue {cue.index}.")

    job_id = payload.get("id") or payload.get("job_id")
    if not job_id:
        raise RuntimeError(f"Blaze response for cue {cue.index} has no job id.")

    print(f"  [{cue.index}] submitted TTS job {job_id}")
    if is_public_model:
        download_speech_job(session, base_url, str(job_id), destination)
    else:
        download_tts_job(session, base_url, str(job_id), destination)
    metadata_path.write_text(
        json.dumps({"cache_key": cache_key, "job_id": job_id}, indent=2),
        encoding="utf-8",
    )


def get_thread_session(token: str) -> requests.Session:
    session = getattr(THREAD_LOCAL, "session", None)
    if session is None:
        session = requests.Session()
        session.headers.update({"Authorization": f"Bearer {token}"})
        THREAD_LOCAL.session = session
    return session


def wait_for_tts_request_slot() -> None:
    global NEXT_TTS_REQUEST_AT
    while True:
        with RATE_LIMIT_LOCK:
            now = time.monotonic()
            next_allowed = max(RATE_LIMIT_UNTIL, NEXT_TTS_REQUEST_AT)
            delay = next_allowed - now
            if delay <= 0:
                NEXT_TTS_REQUEST_AT = now + TTS_REQUEST_INTERVAL
                return
            rate_limited = RATE_LIMIT_UNTIL >= NEXT_TTS_REQUEST_AT
        if rate_limited:
            print(
                f"Blaze rate limit: waiting {delay:.1f}s before retrying.",
                flush=True,
            )
        else:
            print(
                f"TTS request pacing: waiting {delay:.1f}s between submissions.",
                flush=True,
            )
        time.sleep(delay)


def register_rate_limit(response: requests.Response) -> float | None:
    global RATE_LIMIT_UNTIL
    retry_after: float | None = None
    try:
        payload = response.json()
    except requests.JSONDecodeError:
        payload = None

    if isinstance(payload, dict):
        detail = payload.get("detail")
        if isinstance(detail, dict):
            try:
                retry_after = float(detail.get("retry_after_seconds"))
            except (TypeError, ValueError):
                pass
    if retry_after is None:
        try:
            retry_after = float(response.headers.get("Retry-After", ""))
        except ValueError:
            return None
    if retry_after < 0:
        return None

    with RATE_LIMIT_LOCK:
        RATE_LIMIT_UNTIL = max(
            RATE_LIMIT_UNTIL,
            time.monotonic() + retry_after,
        )
    return retry_after


def build_timeline(
    cues: list[Cue],
    work_dir: Path,
    output: Path,
    ffmpeg: str,
) -> None:
    parts_dir = work_dir / "normalized"
    parts_dir.mkdir(parents=True, exist_ok=True)
    audio_parts: list[tuple[Cue, bytes, int]] = []

    for cue_index, cue in enumerate(cues):
        source = work_dir / f"cue_{cue.index:04d}.wav"
        normalized = parts_dir / f"cue_{cue.index:04d}.wav"
        normalize_wav(ffmpeg, source, normalized)
        frames, frame_count = read_wav_frames(normalized)
        slot_end_ms = cue.end_ms
        if cue_index + 1 < len(cues):
            slot_end_ms = min(slot_end_ms, cues[cue_index + 1].start_ms)
        slot_start_frame = round(cue.start_ms * SAMPLE_RATE / 1000)
        slot_end_frame = round(slot_end_ms * SAMPLE_RATE / 1000)
        slot_frames = slot_end_frame - slot_start_frame
        adjusted = parts_dir / f"cue_{cue.index:04d}_fitted.wav"
        frames, frame_count = fit_audio_to_slot(
            ffmpeg,
            normalized,
            adjusted,
            cue,
            slot_frames,
            frames,
            frame_count,
        )

        audio_parts.append((cue, frames, frame_count))

    total_frames = round(max(cue.end_ms for cue in cues) * SAMPLE_RATE / 1000)
    timeline = array.array("h", [0]) * total_frames
    output.parent.mkdir(parents=True, exist_ok=True)
    previous_end_frame = 0
    for cue, frames, frame_count in audio_parts:
        start_frame = round(cue.start_ms * SAMPLE_RATE / 1000)
        if start_frame + frame_count > total_frames:
            raise RuntimeError(f"Cue {cue.index} extends beyond the SRT duration.")
        if start_frame < previous_end_frame:
            raise RuntimeError(
                f"Cue {cue.index} would overlap audio from the previous cue; "
                "refusing to write an overlapping voice track."
            )
        samples = array.array("h")
        samples.frombytes(frames)
        for offset, sample in enumerate(samples):
            position = start_frame + offset
            timeline[position] = sample
        previous_end_frame = start_frame + frame_count

    with wave.open(str(output), "wb") as result:
        result.setnchannels(CHANNELS)
        result.setsampwidth(SAMPLE_WIDTH)
        result.setframerate(SAMPLE_RATE)
        if sys.byteorder != "little":
            timeline.byteswap()
        result.writeframes(timeline.tobytes())


def main() -> int:
    load_dotenv(BASE_DIR / ".env", override=True)
    parser = argparse.ArgumentParser(
        description="Generate a Blaze TTS voice track aligned to SRT timestamps."
    )
    parser.add_argument("srt", nargs="?", type=Path, default=DEFAULT_SRT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--work-dir",
        type=Path,
        help="Directory for resumable TTS audio parts (default: <output>_parts).",
    )
    parser.add_argument("--language", default="vi")
    parser.add_argument("--model", default="v2.0_pro")
    parser.add_argument(
        "--speaker-id",
        default="HN-Nar-Nam-BlazeNote",
        help="Speaker id from GET /v1/tts/list-speaker-ids, for example HN-Nar-Nam-BlazeNote.",
    )
    parser.add_argument("--audio-speed", type=float, default=1.0)
    parser.add_argument(
        "--limit-cues",
        type=int,
        help="Process only the first N subtitle cues (useful for previews).",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="Number of subtitle cues to synthesize concurrently (default: 4).",
    )
    parser.add_argument(
        "--api-base-url",
        default=os.getenv("BLAZE_BASE_URL", "https://api.blaze.vn"),
    )
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and summarize the SRT without calling Blaze or FFmpeg.",
    )
    args = parser.parse_args()

    try:
        cues = parse_srt(args.srt)
        if args.limit_cues is not None:
            if args.limit_cues < 1:
                raise ValueError("--limit-cues must be at least 1.")
            cues = cues[: args.limit_cues]
        duration_seconds = cues[-1].end_ms / 1000
        print(
            f"SRT: {args.srt}\n"
            f"Speaker: {args.speaker_id}\n"
            f"Cues: {len(cues)}\n"
            f"Timeline: {cues[0].start_ms / 1000:.3f}s - {duration_seconds:.3f}s "
            f"({duration_seconds / 60:.2f} min)"
        )
        overlaps = [
            (previous.index, current.index)
            for previous, current in zip(cues, cues[1:])
            if current.start_ms < previous.end_ms
        ]
        if overlaps:
            print(
                f"Warning: {len(overlaps)} cue overlap(s); overlapping speech will "
                "be kept separate by limiting each cue to before the next cue starts."
            )
        if args.dry_run:
            return 0

        if args.audio_speed <= 0:
            raise ValueError("--audio-speed must be greater than zero.")
        if args.workers < 1:
            raise ValueError("--workers must be at least 1.")
        ffmpeg = shutil.which(args.ffmpeg)
        if not ffmpeg:
            raise RuntimeError(
                f"FFmpeg executable not found: {args.ffmpeg}. Install FFmpeg or "
                "pass its path with --ffmpeg."
            )
        token = os.getenv("BLAZE_API_KEY")
        if not token:
            raise RuntimeError("Missing BLAZE_API_KEY in the project .env or environment.")

        base_url = args.api_base_url.rstrip("/")
        settings = {
            "language": args.language,
            "model": args.model,
            "speaker_id": args.speaker_id,
            "audio_speed": args.audio_speed,
        }
        work_dir = args.work_dir or args.output.with_name(
            f"{args.output.stem}_parts"
        )
        work_dir.mkdir(parents=True, exist_ok=True)

        def process_cue(cue: Cue) -> None:
            cache_payload = {
                "text": cue.text,
                "start_ms": cue.start_ms,
                "end_ms": cue.end_ms,
                "settings": settings,
            }
            cache_key = hashlib.sha256(
                json.dumps(cache_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest()
            destination = work_dir / f"cue_{cue.index:04d}.wav"
            print(
                f"[{cue.index}/{len(cues)}] {cue.text.replace(chr(10), ' ')}",
                flush=True,
            )
            synthesize_cue(
                get_thread_session(token),
                base_url,
                cue,
                destination,
                settings,
                cache_key,
            )

        print(f"Generating TTS with {args.workers} concurrent workers.")
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {executor.submit(process_cue, cue): cue for cue in cues}
            for future in as_completed(futures):
                cue = futures[future]
                try:
                    future.result()
                except Exception as exc:
                    for pending in futures:
                        if pending is not future:
                            pending.cancel()
                    raise RuntimeError(
                        f"Failed to synthesize cue {cue.index}: {exc}"
                    ) from exc
                print(
                    f"PROGRESS {cue.index} {cue_duration_ms(cue)}",
                    flush=True,
                )

        build_timeline(cues, work_dir, args.output, ffmpeg)
        print(f"Done: {args.output}")
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
