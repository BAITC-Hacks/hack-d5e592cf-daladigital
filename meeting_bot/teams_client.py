"""Join a Teams meeting with a dedicated signed-in browser profile.

The organizer must admit the account when Teams places it in the lobby. The
browser interface can change, so this adapter needs a live tenant check.
"""

from __future__ import annotations

import argparse
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse


BROWSER_JOIN = re.compile(
    r"Continue (?:in|on) this browser|Join (?:in|on) (?:the )?(?:web|browser)|"
    r"Продолжить в этом браузере|Присоединиться (?:к собранию )?(?:из|в) (?:этого )?браузера|"
    r"Браузерде жалғастыру",
    re.I,
)
JOIN_NOW = re.compile(r"Join now|Присоединиться сейчас|Қазір қосылу", re.I)
GUEST_NAME = re.compile(r"(?:Enter|Type|Your) (?:your )?name|Введите (?:ваше |свое |своё )?имя|Атыңыз", re.I)
MUTE_MIC = re.compile(r"Mute (?:microphone|mic)|Отключить микрофон|Микрофонды өшіру", re.I)
CONTINUE_NO_MEDIA = re.compile(r"Continue without (?:audio|sound)(?: and video)?|Продолжить без звука и видео", re.I)
JOIN_DENIED = re.compile(r"you (?:are not|aren't) allowed to join|вы не допущены к собранию", re.I)


def browser_meeting_url(meeting_url: str) -> str:
    """Open direct web join for Teams /meet links to avoid the app-launch dialog."""
    parsed = urlparse(meeting_url)
    if parsed.netloc != "teams.microsoft.com" or not parsed.path.startswith("/meet/"):
        return meeting_url
    meeting_id = parsed.path.removeprefix("/meet/")
    passcode = parse_qs(parsed.query).get("p", [""])[0]
    if not meeting_id or not passcode:
        return meeting_url
    return ("https://teams.microsoft.com/v2/?meetingjoin=true#/meet/"
            f"{quote(meeting_id, safe='')}?p={quote(passcode, safe='')}&anon=true")


def find_visible_control(context, pattern: re.Pattern[str], roles: tuple[str, ...], timeout_seconds: int):
    """Find a visible Teams control even if sign-in opened another tab."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        for page in reversed(context.pages):
            if page.is_closed():
                continue
            for role in roles:
                controls = page.get_by_role(role, name=pattern)
                for index in range(controls.count()):
                    control = controls.nth(index)
                    if control.is_visible() and control.is_enabled():
                        return page, control
        time.sleep(1)
    raise TimeoutError(f"Teams control not found: {pattern.pattern}")


def fill_guest_name(context, name: str) -> bool:
    """Fill the guest display name when Teams requests it."""
    for page in reversed(context.pages):
        if page.is_closed():
            continue
        for locator in (page.get_by_role("textbox", name=GUEST_NAME),
                        page.get_by_placeholder(GUEST_NAME)):
            for index in range(locator.count()):
                field = locator.nth(index)
                if field.is_visible() and field.is_enabled():
                    field.fill(name)
                    return True
    return False


def mute_microphone(context) -> bool:
    """Keep the bot silent on the Teams prejoin screen."""
    for page in reversed(context.pages):
        if page.is_closed():
            continue
        for role in ("switch", "button"):
            controls = page.get_by_role(role, name=MUTE_MIC)
            for index in range(controls.count()):
                control = controls.nth(index)
                if control.is_visible() and control.is_enabled():
                    control.click()
                    return True
    return False


def main() -> None:
    parser = argparse.ArgumentParser(description="Join Teams and capture audio with a dedicated bot account")
    parser.add_argument("--meeting-url", help="Omit to paste the link at the terminal prompt")
    parser.add_argument("--meeting-url-file", type=Path, default=Path(".teams-meeting-url"),
                        help="Private local file with a Teams link, used when --meeting-url is omitted")
    parser.add_argument("--profile", type=Path, default=Path(".teams-bot-profile"))
    parser.add_argument("--device")
    parser.add_argument("--session", default="test_join")
    parser.add_argument("--data", type=Path, default=Path("data"))
    parser.add_argument("--whisper-model", type=Path)
    parser.add_argument("--ollama-model")
    parser.add_argument("--diarization-model", type=Path)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--join-only", action="store_true", help="Join without recording or processing audio")
    parser.add_argument("--browser-channel", default="chrome", help="Installed Playwright browser channel")
    parser.add_argument("--join-timeout-seconds", type=int, default=300)
    parser.add_argument("--bot-name", default="AI Протоколист", help="Guest name if Teams requests one")
    args = parser.parse_args()
    meeting_url = args.meeting_url
    if not meeting_url and args.meeting_url_file.is_file():
        meeting_url = args.meeting_url_file.read_text(encoding="utf-8").strip()
    if not meeting_url:
        meeting_url = input("Paste Teams meeting link: ").strip()
    if not meeting_url.startswith("https://teams.microsoft.com/"):
        parser.error("Use a Teams meeting link from teams.microsoft.com")
    if not args.join_only and (not args.device or not args.whisper_model or not args.ollama_model):
        parser.error("Audio mode requires --device, --whisper-model and --ollama-model")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise SystemExit("Install requirements-client.txt locally") from exc

    capture: subprocess.Popen | None = None
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch_persistent_context(
            user_data_dir=str(args.profile.resolve()), headless=False,
            channel=args.browser_channel, permissions=[], accept_downloads=False,
        )
        try:
            page = browser.pages[0] if browser.pages else browser.new_page()
            web_url = browser_meeting_url(meeting_url)
            page.goto(web_url, wait_until="domcontentloaded")
            if web_url == meeting_url:
                try:
                    page, in_browser = find_visible_control(browser, BROWSER_JOIN, ("button", "link"), 30)
                    in_browser.click()
                except TimeoutError:
                    pass  # Some tenants open the browser join screen immediately.

            print("Waiting for Teams prejoin screen.")
            deadline = time.monotonic() + args.join_timeout_seconds
            while True:
                try:
                    page, no_media = find_visible_control(browser, CONTINUE_NO_MEDIA, ("button",), 1)
                    no_media.click()
                except TimeoutError:
                    pass
                fill_guest_name(browser, args.bot_name)
                mute_microphone(browser)
                try:
                    page, join = find_visible_control(browser, JOIN_NOW, ("button",), 2)
                    break
                except TimeoutError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Teams join button not found. Check the browser for login, lobby, or an error.")
            join.click()
            print("Waiting for the organizer to admit the bot if it is in the lobby.")
            deadline = time.monotonic() + args.join_timeout_seconds
            while True:
                for candidate in reversed(browser.pages):
                    if not candidate.is_closed() and candidate.get_by_text(JOIN_DENIED).count():
                        raise RuntimeError(
                            "Teams rejected the guest before lobby admission. "
                            "The organizer's Teams policy may block anonymous participants; "
                            "use a signed-in bot account or ask the Teams administrator to check the policy.")
                try:
                    page, _ = find_visible_control(browser, re.compile(r"Leave|Покинуть|Шығу", re.I),
                                                   ("button",), 1)
                    break
                except TimeoutError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Bot was not admitted to the meeting before the timeout.")

            if args.join_only:
                print("Bot joined. Join-only mode: no audio is recorded. Press Ctrl-C to leave.")
            else:
                command = [sys.executable, "-m", "meeting_bot.capture", "--device", args.device,
                           "--session", args.session, "--data", str(args.data),
                           "--whisper-model", str(args.whisper_model), "--ollama-model", args.ollama_model]
                if args.diarization_model:
                    command += ["--diarization-model", str(args.diarization_model)]
                if args.live:
                    command += ["--live"]
                flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
                capture = subprocess.Popen(command, creationflags=flags)
                print("Bot joined. Audio capture is running; press Ctrl-C after the meeting ends.")
            while capture is None or capture.poll() is None:
                if page.is_closed():
                    break
                ended = page.get_by_text(re.compile(r"meeting has ended|встреча завершена", re.I))
                if ended.count() and ended.first.is_visible():
                    break
                time.sleep(2)
        except KeyboardInterrupt:
            pass
        finally:
            if capture and capture.poll() is None:
                capture.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT)
                capture.wait(timeout=600)
            browser.close()


if __name__ == "__main__":
    main()
