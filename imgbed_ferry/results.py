"""上传结果的数据结构。

单独成文件是为了让 `service`（生产者）和 `formatting`（消费者）都能引用它，
不用互相 import。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class UploadedItem:
    """一个成功落地图床的文件。"""

    name: str
    url: str
    size: int = 0
    original_size: int = 0
    file_id: str = ""
    digest: str = ""
    kind: str = "file"
    source: str = ""
    archive: str = ""
    note: str = ""
    reused: bool = False

    @property
    def is_image(self) -> bool:
        return self.kind == "image"

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"name": self.name, "url": self.url, "size": self.size}
        if self.file_id:
            data["file_id"] = self.file_id
        if self.archive:
            data["from_archive"] = self.archive
        if self.reused:
            data["reused"] = True
        if self.note:
            data["note"] = self.note
        return data


@dataclass(slots=True)
class FailedItem:
    """一个没能上传成功的文件及原因。"""

    name: str
    reason: str
    hint: str = ""

    def describe(self) -> str:
        return f"{self.reason}（{self.hint}）" if self.hint else self.reason

    def to_dict(self) -> dict[str, Any]:
        data = {"name": self.name, "reason": self.reason}
        if self.hint:
            data["hint"] = self.hint
        return data


@dataclass(slots=True)
class UploadReport:
    """一次上传请求的完整结果。"""

    items: list[UploadedItem] = field(default_factory=list)
    failures: list[FailedItem] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    elapsed: float = 0.0
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return bool(self.items)

    @property
    def uploaded_bytes(self) -> int:
        return sum(item.size for item in self.items if not item.reused)

    @property
    def reused_count(self) -> int:
        return sum(1 for item in self.items if item.reused)

    def merge(self, other: UploadReport) -> None:
        self.items.extend(other.items)
        self.failures.extend(other.failures)
        self.skipped.extend(other.skipped)
        self.notes.extend(other.notes)
        self.truncated = self.truncated or other.truncated
        self.elapsed += other.elapsed

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "success": self.ok,
            "uploaded": len(self.items),
            "files": [item.to_dict() for item in self.items],
        }
        if self.failures:
            data["failed"] = [item.to_dict() for item in self.failures]
        if self.skipped:
            data["skipped"] = [{"name": name, "reason": reason} for name, reason in self.skipped]
        if self.notes:
            data["notes"] = list(self.notes)
        if self.truncated:
            data["truncated"] = True
        return data
