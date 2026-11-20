"""Parsing of collection-station SQLite backups for the merge (合库) flow.

A temporary collection station runs this same application; its standalone
backup is uploaded to the main server, which re-homes the frames under
user-assigned main-DB firmware versions.  The uploaded file is untrusted:
everything is validated here before it is staged, so a malformed backup is
rejected before it can touch the main database.
"""

import os
import sqlite3
import tempfile

from . import decoder

MAX_MERGE_FRAMES = decoder.MAX_BATCH

_FRAME_COLUMNS = {"device_id", "frame_no", "fw_version", "raw_word", "received_at"}
_FIRMWARE_COLUMNS = {"fw_version", "word_order"}


class BackupError(ValueError):
    """The uploaded file is not a usable collection-station backup."""


def _load_rows(path):
    """Read firmware registrations and frames out of a private copy."""
    try:
        # Open our own throwaway copy normally so SQLite may run journal/WAL
        # recovery if the backup was taken mid-checkpoint.
        conn = sqlite3.connect(path)
        try:
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            if not {"frames", "firmware"} <= tables:
                raise BackupError("备份缺少 firmware/frames 表，不是本应用的库结构")
            fw_cols = {r[1] for r in conn.execute("PRAGMA table_info(firmware)")}
            fr_cols = {r[1] for r in conn.execute("PRAGMA table_info(frames)")}
            if not _FIRMWARE_COLUMNS <= fw_cols or not _FRAME_COLUMNS <= fr_cols:
                raise BackupError("备份的表结构与本应用不一致")
            firmwares = conn.execute(
                "SELECT fw_version, word_order FROM firmware"
            ).fetchall()
            frames = conn.execute(
                "SELECT device_id, frame_no, fw_version, raw_word, received_at"
                " FROM frames ORDER BY rowid LIMIT %d" % (MAX_MERGE_FRAMES + 1)
            ).fetchall()
        finally:
            conn.close()
    except BackupError:
        raise
    except sqlite3.DatabaseError:
        raise BackupError("无法读取备份：不是有效的 SQLite 数据库文件")
    return firmwares, frames


def _check_text(value, label, row_no):
    if not isinstance(value, str) or not value:
        raise BackupError("备份第 %d 行：%s缺失" % (row_no, label))
    if len(value) > 128:
        raise BackupError("备份第 %d 行：%s过长" % (row_no, label))
    return value


def parse_backup(data):
    """Validate backup bytes; return {"firmwares": {fw: order}, "frames": [...]}.

    Frames carry their original received_at so the merge can preserve it.
    Raises BackupError for anything that is not a well-formed app backup of
    at most MAX_MERGE_FRAMES frames.
    """
    if not data:
        raise BackupError("备份文件为空")

    fd, path = tempfile.mkstemp(prefix="merge-backup-", suffix=".sqlite3")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        firmware_rows, frame_rows = _load_rows(path)
    finally:
        os.unlink(path)

    firmwares = {}
    for fw, order in firmware_rows:
        if (
            not isinstance(fw, str)
            or not fw
            or len(fw) > 128
            or order not in decoder.WORD_ORDERS
        ):
            raise BackupError("备份中的固件登记无效（固件号 %r）" % (fw,))
        firmwares[fw] = order

    if not frame_rows:
        raise BackupError("备份中没有任何测量帧")
    if len(frame_rows) > MAX_MERGE_FRAMES:
        raise BackupError(
            "备份帧数超过 %d 帧上限，无法合库" % MAX_MERGE_FRAMES
        )

    frames = []
    seen = set()
    for row_no, (device_id, frame_no, fw_version, raw_word, received_at) in enumerate(
        frame_rows, 1
    ):
        _check_text(device_id, "设备号", row_no)
        _check_text(fw_version, "固件号", row_no)
        if (
            not isinstance(frame_no, int)
            or isinstance(frame_no, bool)
            or not 0 <= frame_no < decoder.MAX_FRAME_NO
        ):
            raise BackupError("备份第 %d 行：帧号无效" % row_no)
        if not isinstance(raw_word, str) or not decoder.HEX8_RE.match(raw_word):
            raise BackupError(
                "备份第 %d 行：原始字必须是 8 位十六进制" % row_no
            )
        _check_text(received_at, "接收时间", row_no)
        key = (device_id, frame_no)
        if key in seen:
            raise BackupError(
                "备份中设备 %s 帧号 %d 重复" % (device_id, frame_no)
            )
        seen.add(key)
        frames.append(
            {
                "device_id": device_id,
                "frame_no": frame_no,
                "fw_version": fw_version,
                "raw_word": raw_word.upper(),
                "received_at": received_at,
            }
        )
    return {"firmwares": firmwares, "frames": frames}
