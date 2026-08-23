from dataclasses import dataclass


@dataclass(slots=True)
class DeviceInfo:
    serial: str
    state: str = ""
    model: str = ""
    android_version: str = ""
    fb_uid: str = ""
    fb_name: str = ""
    proxy: str = ""
    note: str = ""
    last_check_status: str = ""

    @property
    def display_name(self) -> str:
        return self.model or self.serial
