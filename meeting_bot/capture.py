"""Capture meeting playback, then create a protocol after Ctrl-C."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from .audio_store import AudioStore, FRAME_BYTES, SAMPLE_RATE, safe_id
from .live import LiveTranscriber
from .process import process_session


def build_mac_helper() -> Path:
    source = Path(__file__).resolve().parent.parent / "native" / "mac_audio_capture.swift"
    build = Path(__file__).resolve().parent.parent / ".build"
    build.mkdir(exist_ok=True)
    binary = build / "mac_audio_capture"
    if not binary.exists() or binary.stat().st_mtime < source.stat().st_mtime:
        env = os.environ.copy()
        env["CLANG_MODULE_CACHE_PATH"] = str(build / "clang-cache")
        env["SWIFT_MODULE_CACHE_PATH"] = str(build / "swift-cache")
        subprocess.run(["swiftc", "-parse-as-library", str(source), "-o", str(binary)],
                       check=True, env=env)
    return binary


def capture_mac_audio(data: Path, session: str, chrome_pid: int | None = None,
                      duration: float | None = None) -> Path:
    binary = build_mac_helper()
    directory = data / safe_id(session)
    directory.mkdir(parents=True, exist_ok=True)
    wav_path = directory / "speaker_mixed.wav"
    if wav_path.exists():
        raise FileExistsError(f'Recording already exists: {wav_path}')
    command = [str(binary), str(wav_path)]
    if chrome_pid is not None:
        command.append(str(chrome_pid))
    helper = subprocess.Popen(command, start_new_session=True)
    try:
        try:
            result = helper.wait(timeout=duration)
        except (KeyboardInterrupt, subprocess.TimeoutExpired):
            if helper.poll() is None:
                helper.send_signal(signal.SIGINT)
            result = helper.wait(timeout=30)
    finally:
        if helper.poll() is None:
            helper.terminate()
            try:
                helper.wait(timeout=5)
            except subprocess.TimeoutExpired:
                helper.kill()
                helper.wait()
    if result != 0:
        raise RuntimeError("macOS audio capture failed. Check Screen Recording permission for Terminal")
    (directory / 'capture.done').write_text('saved', encoding='utf-8')
    from .openai_process import audio_stats, write_json
    stats = audio_stats(wav_path)
    write_json(directory / 'audio_stats.json', stats)
    if stats['peak'] < 10:
        raise RuntimeError(f'Записана тишина. Проверьте звук встречи. WAV сохранён: {wav_path}')
    return wav_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Capture meeting playback and create a protocol")
    parser.add_argument("--list-devices", action="store_true")
    parser.add_argument("--capture-mode", choices=("loopback", "mac"), default="loopback")
    parser.add_argument("--device", help="Exact loopback microphone name")
    parser.add_argument("--session", help="Unique session ID")
    parser.add_argument("--data", type=Path, default=Path("data"))
    parser.add_argument("--provider", choices=("local", "openai"), default="local")
    parser.add_argument("--whisper-model", type=Path)
    parser.add_argument("--ollama-model")
    parser.add_argument("--diarization-model", type=Path)
    parser.add_argument("--summary-model", default="gpt-4.1-mini")
    parser.add_argument("--transcribe-model", default="gpt-4o-transcribe-diarize")
    parser.add_argument("--live", action="store_true", help="Print draft transcription about every 10 seconds")
    parser.add_argument("--chrome-pid", type=int)
    parser.add_argument("--duration-seconds", type=float, help="Stop the standalone macOS audio check after N seconds")
    parser.add_argument("--record-only", action="store_true", help="Save audio without API processing")
    args = parser.parse_args()
    if not args.list_devices and not args.session:
        parser.error("--session is required")
    if args.capture_mode == "mac" and not args.list_devices:
        if sys.platform != "darwin":
            parser.error("mac capture mode requires macOS")
        if args.duration_seconds is not None and args.duration_seconds <= 0:
            parser.error('--duration-seconds must be positive')
        if not args.record_only and args.provider == "local" and (not args.whisper_model or not args.ollama_model):
            parser.error("Local mode requires --whisper-model and --ollama-model")
        wav_path = capture_mac_audio(args.data, args.session, args.chrome_pid, args.duration_seconds)
        if args.record_only:
            print(wav_path)
            return
        if args.provider == "openai":
            from .openai_process import process_wav
            print(process_wav(args.data, args.session, wav_path,
                              args.transcribe_model, args.summary_model))
        else:
            print(process_session(args.data, args.session, args.whisper_model, args.ollama_model,
                                  diarization_model=args.diarization_model))
        return
    try:
        import numpy as np
        import soundcard as sc
    except ImportError as exc:
        raise SystemExit("Install requirements-capture.txt locally") from exc

    devices = sc.all_microphones(include_loopback=True)
    if args.list_devices:
        for device in devices:
            print(device.name)
        return
    if not args.session or not args.device:
        parser.error("--session and --device are required")
    if not args.record_only and args.provider == "local" and (not args.whisper_model or not args.ollama_model):
        parser.error("Local mode requires --whisper-model and --ollama-model")
    if args.live and args.provider != "local":
        parser.error("Live transcription preview currently requires the local provider")
    matches = [device for device in devices if device.name == args.device]
    if len(matches) != 1:
        parser.error("Loopback device not found; use --list-devices")

    store = AudioStore(args.data)
    live = LiveTranscriber(args.data / args.session, args.whisper_model) if args.live else None
    pending = bytearray()
    pending_start = 0.0
    elapsed_seconds = 0.0
    print("Capturing meeting playback. Press Ctrl-C when the meeting ends.")
    try:
        with matches[0].recorder(samplerate=SAMPLE_RATE, channels=1) as recorder:
            while True:
                samples = recorder.record(numframes=SAMPLE_RATE)
                mono = np.asarray(samples, dtype=np.float32).reshape(-1)
                pcm = (np.clip(mono, -1, 1) * 32767).astype("<i2").tobytes()
                first_timestamp = (time.monotonic_ns() - 1_000_000_000) // 100
                for index in range(0, len(pcm) - FRAME_BYTES + 1, FRAME_BYTES):
                    timestamp = first_timestamp + (index // FRAME_BYTES) * 200_000
                    store.append_frame(args.session, "mixed", timestamp, pcm[index:index + FRAME_BYTES])
                if live:
                    pending.extend(pcm)
                    elapsed_seconds += len(pcm) / (SAMPLE_RATE * 2)
                    if len(pending) >= SAMPLE_RATE * 2 * 10:
                        live.submit(pending_start, bytes(pending))
                        pending.clear()
                        pending_start = elapsed_seconds
    except KeyboardInterrupt:
        pass
    if live:
        if pending:
            live.submit(pending_start, bytes(pending))
        live.close()
        if live.dropped_blocks:
            print(f"Live preview skipped {live.dropped_blocks} blocks; final transcription uses full audio.")
    wav_files = store.finalize(args.session)
    if args.record_only:
        print(wav_files[0])
        return
    if args.provider == "openai":
        from .openai_process import process_wav
        print(process_wav(args.data, args.session, wav_files[0],
                          args.transcribe_model, args.summary_model))
    else:
        print(process_session(args.data, args.session, args.whisper_model, args.ollama_model,
                              diarization_model=args.diarization_model))


if __name__ == "__main__":
    main()
