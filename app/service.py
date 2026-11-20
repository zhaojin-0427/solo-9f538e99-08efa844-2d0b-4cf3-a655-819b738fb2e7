"""Transactional business logic for uploads and firmware registration."""

import hashlib
import json
from contextlib import contextmanager

from . import decoder


class ConflictError(Exception):
    """Request conflicts with stored, immutable state."""


class BatchRejected(Exception):
    """The whole upload is rejected (e.g. content mismatch on retransmit)."""

    def __init__(self, message, conflicts=None):
        super().__init__(message)
        self.conflicts = conflicts or []


class MergeRejected(Exception):
    """The whole merge is rejected; the main database stays unchanged."""

    def __init__(self, message, problems=None, conflicts=None):
        super().__init__(message)
        self.problems = problems or []
        self.conflicts = conflicts or []


@contextmanager
def write_transaction(db):
    """Single serialised writer; everything commits or nothing does."""
    db.execute("BEGIN IMMEDIATE")
    try:
        yield db
    except BaseException:
        db.execute("ROLLBACK")
        raise
    else:
        db.execute("COMMIT")


def _firmware_orders(db, versions):
    versions = list(versions)
    if not versions:
        return {}
    placeholders = ",".join("?" for _ in versions)
    rows = db.execute(
        "SELECT fw_version, word_order FROM firmware WHERE fw_version IN (%s)"
        % placeholders,
        versions,
    ).fetchall()
    return {r["fw_version"]: r["word_order"] for r in rows}


def ingest(db, rows):
    """Apply a validated batch atomically.

    Rules:
      * unknown firmware -> stored pending, no guessed reading;
      * registered firmware -> decoded exactly once, at insert time;
      * retransmission (same device+frame+content) -> reuse stored result;
      * same device+frame with different content -> reject the whole batch.
    """
    summary = {
        "received": len(rows),
        "inserted": 0,
        "reused": 0,
        "decoded": 0,
        "invalid": 0,
        "pending": 0,
        "inserted_keys": [],
    }

    with write_transaction(db):
        orders = _firmware_orders(db, {r["fw_version"] for r in rows})

        # Reject the whole batch before touching anything.
        existing = {}
        conflicts = []
        for row in rows:
            key = (row["device_id"], row["frame_no"])
            if key in existing:
                continue
            prev = db.execute(
                "SELECT raw_word, fw_version FROM frames WHERE device_id = ? AND frame_no = ?",
                key,
            ).fetchone()
            existing[key] = prev
            if prev is not None and (
                prev["raw_word"] != row["raw_word"]
                or prev["fw_version"] != row["fw_version"]
            ):
                conflicts.append(
                    {
                        "device_id": row["device_id"],
                        "frame_no": row["frame_no"],
                        "stored_raw_word": "%s (%s)"
                        % (prev["raw_word"], prev["fw_version"]),
                        "uploaded_raw_word": "%s (%s)"
                        % (row["raw_word"], row["fw_version"]),
                    }
                )
        if conflicts:
            raise BatchRejected(
                "上传被拒绝：%d 帧与该设备已存帧号内容不一致" % len(conflicts),
                conflicts,
            )

        inserted_in_batch = set()
        for row in rows:
            key = (row["device_id"], row["frame_no"])
            prev = existing.get(key)
            if prev is not None or key in inserted_in_batch:
                summary["reused"] += 1
                continue

            fw = row["fw_version"]
            order = orders.get(fw)
            if order is None:
                status, reading, bits = "pending", None, None
                summary["pending"] += 1
            else:
                status, reading, bits = decoder.decode(row["raw_word"], order)
                if status == "decoded":
                    summary["decoded"] += 1
                else:
                    summary["invalid"] += 1

            db.execute(
                """INSERT INTO frames
                   (device_id, frame_no, fw_version, raw_word,
                    status, reading, word_order, bit_pattern, decoded_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?,
                           CASE WHEN ? IS NULL THEN NULL
                                ELSE strftime('%Y-%m-%dT%H:%M:%fZ', 'now') END)""",
                (
                    row["device_id"],
                    row["frame_no"],
                    fw,
                    row["raw_word"],
                    status,
                    reading,
                    order,
                    bits,
                    bits,
                ),
            )
            inserted_in_batch.add(key)
            summary["inserted"] += 1
            summary["inserted_keys"].append(key)

    return summary


def register_firmware(db, fw_version, word_order):
    """Register an immutable word order and backfill its pending frames.

    Registration and backfill happen in one transaction, so no reader can
    observe the firmware as configured while its frames are still pending.
    """
    if word_order not in decoder.WORD_ORDERS:
        raise ConflictError("字序只能是 high_first 或 low_first")

    with write_transaction(db):
        row = db.execute(
            "SELECT word_order FROM firmware WHERE fw_version = ?", (fw_version,)
        ).fetchone()
        already = row is not None
        if already:
            if row["word_order"] != word_order:
                raise ConflictError(
                    "固件 %s 已登记为「%s」，字序不可更改（请求「%s」）"
                    % (
                        fw_version,
                        decoder.WORD_ORDERS[row["word_order"]],
                        decoder.WORD_ORDERS[word_order],
                    )
                )
        else:
            db.execute(
                "INSERT INTO firmware (fw_version, word_order) VALUES (?, ?)",
                (fw_version, word_order),
            )

        # Even for an already-registered firmware, sweep any row still marked
        # pending so the invariant "registered implies nothing pending" holds
        # no matter how the data got there.  The status guard makes this safe
        # to repeat.
        pending = db.execute(
            "SELECT id, raw_word FROM frames WHERE fw_version = ? AND status = 'pending'",
            (fw_version,),
        ).fetchall()

        decoded = invalid = 0
        for frame in pending:
            status, reading, bits = decoder.decode(frame["raw_word"], word_order)
            count = db.execute(
                """UPDATE frames
                   SET status = ?, reading = ?, word_order = ?, bit_pattern = ?,
                       decoded_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                   WHERE id = ? AND status = 'pending'""",
                (status, reading, word_order, bits, frame["id"]),
            ).rowcount
            # The row was selected inside this write transaction and the
            # UPDATE repeats the pending guard, so a frame cannot be decoded
            # twice.
            if count == 1:
                if status == "decoded":
                    decoded += 1
                else:
                    invalid += 1

    return {
        "fw_version": fw_version,
        "word_order": word_order,
        "already_registered": already,
        "backfilled": decoded + invalid,
        "decoded": decoded,
        "invalid": invalid,
    }


def stage_merge(conn, token, backup_hash, staged):
    """Park a validated backup under a token until the mapping is submitted."""
    payload = json.dumps(
        {"firmwares": staged["firmwares"], "frames": staged["frames"]},
        ensure_ascii=False,
    )
    with write_transaction(conn):
        conn.execute(
            "INSERT INTO merge_staging (token, backup_hash, payload) VALUES (?, ?, ?)",
            (token, backup_hash, payload),
        )


def load_staged_merge(conn, token):
    row = conn.execute(
        "SELECT backup_hash, payload FROM merge_staging WHERE token = ?", (token,)
    ).fetchone()
    if row is None:
        return None
    staged = json.loads(row["payload"])
    staged["backup_hash"] = row["backup_hash"]
    return staged


def _receipt_key(backup_hash, mapping):
    """Identity of a merge request: this backup under exactly this mapping."""
    canonical = json.dumps(mapping, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(
        ("%s\n%s" % (backup_hash, canonical)).encode("utf-8")
    ).hexdigest()


def apply_merge(conn, staged, mapping):
    """Merge a staged backup under the given source->target firmware mapping.

    Everything — new firmware registrations, new frames and the receipt —
    commits in one transaction; any rejection leaves the main DB unchanged.
    All checks against main-DB registrations and frames happen inside the
    serialised write transaction, so interleaved uploads/registrations are
    observed in commit order.  Re-submitting the same backup with the same
    mapping returns the stored receipt without touching the main DB.
    """
    key = _receipt_key(staged["backup_hash"], mapping)
    frames = staged["frames"]
    src_orders = staged["firmwares"]

    with write_transaction(conn):
        row = conn.execute(
            "SELECT payload, created_at FROM merge_receipts WHERE receipt_key = ?",
            (key,),
        ).fetchone()
        replayed = row is not None

        if not replayed:
            orders = _firmware_orders(conn, set(mapping.values()))

            # Firmware mapping plan, checked against current registrations.
            problems = []
            plan = {}  # target firmware -> (action, word_order)
            for src in sorted(mapping):
                tgt = mapping[src]
                src_order = src_orders.get(src)
                tgt_order = orders.get(tgt)
                if src_order is not None:
                    if tgt_order is not None:
                        if tgt_order != src_order:
                            problems.append(
                                "源固件 %s 字序为「%s」，目标固件 %s 已登记为「%s」，"
                                "字序不同不能复用"
                                % (
                                    src,
                                    decoder.WORD_ORDERS[src_order],
                                    tgt,
                                    decoder.WORD_ORDERS[tgt_order],
                                )
                            )
                        else:
                            plan[tgt] = ("reuse", tgt_order)
                    else:
                        count = conn.execute(
                            "SELECT COUNT(*) FROM frames WHERE fw_version = ?",
                            (tgt,),
                        ).fetchone()[0]
                        if count:
                            problems.append(
                                "目标固件号 %s 已有 %d 帧且尚未登记，"
                                "不能在其下新增登记（源固件 %s）" % (tgt, count, src)
                            )
                        else:
                            plan[tgt] = ("register", src_order)
                else:
                    if tgt_order is not None:
                        problems.append(
                            "源固件 %s 未登记字序，目标固件 %s 已登记，不能合入"
                            % (src, tgt)
                        )
                    else:
                        plan[tgt] = ("pending", None)

            # Retransmission check: same device+frame is judged by the
            # mapped firmware version and the raw word.
            existing = {}
            conflicts = []
            for f in frames:
                fkey = (f["device_id"], f["frame_no"])
                prev = conn.execute(
                    "SELECT raw_word, fw_version FROM frames"
                    " WHERE device_id = ? AND frame_no = ?",
                    fkey,
                ).fetchone()
                existing[fkey] = prev
                if prev is not None:
                    mapped_fw = mapping[f["fw_version"]]
                    if (
                        prev["raw_word"] != f["raw_word"]
                        or prev["fw_version"] != mapped_fw
                    ):
                        conflicts.append(
                            {
                                "device_id": f["device_id"],
                                "frame_no": f["frame_no"],
                                "stored_raw_word": "%s (%s)"
                                % (prev["raw_word"], prev["fw_version"]),
                                "uploaded_raw_word": "%s (%s)"
                                % (f["raw_word"], mapped_fw),
                            }
                        )
            if problems or conflicts:
                raise MergeRejected(
                    "合库被拒绝：%d 个映射问题，%d 帧与主库已存内容不一致"
                    % (len(problems), len(conflicts)),
                    problems,
                    conflicts,
                )

            for tgt in sorted(plan):
                action, order = plan[tgt]
                if action == "register":
                    conn.execute(
                        "INSERT INTO firmware (fw_version, word_order) VALUES (?, ?)",
                        (tgt, order),
                    )

            inserted = reused = 0
            for f in frames:
                fkey = (f["device_id"], f["frame_no"])
                if existing[fkey] is not None:
                    reused += 1
                    continue
                tgt = mapping[f["fw_version"]]
                order = plan[tgt][1]
                if order is None:
                    status, reading, bits = "pending", None, None
                else:
                    status, reading, bits = decoder.decode(f["raw_word"], order)
                conn.execute(
                    """INSERT INTO frames
                       (device_id, frame_no, fw_version, raw_word,
                        status, reading, word_order, bit_pattern,
                        received_at, decoded_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?,
                               CASE WHEN ? IS NULL THEN NULL
                                    ELSE strftime('%Y-%m-%dT%H:%M:%fZ', 'now') END)""",
                    (
                        f["device_id"],
                        f["frame_no"],
                        tgt,
                        f["raw_word"],
                        status,
                        reading,
                        order,
                        bits,
                        f["received_at"],
                        bits,
                    ),
                )
                inserted += 1

            receipt = {
                "receipt_key": key,
                "backup_hash": staged["backup_hash"],
                "mapping": [
                    {
                        "source": src,
                        "target": mapping[src],
                        "word_order": src_orders.get(src),
                        "action": plan[mapping[src]][0],
                    }
                    for src in sorted(mapping)
                ],
                "total": len(frames),
                "inserted": inserted,
                "reused": reused,
            }
            conn.execute(
                "INSERT INTO merge_receipts (receipt_key, backup_hash, payload)"
                " VALUES (?, ?, ?)",
                (key, staged["backup_hash"], json.dumps(receipt, ensure_ascii=False)),
            )
            row = conn.execute(
                "SELECT payload, created_at FROM merge_receipts WHERE receipt_key = ?",
                (key,),
            ).fetchone()

        receipt = json.loads(row["payload"])
        receipt["merged_at"] = row["created_at"]
    return receipt, replayed


def list_devices(db):
    rows = db.execute(
        """SELECT device_id,
                  COUNT(*) AS frame_count,
                  SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending_count,
                  MAX(received_at) AS last_seen
           FROM frames
           GROUP BY device_id
           ORDER BY device_id"""
    ).fetchall()
    return [dict(r) for r in rows]


def list_firmwares(db):
    rows = db.execute(
        """SELECT f.fw_version, f.word_order, f.registered_at,
                  COUNT(fr.id) AS frame_count,
                  SUM(CASE WHEN fr.status = 'pending' THEN 1 ELSE 0 END) AS pending_count
           FROM firmware f
           LEFT JOIN frames fr ON fr.fw_version = f.fw_version
           GROUP BY f.fw_version
           ORDER BY f.fw_version"""
    ).fetchall()
    return [dict(r) for r in rows]


def device_frames(db, device_id):
    rows = db.execute(
        """SELECT frame_no, fw_version, raw_word, status, reading, word_order,
                  bit_pattern, received_at, decoded_at
           FROM frames WHERE device_id = ? ORDER BY frame_no""",
        (device_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def batch_frames(db, rows):
    """Current stored state of the frames in a submitted batch.

    Reading this inside one snapshot after the write commits means the upload
    response reflects a firmware registration that ran concurrently and had
    already committed, never a stale "pending".
    """
    result = []
    seen = set()
    for row in rows:
        key = (row["device_id"], row["frame_no"])
        if key in seen:
            continue
        seen.add(key)
        stored = db.execute(
            """SELECT device_id, frame_no, fw_version, raw_word, status, reading,
                      bit_pattern
               FROM frames WHERE device_id = ? AND frame_no = ?""",
            key,
        ).fetchone()
        if stored is not None:
            result.append(dict(stored))
    return result
