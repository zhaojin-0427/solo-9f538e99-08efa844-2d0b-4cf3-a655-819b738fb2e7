import io
import os
import re
import sqlite3
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app, db, service  # noqa: E402


def hex_word(value, word_order="high_first"):
    """Encode a float as an 8-hex-digit raw field in the given wire order."""
    pattern = struct.unpack(">I", struct.pack(">f", value))[0]
    high = "%04X" % (pattern >> 16)
    low = "%04X" % (pattern & 0xFFFF)
    return high + low if word_order == "high_first" else low + high


def make_backup(path, firmwares=(), frames=()):
    """Create a collection-station backup with this application's schema.

    firmwares: iterable of (fw_version, word_order)
    frames:    iterable of (device_id, frame_no, fw_version, raw_word, received_at)
    """
    conn = sqlite3.connect(path)
    try:
        conn.executescript(db.SCHEMA)
        for fw, order in firmwares:
            conn.execute(
                "INSERT INTO firmware (fw_version, word_order) VALUES (?, ?)",
                (fw, order),
            )
        for device_id, frame_no, fw, raw, received_at in frames:
            conn.execute(
                """INSERT INTO frames
                   (device_id, frame_no, fw_version, raw_word, status, received_at)
                   VALUES (?, ?, ?, ?, 'pending', ?)""",
                (device_id, frame_no, fw, raw, received_at),
            )
        conn.commit()
    finally:
        conn.close()
    return path


TS = "2026-01-01T00:00:%02d.000Z"


class MergeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "main.sqlite3")
        self.app = create_app(
            {
                "TESTING": True,
                "DATA_DIR": self.tmp.name,
                "DATABASE": self.db_path,
            }
        )
        self.client = self.app.test_client()
        self.backup_path = os.path.join(self.tmp.name, "backup.sqlite3")

    def tearDown(self):
        self.tmp.cleanup()

    # --- helpers -----------------------------------------------------------

    def upload_backup(self, follow_redirects=False):
        with open(self.backup_path, "rb") as fh:
            data = fh.read()
        return self.client.post(
            "/merge",
            data={"backup": (io.BytesIO(data), "backup.sqlite3")},
            content_type="multipart/form-data",
            follow_redirects=follow_redirects,
        )

    @staticmethod
    def token_from(html):
        match = re.search(r'name="token" value="([^"]+)"', html)
        assert match, "preview page must carry a staging token"
        return match.group(1)

    def apply(self, mapping, token=None):
        """mapping: list of (source, target) pairs, in form order."""
        if token is None:
            html = self.upload_backup().get_data(as_text=True)
            token = self.token_from(html)
        data = {
            "token": token,
            "source": [src for src, _ in mapping],
            "target": [tgt for _, tgt in mapping],
        }
        return self.client.post("/merge/apply", data=data)

    def frames(self, device):
        with self.app.app_context():
            return service.device_frames(db.get_db(), device)

    def firmware(self, fw_version):
        with self.app.app_context():
            row = db.get_db().execute(
                "SELECT fw_version, word_order, registered_at"
                " FROM firmware WHERE fw_version = ?",
                (fw_version,),
            ).fetchone()
            return dict(row) if row is not None else None

    def frame_count(self):
        with self.app.app_context():
            return db.get_db().execute("SELECT COUNT(*) FROM frames").fetchone()[0]

    # --- preview -----------------------------------------------------------

    def test_preview_lists_source_firmwares_and_orders(self):
        make_backup(
            self.backup_path,
            firmwares=[("FW-LOW", "low_first"), ("FW-HI", "high_first")],
            frames=[
                ("D1", 1, "FW-LOW", hex_word(1.0, "low_first"), TS % 0),
                ("D1", 2, "FW-HI", hex_word(2.0), TS % 1),
                ("D1", 3, "FW-UNKNOWN", "3F800000", TS % 2),
            ],
        )
        html = self.upload_backup().get_data(as_text=True)
        self.assertIn("FW-LOW", html)
        self.assertIn("FW-HI", html)
        self.assertIn("FW-UNKNOWN", html)
        self.assertIn("低字在前", html)
        self.assertIn("高字在前", html)
        self.assertIn("未登记", html)
        self.assertIn('name="target"', html)
        # nothing applied yet
        self.assertEqual(self.frame_count(), 0)
        self.assertIsNone(self.firmware("FW-LOW"))

    # --- happy paths -------------------------------------------------------

    def test_merge_registers_new_target_and_decodes(self):
        make_backup(
            self.backup_path,
            firmwares=[("FW-S", "low_first")],
            frames=[
                ("D1", 1, "FW-S", hex_word(1.5, "low_first"), TS % 0),
                ("D1", 2, "FW-S", "00007FC0", TS % 1),  # NaN under low_first
            ],
        )
        html = self.apply([("FW-S", "FW-M")]).get_data(as_text=True)
        self.assertIn("合库回执", html)
        self.assertIn("FW-S", html)
        self.assertIn("FW-M", html)
        self.assertIn("新增登记", html)
        self.assertIn("新增 2", html)
        self.assertIn("沿用 0", html)

        fw = self.firmware("FW-M")
        self.assertEqual(fw["word_order"], "low_first")
        frames = {f["frame_no"]: f for f in self.frames("D1")}
        self.assertEqual(frames[1]["fw_version"], "FW-M")
        self.assertEqual(frames[1]["status"], "decoded")
        self.assertEqual(frames[1]["reading"], 1.5)
        # original receive time preserved
        self.assertEqual(frames[1]["received_at"], TS % 0)
        self.assertEqual(frames[2]["received_at"], TS % 1)
        self.assertEqual(frames[2]["status"], "invalid")
        self.assertIsNone(frames[2]["reading"])

        # device page shows the imported firmware version and reading
        page = self.client.get("/devices/D1").get_data(as_text=True)
        self.assertIn("FW-M", page)
        self.assertIn("1.5", page)

    def test_merge_reuses_same_order_registration(self):
        self.client.post(
            "/firmware", data={"fw_version": "FW1", "word_order": "high_first"}
        )
        before = self.firmware("FW1")["registered_at"]
        make_backup(
            self.backup_path,
            firmwares=[("FW1", "high_first")],
            frames=[("D1", 1, "FW1", hex_word(2.0), TS % 0)],
        )
        html = self.apply([("FW1", "FW1")]).get_data(as_text=True)
        self.assertIn("复用主库登记", html)
        self.assertNotIn("新增登记", html)
        self.assertEqual(self.firmware("FW1")["registered_at"], before)
        self.assertEqual(self.frames("D1")[0]["reading"], 2.0)

    def test_merge_opposite_order_remapped_to_new_target(self):
        # main DB registered FW1 with the opposite word order
        self.client.post(
            "/firmware", data={"fw_version": "FW1", "word_order": "high_first"}
        )
        make_backup(
            self.backup_path,
            firmwares=[("FW1", "low_first")],
            frames=[("D1", 1, "FW1", hex_word(1.0, "low_first"), TS % 0)],
        )
        html = self.apply([("FW1", "FW1-LOW")]).get_data(as_text=True)
        self.assertIn("合库回执", html)
        self.assertEqual(self.firmware("FW1-LOW")["word_order"], "low_first")
        self.assertEqual(self.firmware("FW1")["word_order"], "high_first")
        frame = self.frames("D1")[0]
        self.assertEqual(frame["fw_version"], "FW1-LOW")
        self.assertEqual(frame["reading"], 1.0)

    def test_merge_unregistered_source_stays_pending_then_backfills(self):
        make_backup(
            self.backup_path,
            frames=[("D1", 1, "FW-U", hex_word(3.0, "low_first"), TS % 0)],
        )
        html = self.apply([("FW-U", "FW-U")]).get_data(as_text=True)
        self.assertIn("留待补解", html)
        self.assertIsNone(self.firmware("FW-U"))
        frame = self.frames("D1")[0]
        self.assertEqual(frame["status"], "pending")
        self.assertIsNone(frame["reading"])

        # later registration backfills the imported pending frames
        self.client.post(
            "/firmware", data={"fw_version": "FW-U", "word_order": "low_first"}
        )
        frame = self.frames("D1")[0]
        self.assertEqual(frame["status"], "decoded")
        self.assertEqual(frame["reading"], 3.0)

    def test_merge_retransmit_same_content_is_reused(self):
        self.client.post(
            "/firmware", data={"fw_version": "FW1", "word_order": "high_first"}
        )
        self.client.post(
            "/api/upload", data="D1,1,FW1,3F800000\n", content_type="text/csv"
        )
        make_backup(
            self.backup_path,
            firmwares=[("FW1", "high_first")],
            frames=[
                ("D1", 1, "FW1", "3F800000", TS % 0),  # same content -> reuse
                ("D1", 2, "FW1", "40000000", TS % 1),  # new frame
            ],
        )
        html = self.apply([("FW1", "FW1")]).get_data(as_text=True)
        self.assertIn("新增 1", html)
        self.assertIn("沿用 1", html)
        frames = {f["frame_no"]: f for f in self.frames("D1")}
        self.assertEqual(len(frames), 2)
        # the reused frame keeps its original stored receive time
        self.assertNotEqual(frames[1]["received_at"], TS % 0)
        self.assertEqual(frames[2]["received_at"], TS % 1)

    # --- rejections keep the main DB unchanged ------------------------------

    def test_merge_opposite_order_same_target_rejected(self):
        self.client.post(
            "/firmware", data={"fw_version": "FW1", "word_order": "high_first"}
        )
        make_backup(
            self.backup_path,
            firmwares=[("FW1", "low_first")],
            frames=[("D1", 1, "FW1", hex_word(1.0, "low_first"), TS % 0)],
        )
        html = self.apply([("FW1", "FW1")]).get_data(as_text=True)
        self.assertIn("合库被拒绝", html)
        self.assertIn("字序不同不能复用", html)
        self.assertEqual(self.frames("D1"), [])
        self.assertEqual(self.firmware("FW1")["word_order"], "high_first")

    def test_merge_target_with_frames_cannot_be_registered(self):
        # normal upload leaves pending frames under unregistered FW9
        self.client.post(
            "/api/upload", data="D9,1,FW9,3F800000\n", content_type="text/csv"
        )
        make_backup(
            self.backup_path,
            firmwares=[("FW-S", "high_first")],
            frames=[("D1", 1, "FW-S", "3F800000", TS % 0)],
        )
        html = self.apply([("FW-S", "FW9")]).get_data(as_text=True)
        self.assertIn("合库被拒绝", html)
        self.assertIn("尚未登记", html)
        self.assertIsNone(self.firmware("FW9"))
        self.assertEqual(self.frames("D1"), [])
        self.assertEqual(len(self.frames("D9")), 1)

    def test_merge_unregistered_source_to_registered_target_rejected(self):
        self.client.post(
            "/firmware", data={"fw_version": "FW1", "word_order": "high_first"}
        )
        make_backup(
            self.backup_path,
            frames=[("D1", 1, "FW-U", "3F800000", TS % 0)],
        )
        html = self.apply([("FW-U", "FW1")]).get_data(as_text=True)
        self.assertIn("合库被拒绝", html)
        self.assertIn("未登记字序", html)
        self.assertEqual(self.frames("D1"), [])

    def test_merge_duplicate_targets_rejected(self):
        make_backup(
            self.backup_path,
            firmwares=[("A", "high_first"), ("B", "low_first")],
            frames=[
                ("D1", 1, "A", "3F800000", TS % 0),
                ("D1", 2, "B", "3F800000", TS % 1),
            ],
        )
        html = self.apply([("A", "T"), ("B", "T")]).get_data(as_text=True)
        self.assertIn("同一个目标固件号", html)
        self.assertIsNone(self.firmware("T"))
        self.assertEqual(self.frame_count(), 0)

    def test_merge_conflicting_frame_rejects_whole_merge(self):
        self.client.post(
            "/firmware", data={"fw_version": "FW1", "word_order": "high_first"}
        )
        self.client.post(
            "/api/upload", data="D1,1,FW1,3F800000\n", content_type="text/csv"
        )
        make_backup(
            self.backup_path,
            firmwares=[("FW1", "high_first")],
            frames=[
                ("D1", 1, "FW1", "40000000", TS % 0),  # contradicts stored
                ("D1", 2, "FW1", "3F800000", TS % 1),
            ],
        )
        html = self.apply([("FW1", "FW1")]).get_data(as_text=True)
        self.assertIn("合库被拒绝", html)
        self.assertIn("40000000", html)
        frames = self.frames("D1")
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0]["raw_word"], "3F800000")

    def test_merge_conflict_via_mapped_firmware_name(self):
        # stored under FW-M; backup maps a different source name onto FW-M
        self.client.post(
            "/firmware", data={"fw_version": "FW-M", "word_order": "low_first"}
        )
        self.client.post(
            "/api/upload", data="D1,1,FW-M,3F800000\n", content_type="text/csv"
        )
        make_backup(
            self.backup_path,
            firmwares=[("FW-S", "low_first")],
            frames=[("D1", 1, "FW-S", "3F800000", TS % 0)],
        )
        # same raw word but mapped fw equals stored fw -> same content, reused
        html = self.apply([("FW-S", "FW-M")]).get_data(as_text=True)
        self.assertIn("沿用 1", html)
        self.assertEqual(self.frame_count(), 1)

    # --- idempotent replay --------------------------------------------------

    def test_repeated_merge_returns_original_receipt(self):
        make_backup(
            self.backup_path,
            frames=[
                ("D1", 1, "FW-U", hex_word(1.0, "low_first"), TS % 0),
                ("D1", 2, "FW-U", hex_word(2.0, "low_first"), TS % 1),
            ],
        )
        html1 = self.apply([("FW-U", "FW-U")]).get_data(as_text=True)
        self.assertIn("新增 2", html1)
        self.assertIn("沿用 0", html1)

        # later backfill decodes the imported frames
        self.client.post(
            "/firmware", data={"fw_version": "FW-U", "word_order": "low_first"}
        )
        decoded_at = {
            f["frame_no"]: f["decoded_at"] for f in self.frames("D1")
        }
        self.assertTrue(all(decoded_at.values()))

        # resubmitting the same backup and mapping returns the original receipt
        html2 = self.apply([("FW-U", "FW-U")]).get_data(as_text=True)
        self.assertIn("原回执", html2)
        self.assertIn("新增 2", html2)
        self.assertIn("沿用 0", html2)
        self.assertEqual(self.frame_count(), 2)
        self.assertEqual(
            {f["frame_no"]: f["decoded_at"] for f in self.frames("D1")},
            decoded_at,
        )

        # ... also after a restart on the same database
        app2 = create_app(
            {
                "TESTING": True,
                "DATA_DIR": self.tmp.name,
                "DATABASE": self.db_path,
            }
        )
        client2 = app2.test_client()
        with open(self.backup_path, "rb") as fh:
            data = fh.read()
        resp = client2.post(
            "/merge",
            data={"backup": (io.BytesIO(data), "backup.sqlite3")},
            content_type="multipart/form-data",
        )
        token = self.token_from(resp.get_data(as_text=True))
        resp = client2.post(
            "/merge/apply",
            data={"token": token, "source": ["FW-U"], "target": ["FW-U"]},
        )
        html3 = resp.get_data(as_text=True)
        self.assertIn("原回执", html3)
        self.assertIn("新增 2", html3)
        self.assertEqual(self.frame_count(), 2)

    def test_same_backup_different_mapping_is_a_new_merge(self):
        make_backup(
            self.backup_path,
            firmwares=[("FW-S", "high_first")],
            frames=[("D1", 1, "FW-S", "3F800000", TS % 0)],
        )
        html1 = self.apply([("FW-S", "T1")]).get_data(as_text=True)
        self.assertIn("新增 1", html1)
        # a different mapping must not replay the first receipt; the frame
        # now contradicts the stored one (different fw_version) -> rejected
        html2 = self.apply([("FW-S", "T2")]).get_data(as_text=True)
        self.assertIn("合库被拒绝", html2)
        self.assertIsNone(self.firmware("T2"))

    # --- interleaving: checks happen at apply time, in commit order ---------

    def test_interleaved_upload_is_seen_by_apply(self):
        make_backup(
            self.backup_path,
            firmwares=[("FW1", "high_first")],
            frames=[("D1", 5, "FW1", "3F800000", TS % 0)],
        )
        token = self.token_from(self.upload_backup().get_data(as_text=True))
        # a normal upload commits first with different content for the slot
        self.client.post(
            "/api/upload", data="D1,5,FW1,40000000\n", content_type="text/csv"
        )
        html = self.apply([("FW1", "FW1")], token=token).get_data(as_text=True)
        self.assertIn("合库被拒绝", html)
        frames = self.frames("D1")
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0]["raw_word"], "40000000")

    def test_interleaved_registration_is_seen_by_apply(self):
        make_backup(
            self.backup_path,
            firmwares=[("FW1", "low_first")],
            frames=[("D1", 1, "FW1", hex_word(1.0, "low_first"), TS % 0)],
        )
        token = self.token_from(self.upload_backup().get_data(as_text=True))
        # someone registers the same name with the opposite order meanwhile
        self.client.post(
            "/firmware", data={"fw_version": "FW1", "word_order": "high_first"}
        )
        html = self.apply([("FW1", "FW1")], token=token).get_data(as_text=True)
        self.assertIn("合库被拒绝", html)
        self.assertEqual(self.frames("D1"), [])
        self.assertEqual(self.firmware("FW1")["word_order"], "high_first")

    # --- bad backups and bad sessions ---------------------------------------

    def test_backup_over_200_frames_rejected(self):
        make_backup(
            self.backup_path,
            frames=[("D1", i, "FW1", "3F800000", TS % 0) for i in range(201)],
        )
        html = self.upload_backup(follow_redirects=True).get_data(as_text=True)
        self.assertIn("超过 200 帧上限", html)
        self.assertEqual(self.frame_count(), 0)

    def test_non_sqlite_upload_rejected(self):
        resp = self.client.post(
            "/merge",
            data={"backup": (io.BytesIO(b"this is not sqlite"), "x.sqlite3")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertIn("不是有效的 SQLite", resp.get_data(as_text=True))

    def test_schema_mismatch_rejected(self):
        conn = sqlite3.connect(self.backup_path)
        conn.execute("CREATE TABLE something (x INTEGER)")
        conn.commit()
        conn.close()
        html = self.upload_backup(follow_redirects=True).get_data(as_text=True)
        self.assertIn("不是本应用的库结构", html)

    def test_empty_backup_rejected(self):
        make_backup(self.backup_path)
        html = self.upload_backup(follow_redirects=True).get_data(as_text=True)
        self.assertIn("没有任何测量帧", html)

    def test_missing_file_rejected(self):
        resp = self.client.post("/merge", data={}, follow_redirects=True)
        self.assertIn("备份文件", resp.get_data(as_text=True))

    def test_unknown_token_rejected(self):
        resp = self.client.post(
            "/merge/apply",
            data={"token": "no-such-token", "source": ["A"], "target": ["B"]},
            follow_redirects=True,
        )
        self.assertIn("重新上传备份", resp.get_data(as_text=True))

    def test_tampered_source_list_rejected(self):
        make_backup(
            self.backup_path,
            frames=[("D1", 1, "FW-U", "3F800000", TS % 0)],
        )
        token = self.token_from(self.upload_backup().get_data(as_text=True))
        resp = self.client.post(
            "/merge/apply",
            data={"token": token, "source": ["OTHER"], "target": ["T"]},
        )
        self.assertIn("不一致", resp.get_data(as_text=True))
        self.assertEqual(self.frame_count(), 0)


class MergeConcurrencyTests(unittest.TestCase):
    """A merge racing a plain registration must not corrupt either outcome."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = create_app(
            {
                "TESTING": True,
                "DATA_DIR": self.tmp.name,
                "DATABASE": os.path.join(self.tmp.name, "race.sqlite3"),
            }
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_merge_and_registration_race(self):
        import threading

        staged = {
            "firmwares": {"FW-S": "low_first"},
            "frames": [
                {
                    "device_id": "D1",
                    "frame_no": i,
                    "fw_version": "FW-S",
                    "raw_word": hex_word(float(i), "low_first"),
                    "received_at": TS % 0,
                }
                for i in range(1, 20)
            ],
            "backup_hash": "race-backup",
        }
        barrier = threading.Barrier(2)
        results = {}

        def merger():
            try:
                barrier.wait()
                with self.app.app_context():
                    results["merge"] = service.apply_merge(
                        db.get_db(), staged, {"FW-S": "FW9"}
                    )
            except service.MergeRejected as exc:
                results["merge"] = exc

        def registrar():
            try:
                barrier.wait()
                with self.app.app_context():
                    results["register"] = service.register_firmware(
                        db.get_db(), "FW9", "high_first"
                    )
            except service.ConflictError as exc:
                results["register"] = exc

        threads = [threading.Thread(target=merger), threading.Thread(target=registrar)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        merge_ok = not isinstance(results["merge"], service.MergeRejected)
        register_ok = not isinstance(results["register"], service.ConflictError)
        # opposite word orders on one name: exactly one side may win
        self.assertNotEqual(merge_ok, register_ok)

        with self.app.app_context():
            conn = db.get_db()
            order = conn.execute(
                "SELECT word_order FROM firmware WHERE fw_version = 'FW9'"
            ).fetchone()[0]
            count = conn.execute("SELECT COUNT(*) FROM frames").fetchone()[0]
        if merge_ok:
            self.assertEqual(order, "low_first")
            self.assertEqual(count, 19)
        else:
            self.assertEqual(order, "high_first")
            self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
