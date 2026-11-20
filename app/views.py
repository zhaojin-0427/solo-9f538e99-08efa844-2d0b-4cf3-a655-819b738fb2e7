"""HTTP routes: HTML pages for the data clerk plus a small JSON API."""

import hashlib
import secrets

from flask import (
    Blueprint,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from . import db, decoder, merge, service

bp = Blueprint("main", __name__)

STATUS_LABELS = {
    "decoded": "有效",
    "invalid": "无效",
    "pending": "待解",
    "reused": "沿用",
}


@bp.app_template_filter("status_label")
def status_label(status):
    return STATUS_LABELS.get(status, status)


@bp.app_template_filter("reading_fmt")
def reading_fmt(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "—"
    return format(value, ".9g")


def _csv_text_from_request():
    """Pull CSV text out of a file upload, a form field, or a raw body."""
    for _field, storage in request.files.items():
        if storage is not None:
            try:
                return storage.read().decode("utf-8")
            except UnicodeDecodeError:
                raise decoder.ValidationError("CSV 必须是 UTF-8 编码")

    if request.form:
        for key in ("csv", "data", "content"):
            if key in request.form and request.form[key].strip():
                return request.form[key]

    raw = request.get_data(cache=True)
    if raw:
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            raise decoder.ValidationError("CSV 必须是 UTF-8 编码")

    raise decoder.ValidationError("请选择 CSV 文件")


def _run_upload(text):
    """Parse and ingest; return (summary, stored frames, inserted keys).

    Raises decoder.ValidationError or service.BatchRejected.
    """
    rows = decoder.parse_csv(text)
    conn = db.get_db()
    summary = service.ingest(conn, rows)

    # Report the committed state rather than values computed during insert: a
    # concurrent firmware registration may already have decoded these frames.
    with db.read_snapshot(conn):
        stored = service.batch_frames(conn, rows)

    inserted = set(summary["inserted_keys"])
    for status, label in (
        ("decoded", "decoded"),
        ("invalid", "invalid"),
        ("pending", "pending"),
    ):
        summary[label] = sum(
            1
            for f in stored
            if (f["device_id"], f["frame_no"]) in inserted and f["status"] == status
        )
    return summary, stored, inserted


@bp.get("/")
def index():
    conn = db.get_db()
    with db.read_snapshot(conn):
        devices = service.list_devices(conn)
        firmwares = service.list_firmwares(conn)
    return render_template(
        "index.html",
        devices=devices,
        firmwares=firmwares,
        word_orders=decoder.WORD_ORDERS,
        max_batch=decoder.MAX_BATCH,
    )


@bp.post("/upload")
def upload():
    try:
        summary, stored, inserted = _run_upload(_csv_text_from_request())
    except decoder.ValidationError as exc:
        flash(str(exc), "error")
        return redirect(url_for("main.index"))
    except service.BatchRejected as exc:
        flash(str(exc), "error")
        return render_template(
            "upload_rejected.html", message=str(exc), conflicts=exc.conflicts
        )

    flash(
        "上传完成：共 %d 帧，新增 %d（有效 %d，无效 %d，待解 %d），沿用 %d。"
        % (
            summary["received"],
            summary["inserted"],
            summary["decoded"],
            summary["invalid"],
            summary["pending"],
            summary["reused"],
        ),
        "success",
    )
    return render_template(
        "upload_result.html",
        summary=summary,
        frames=stored,
        inserted=inserted,
    )


@bp.post("/firmware")
def register_firmware():
    fw_version = (request.form.get("fw_version") or "").strip()
    word_order = (request.form.get("word_order") or "").strip()
    if not fw_version:
        flash("固件号不能为空", "error")
        return redirect(url_for("main.index"))

    try:
        result = service.register_firmware(db.get_db(), fw_version, word_order)
    except service.ConflictError as exc:
        flash(str(exc), "error")
        return redirect(url_for("main.index"))

    if result["already_registered"]:
        if result["backfilled"]:
            flash(
                "固件 %s 已登记为「%s」，本次补解遗留待解帧 %d 帧。"
                % (
                    fw_version,
                    decoder.WORD_ORDERS[result["word_order"]],
                    result["backfilled"],
                ),
                "success",
            )
        else:
            flash("固件 %s 已登记，字序未变。" % fw_version, "info")
    else:
        flash(
            "固件 %s 已登记为「%s」，补解 %d 帧（有效 %d，无效 %d）。"
            % (
                fw_version,
                decoder.WORD_ORDERS[result["word_order"]],
                result["backfilled"],
                result["decoded"],
                result["invalid"],
            ),
            "success",
        )
    return redirect(url_for("main.index"))


# --- 合库：采集台备份导入 -------------------------------------------------


def _source_summary(staged):
    """One row per source firmware used by the staged frames."""
    counts = {}
    for f in staged["frames"]:
        counts[f["fw_version"]] = counts.get(f["fw_version"], 0) + 1
    return [
        {
            "fw_version": fw,
            "word_order": staged["firmwares"].get(fw),
            "frame_count": counts[fw],
        }
        for fw in sorted(counts)
    ]


def _render_merge_preview(token, staged, backup_hash):
    conn = db.get_db()
    with db.read_snapshot(conn):
        firmwares = service.list_firmwares(conn)
    return render_template(
        "merge_preview.html",
        token=token,
        backup_hash=backup_hash,
        sources=_source_summary(staged),
        frames=staged["frames"],
        firmwares=firmwares,
        word_orders=decoder.WORD_ORDERS,
    )


@bp.post("/merge")
def merge_upload():
    storage = request.files.get("backup")
    if storage is None:
        flash("请选择采集台的 SQLite 备份文件", "error")
        return redirect(url_for("main.index"))
    data = storage.read()
    try:
        staged = merge.parse_backup(data)
    except merge.BackupError as exc:
        flash(str(exc), "error")
        return redirect(url_for("main.index"))

    token = secrets.token_urlsafe(16)
    backup_hash = hashlib.sha256(data).hexdigest()
    service.stage_merge(db.get_db(), token, backup_hash, staged)
    return _render_merge_preview(token, staged, backup_hash)


@bp.post("/merge/apply")
def merge_apply():
    token = (request.form.get("token") or "").strip()
    staged = service.load_staged_merge(db.get_db(), token) if token else None
    if staged is None:
        flash("合库会话不存在或已失效，请重新上传备份。", "error")
        return redirect(url_for("main.index"))

    sources = sorted({f["fw_version"] for f in staged["frames"]})
    form_sources = request.form.getlist("source")
    form_targets = request.form.getlist("target")
    error = None
    mapping = {}
    if sorted(form_sources) != sources or len(form_targets) != len(form_sources):
        error = "提交的映射与备份中的源固件号不一致，请重新上传备份。"
    else:
        for src, tgt in zip(form_sources, form_targets):
            tgt = tgt.strip()
            if not tgt:
                error = "每个源固件号都必须指定主库固件号。"
                break
            if len(tgt) > 128:
                error = "目标固件号过长。"
                break
            mapping[src] = tgt
        else:
            if len(set(mapping.values())) != len(mapping):
                error = "不同的源固件号不能映射到同一个目标固件号。"
    if error:
        flash(error, "error")
        return _render_merge_preview(token, staged, staged["backup_hash"])

    try:
        receipt, replayed = service.apply_merge(db.get_db(), staged, mapping)
    except service.MergeRejected as exc:
        return render_template(
            "merge_rejected.html",
            message=str(exc),
            problems=exc.problems,
            conflicts=exc.conflicts,
        )

    if replayed:
        flash("该备份与映射此前已完成合库，以下为原回执，主库未再次变更。", "info")
    else:
        flash(
            "合库完成：映射 %d 个源固件号，新增 %d 帧，沿用 %d 帧。"
            % (len(receipt["mapping"]), receipt["inserted"], receipt["reused"]),
            "success",
        )
    return render_template(
        "merge_receipt.html",
        receipt=receipt,
        replayed=replayed,
        word_orders=decoder.WORD_ORDERS,
    )


@bp.get("/devices/<path:device_id>")
def device(device_id):
    conn = db.get_db()
    with db.read_snapshot(conn):
        frames = service.device_frames(conn, device_id)
    if not frames:
        abort(404)
    return render_template(
        "device.html",
        device_id=device_id,
        frames=frames,
        word_orders=decoder.WORD_ORDERS,
    )


# --- JSON API -------------------------------------------------------------


@bp.get("/healthz")
def healthz():
    conn = db.get_db()
    conn.execute("SELECT 1 FROM firmware LIMIT 1").fetchall()
    return jsonify({"status": "ok"})


@bp.get("/api/devices")
def api_devices():
    conn = db.get_db()
    with db.read_snapshot(conn):
        return jsonify({"devices": service.list_devices(conn)})


@bp.get("/api/devices/<path:device_id>")
def api_device(device_id):
    conn = db.get_db()
    with db.read_snapshot(conn):
        frames = service.device_frames(conn, device_id)
    if not frames:
        abort(404)
    return jsonify({"device_id": device_id, "frames": frames})


@bp.get("/api/firmwares")
def api_firmwares():
    conn = db.get_db()
    with db.read_snapshot(conn):
        return jsonify({"firmwares": service.list_firmwares(conn)})


@bp.post("/api/firmware")
def api_register_firmware():
    payload = request.get_json(silent=True) or request.form
    fw_version = (payload.get("fw_version") or "").strip()
    word_order = (payload.get("word_order") or "").strip()
    if not fw_version:
        return jsonify({"error": "固件号不能为空"}), 400
    try:
        result = service.register_firmware(db.get_db(), fw_version, word_order)
    except service.ConflictError as exc:
        return jsonify({"error": str(exc)}), 409
    status = 200 if result["already_registered"] else 201
    return jsonify(result), status


@bp.post("/api/upload")
def api_upload():
    try:
        summary, stored, inserted = _run_upload(_csv_text_from_request())
    except decoder.ValidationError as exc:
        return jsonify({"error": str(exc)}), 400
    except service.BatchRejected as exc:
        return (
            jsonify(
                {
                    "error": str(exc),
                    "rejected": True,
                    "conflicts": exc.conflicts,
                }
            ),
            409,
        )
    return jsonify(
        {
            "summary": {k: v for k, v in summary.items() if k != "inserted_keys"},
            "inserted": [
                {"device_id": d, "frame_no": n} for d, n in sorted(inserted)
            ],
            "frames": stored,
        }
    )
