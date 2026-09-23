"""Join Google Meet using a dedicated, manually authenticated Chrome profile."""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse


NAME = re.compile(r"Your name|Enter your name|Укажите сво[её] имя|Как вас зовут|Атыңыз", re.I)
JOIN = re.compile(r"^(?:Join now|Ask to join|Join meeting|Join here too|Присоединиться|"
                  r"Попросить присоединиться|Подключиться также на этом устройстве|Қосылу)$", re.I)
LEAVE = re.compile(r"Leave call|Leave meeting|Покинуть (?:видео)?встречу|Выйти из звонка|Выйти из встречи", re.I)
DENIED = re.compile(r"can't join|cannot join|not allowed to join|request to join was denied|не можете присоединиться|запрос.*отклонен|не допущены|вам запрещено участвовать", re.I)
VERIFY = re.compile(r"verify.*(?:human|robot)|captcha|подтвердите.*(?:человек|робот)", re.I)
NO_MEDIA = re.compile(r"Continue with (?:mic|microphone) and camera off|Продолжить с отключенными микрофоном и камерой", re.I)
SIGN_IN = re.compile(r"^(?:Sign in|Войти|Войти в аккаунт|Кіру)$", re.I)
ENDED = re.compile(r"you left the meeting|you.ve been removed|meeting has ended|call has ended|вы покинули|вас удалили|встреча завершена|видеовстреча завершена|звонок завершен", re.I)
MUTE = re.compile(r"^(?:Turn off microphone|Выключить микрофон|Отключить микрофон)", re.I)
CAMERA_OFF = re.compile(r"^(?:Turn off camera|Выключить камеру|Отключить камеру)", re.I)


def login(profile: Path) -> None:
    """First login uses ordinary Chrome, with no automation or credential handling."""
    chrome = Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
    if not chrome.is_file():
        raise RuntimeError('Google Chrome не найден в /Applications')
    profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    process = subprocess.Popen([str(chrome), f'--user-data-dir={profile.resolve()}',
                                '--no-first-run', '--no-default-browser-check',
                                'https://accounts.google.com/'],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               start_new_session=True)
    print('Войдите в отдельный аккаунт бота в открывшемся Chrome. '
          'Затем полностью закройте это окно Chrome. Пароль вводится только в Google.', flush=True)
    try:
        result = process.wait()
        if result:
            raise RuntimeError('Chrome не запустился. Закройте другой запуск профиля бота и повторите.')
    except KeyboardInterrupt:
        print('Окно авторизации оставлено открытым. Закройте его перед запуском бота.')


def require_account(page) -> None:
    if urlparse(page.url).hostname == 'accounts.google.com' or visible_button(page, SIGN_IN) or \
            page.get_by_role('link', name=SIGN_IN).count() or \
            page.get_by_role('textbox', name=NAME).count():
        raise RuntimeError('Профиль не авторизован. Выполните ./start_meet_login.command, '
                           'войдите в отдельный Google-аккаунт и закройте окно авторизации.')


def mute_media(page) -> None:
    for pattern in (MUTE, CAMERA_OFF):
        button = visible_button(page, pattern)
        if button:
            button.click()


def meeting_finished(page, missing_since: float | None, now: float) -> tuple[bool, float | None]:
    if page.is_closed():
        return True, missing_since
    if visible_button(page, LEAVE):
        return False, None
    if ENDED.search(page_text(page)):
        return True, missing_since
    missing_since = now if missing_since is None else missing_since
    return now - missing_since >= 30, missing_since


def participant_count(page) -> int | None:
    button = visible_button(page, re.compile(r'Участники|People|Participants|Show everyone', re.I))
    if not button:
        return None
    label = button.inner_text() + ' ' + (button.get_attribute('aria-label') or '')
    match = re.search(r'\b(\d+)\b', label)
    return int(match[1]) if match else None


def valid_meet_url(value: str) -> bool:
    parsed = urlparse(value)
    return (parsed.scheme == "https" and parsed.netloc == "meet.google.com" and
            re.fullmatch(r"/[a-z]{3}-[a-z]{4}-[a-z]{3}/?", parsed.path) is not None)


def visible_button(page, pattern: re.Pattern[str]):
    buttons = page.get_by_role("button", name=pattern)
    for index in range(buttons.count()):
        button = buttons.nth(index)
        if button.is_visible() and button.is_enabled():
            return button
    return None


def dismiss_info_dialog(page) -> None:
    dialogs = page.get_by_role("dialog")
    for index in range(dialogs.count()):
        dialog = dialogs.nth(index)
        if not dialog.is_visible():
            continue
        name = dialog.get_attribute("aria-label") or dialog.inner_text()
        if re.search(r"sign in with.*google|войдите.*аккаунт Google|Переключение звонка на это устройство", name, re.I):
            button = dialog.get_by_role("button", name=re.compile(r"^(?:OK|ОК|Got it)$", re.I))
            if button.count() and button.first.is_visible():
                button.first.click()


def page_text(page) -> str:
    return page.locator("body").inner_text(timeout=2000)


def wait_for_admission(page, timeout_seconds: int) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if page.is_closed():
            raise RuntimeError("Meet browser window closed before admission")
        if visible_button(page, LEAVE):
            return
        body = page_text(page)
        if DENIED.search(body):
            raise RuntimeError("Google Meet отказал во входе. Повторный запрос автоматически не отправляется.")
        if VERIFY.search(body):
            raise RuntimeError("Google Meet requires human verification; complete it in the browser")
        time.sleep(1)
    raise TimeoutError("The organizer did not admit the guest before the timeout")


def main() -> None:
    parser = argparse.ArgumentParser(description="Join Google Meet and create a meeting protocol")
    parser.add_argument("--meeting-url")
    parser.add_argument("--login", action="store_true", help="Manual first sign-in in the dedicated Chrome profile")
    parser.add_argument("--meeting-url-file", type=Path, default=Path(".meet-meeting-url"))
    parser.add_argument("--profile", type=Path, default=Path(".meet-bot-profile"))
    parser.add_argument("--join-only", action="store_true")
    parser.add_argument('--record-only', action='store_true', help='Join and save WAV; process it later')
    parser.add_argument('--max-duration-seconds', type=float, help='Optional recording duration for a live check')
    parser.add_argument('--alone-timeout-seconds', type=int, default=60,
                        help='Leave after all other participants leave (0 disables)')
    parser.add_argument("--join-timeout-seconds", type=int, default=300)
    parser.add_argument("--session", default=f"meet_{datetime.now():%Y%m%d_%H%M%S_%f}")
    parser.add_argument("--data", type=Path, default=Path("data"))
    parser.add_argument("--capture-mode", choices=("mac", "loopback"),
                        default="mac" if sys.platform == "darwin" else "loopback")
    parser.add_argument("--device", help="Loopback input name when capture-mode=loopback")
    parser.add_argument("--provider", choices=("openai", "local"), default="openai")
    parser.add_argument("--whisper-model", type=Path)
    parser.add_argument("--ollama-model")
    parser.add_argument("--diarization-model", type=Path)
    parser.add_argument("--summary-model", default="gpt-4.1-mini")
    parser.add_argument("--transcribe-model", default="gpt-4o-transcribe-diarize")
    parser.add_argument("--browser-channel", default="chrome")
    args = parser.parse_args()
    if args.login:
        login(args.profile)
        return
    if args.join_timeout_seconds <= 0:
        parser.error('--join-timeout-seconds must be positive')
    if args.max_duration_seconds is not None and args.max_duration_seconds <= 0:
        parser.error('--max-duration-seconds must be positive')
    if args.alone_timeout_seconds < 0:
        parser.error('--alone-timeout-seconds must be nonnegative')
    from .audio_store import safe_id
    safe_id(args.session)
    if not args.join_only and (args.data / args.session).exists():
        parser.error('Session already exists; choose a new --session to preserve recordings')

    meeting_url = args.meeting_url
    if not meeting_url and args.meeting_url_file.is_file():
        meeting_url = args.meeting_url_file.read_text(encoding="utf-8").strip()
    if not meeting_url:
        meeting_url = input("Paste Google Meet link: ").strip()
    if not valid_meet_url(meeting_url):
        parser.error("Use a Google Meet link like https://meet.google.com/abc-defg-hij")
    if not args.join_only:
        if args.capture_mode == "loopback" and not args.device:
            parser.error("Loopback capture requires --device")
        if not args.record_only and args.provider == "openai" and not os.environ.get("OPENAI_API_KEY"):
            parser.error("Set OPENAI_API_KEY before using the OpenAI processing mode")
        if not args.record_only and args.provider == "local" and (not args.whisper_model or not args.ollama_model):
            parser.error("Local processing requires --whisper-model and --ollama-model")
        if args.capture_mode == 'mac':
            from .capture import build_mac_helper
            build_mac_helper()
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise SystemExit("Install requirements-client.txt locally") from exc

    capture: subprocess.Popen | None = None

    def event(state: str) -> None:
        if args.join_only:
            return
        directory = args.data / args.session
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / 'events.jsonl').open('a', encoding='utf-8') as log:
            log.write(json.dumps({'time': datetime.now().astimezone().isoformat(), 'state': state}) + '\n')

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch_persistent_context(
            user_data_dir=str(args.profile.resolve()), headless=False,
            channel=args.browser_channel, permissions=[], accept_downloads=False,
            chromium_sandbox=True,
            ignore_default_args=['--use-mock-keychain', '--password-store=basic'],
        )
        browser.set_default_timeout(5000)
        # Deny actual device access even when the dedicated account previously granted it.
        cdp = browser.browser.new_browser_cdp_session()
        try:
            for permission in ('microphone', 'camera'):
                cdp.send('Browser.setPermission', {'permission': {'name': permission},
                         'setting': 'denied', 'origin': 'https://meet.google.com'})
            page = browser.pages[0] if browser.pages else browser.new_page()
            page.goto(meeting_url, wait_until="domcontentloaded")
            deadline = time.monotonic() + args.join_timeout_seconds
            while time.monotonic() < deadline:
                dismiss_info_dialog(page)
                require_account(page)
                body = page_text(page)
                if DENIED.search(body):
                    raise RuntimeError('Google Meet отказал во входе; проверьте доступ аккаунта к встрече.')
                silent = visible_button(page, NO_MEDIA)
                if silent:
                    silent.click()
                mute_media(page)
                button = visible_button(page, JOIN)
                if button:
                    button.click()
                    break
                if VERIFY.search(page_text(page)):
                    raise RuntimeError("Google Meet requires human verification in the browser")
                time.sleep(1)
            else:
                raise TimeoutError("Google Meet join button did not appear")

            print("Join request sent. The organizer may need to admit the bot.")
            wait_for_admission(page, args.join_timeout_seconds)
            print("Bot joined the Google Meet meeting.")
            event('admitted')
            joined_at = time.monotonic()
            if not args.join_only:
                command = [sys.executable, "-m", "meeting_bot.capture",
                           "--capture-mode", args.capture_mode,
                           "--session", args.session, "--data", str(args.data),
                           "--provider", args.provider]
                if args.record_only:
                    command.append('--record-only')
                if args.capture_mode == 'mac':
                    process_info = cdp.send('SystemInfo.getProcessInfo')['processInfo']
                    chrome_pid = next(int(p['id']) for p in process_info if p['type'] == 'browser')
                    command += ['--chrome-pid', str(chrome_pid)]
                if args.device:
                    command += ["--device", args.device]
                if args.provider == "openai":
                    command += ["--summary-model", args.summary_model,
                                "--transcribe-model", args.transcribe_model]
                else:
                    command += ["--whisper-model", str(args.whisper_model),
                                "--ollama-model", args.ollama_model]
                    if args.diarization_model:
                        command += ["--diarization-model", str(args.diarization_model)]
                flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
                capture = subprocess.Popen(command, creationflags=flags,
                                           start_new_session=os.name != 'nt')
                if args.capture_mode == 'mac':
                    ready = args.data / args.session / 'speaker_mixed.wav.ready'
                    deadline = time.monotonic() + 60
                    while not ready.exists():
                        if capture.poll() is not None:
                            raise RuntimeError('Не удалось начать запись. Проверьте разрешение macOS на запись звука.')
                        if time.monotonic() > deadline:
                            raise TimeoutError('macOS не запустила запись за 60 секунд. Проверьте окно разрешений.')
                        time.sleep(0.25)
                print("Recording started. Press Ctrl-C when the meeting ends.", flush=True)
                event('recording')
            missing_since = None
            alone_since = None
            had_company = False
            while True:
                if args.max_duration_seconds and time.monotonic() - joined_at >= args.max_duration_seconds:
                    break
                try:
                    ended, missing_since = meeting_finished(page, missing_since, time.monotonic())
                    if not ended and args.alone_timeout_seconds:
                        count = participant_count(page)
                        had_company = had_company or (count is not None and count > 1)
                        if had_company and count == 1:
                            alone_since = alone_since or time.monotonic()
                            if time.monotonic() - alone_since >= args.alone_timeout_seconds:
                                event('last_participant_left')
                                break
                        else:
                            alone_since = None
                except Exception:
                    if page.is_closed() or not browser.browser.is_connected():
                        break
                    raise
                if ended:
                    break
                if capture and capture.poll() is not None:
                    raise RuntimeError(f"Audio capture stopped unexpectedly (exit {capture.returncode})")
                time.sleep(2)
        except KeyboardInterrupt:
            pass
        finally:
            event('stopping')
            previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
            try:
                if capture and capture.poll() is None:
                    capture.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT)
                    print('Останавливаю запись и готовлю протокол...', flush=True)
                    # Finalize WAV before destroying the Chrome audio source.
                    deadline = time.monotonic() + 35
                    done = args.data / args.session / 'capture.done'
                    while args.capture_mode == 'mac' and not done.exists() and capture.poll() is None:
                        if time.monotonic() > deadline:
                            break
                        time.sleep(0.1)
                try:
                    browser.close()
                except Exception:
                    pass
                if capture:
                    capture.wait()
            finally:
                signal.signal(signal.SIGINT, previous)
            if capture and capture.returncode:
                event('failed')
                raise RuntimeError(f'Запись или обработка завершилась с ошибкой ({capture.returncode}). '
                                   f'Сохранённые файлы: {args.data / args.session}')
            if capture:
                event('recorded' if args.record_only else 'complete')


if __name__ == "__main__":
    main()
