from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path


import sys

if getattr(sys, "frozen", False):
    ROOT_DIR = Path(sys.executable).resolve().parent
else:
    ROOT_DIR = Path(__file__).resolve().parents[1]

DATA_DIR = ROOT_DIR / "data"
PROFILE_FILE = DATA_DIR / "device_profiles.json"
SETTINGS_FILE = DATA_DIR / "app_settings.json"


@dataclass(slots=True)
class DeviceProfile:
    fb_uid: str = ""
    fb_pass: str = ""
    fb_2fa: str = ""
    cookie: str = ""
    token: str = ""
    mailr: str = ""
    pass_mail: str = ""
    mail_recover: str = ""
    session_token: str = ""
    fb_name: str = ""
    proxy: str = ""
    note: str = ""
    state: str = ""
    last_check_status: str = ""


@dataclass(slots=True)
class AppSettings:
    scrcpy_path: str = ""


class ProfileStore:
    def __init__(self, path: Path = PROFILE_FILE) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._profiles = self._load()

    def _load(self) -> dict[str, DeviceProfile]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}

        profiles: dict[str, DeviceProfile] = {}
        for serial, payload in raw.items():
            profiles[serial] = DeviceProfile(
                fb_uid=str(payload.get("fb_uid", "")),
                fb_pass=str(payload.get("fb_pass", "")),
                fb_2fa=str(payload.get("fb_2fa", "")),
                cookie=str(payload.get("cookie", "")),
                token=str(payload.get("token", "")),
                mailr=str(payload.get("mailr", "")),
                pass_mail=str(payload.get("pass_mail", "")),
                mail_recover=str(payload.get("mail_recover", "")),
                session_token=str(payload.get("session_token", "")),
                fb_name=str(payload.get("fb_name", "")),
                proxy=str(payload.get("proxy", "")),
                note=str(payload.get("note", "")),
                state=str(payload.get("state", "")),
                last_check_status=str(payload.get("last_check_status", "")),
            )
        return profiles

    def save(self) -> None:
        payload = {serial: asdict(profile) for serial, profile in self._profiles.items()}
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def get(self, serial: str) -> DeviceProfile:
        return self._profiles.get(serial, DeviceProfile())

    def update(
        self,
        serial: str,
        fb_uid: str | None = None,
        fb_pass: str | None = None,
        fb_2fa: str | None = None,
        cookie: str | None = None,
        token: str | None = None,
        mailr: str | None = None,
        pass_mail: str | None = None,
        mail_recover: str | None = None,
        session_token: str | None = None,
        fb_name: str | None = None,
        proxy: str | None = None,
        note: str | None = None,
        state: str | None = None,
        last_check_status: str | None = None,
        **kwargs,
    ) -> None:
        profile = self._profiles.get(serial, DeviceProfile())
        if fb_uid is not None:
            profile.fb_uid = fb_uid.strip()
        if fb_pass is not None:
            profile.fb_pass = fb_pass.strip()
        if fb_2fa is not None:
            profile.fb_2fa = fb_2fa.strip()
        if cookie is not None:
            profile.cookie = cookie.strip()
        if token is not None:
            profile.token = token.strip()
        if mailr is not None:
            profile.mailr = mailr.strip()
        if pass_mail is not None:
            profile.pass_mail = pass_mail.strip()
        if mail_recover is not None:
            profile.mail_recover = mail_recover.strip()
        if session_token is not None:
            profile.session_token = session_token.strip()
        if fb_name is not None:
            profile.fb_name = fb_name.strip()
        if proxy is not None:
            profile.proxy = proxy.strip()
        if note is not None:
            profile.note = note.strip()
        if state is not None:
            profile.state = state.strip()
        if last_check_status is not None:
            profile.last_check_status = last_check_status.strip()

        for k, v in kwargs.items():
            if hasattr(profile, k) and v is not None:
                setattr(profile, k, str(v).strip())

        self._profiles[serial] = profile
        self.save()


class SettingsStore:
    def __init__(self, path: Path = SETTINGS_FILE) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._settings = self._load()

    def _load(self) -> AppSettings:
        if not self.path.exists():
            return AppSettings()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return AppSettings()

        return AppSettings(scrcpy_path=str(raw.get("scrcpy_path", "")))

    def save(self) -> None:
        self.path.write_text(
            json.dumps(asdict(self._settings), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def get(self) -> AppSettings:
        return self._settings

    def update_scrcpy_path(self, scrcpy_path: str) -> None:
        self._settings.scrcpy_path = scrcpy_path.strip()
        self.save()
