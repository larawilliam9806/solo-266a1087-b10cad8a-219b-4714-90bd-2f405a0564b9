"""路由：网页 + JSON API。写操作统一事务：成绩与核定底表一并入账，失败回滚沿用原账。"""
import json
import uuid

from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for

from .db import get_db, rollback_quietly
from .logic import (
    ApiError,
    apply_transcript_batch,
    import_base_data,
    recompute_student,
    revoke_record,
    student_snapshot,
)

bp = Blueprint("main", __name__)


def _wants_json():
    return request.path.startswith("/api/")


def _read_json_payload():
    """支持 application/json 正文、表单字段或上传文件三种来源。"""
    if request.is_json:
        return request.get_json()
    if "file" in request.files and request.files["file"].filename:
        raw = request.files["file"].read()
    else:
        raw = request.form.get("payload", "")
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise ApiError(f"JSON 解析失败：{e}")


# ---------------------------------------------------------------- 网页

@bp.route("/")
def index():
    return render_template("index.html")


@bp.route("/student")
def student_redirect():
    sid = request.args.get("sid", "").strip()
    if not sid:
        flash("请输入学号", "err")
        return redirect(url_for("main.index"))
    return redirect(url_for("main.student", student_id=sid))


@bp.route("/students/<student_id>")
def student(student_id):
    snap = student_snapshot(get_db(), student_id)
    return render_template("student.html", s=snap)


@bp.route("/upload", methods=["GET", "POST"])
def upload():
    if request.method == "POST":
        db = get_db()
        try:
            payload = _read_json_payload()
            db.execute("BEGIN IMMEDIATE")
            result = apply_transcript_batch(db, payload, batch_id=uuid.uuid4().hex[:12])
            db.execute("COMMIT")
        except ApiError as e:
            rollback_quietly(db)
            flash(e.message, "err")
            return render_template("upload.html"), e.status
        except Exception as e:  # 计算或保存失败：回滚，沿用原账
            rollback_quietly(db)
            flash(f"入账失败，已沿用原账：{e}", "err")
            return render_template("upload.html"), 500
        flash(
            f"批次 {result['batch_id']} 入账：新增 {result['added']} 单、更正 {result['corrected']} 单；"
            f"该生已认定总学分 {result['total_credits']}",
            "ok",
        )
        return redirect(url_for("main.student", student_id=result["student_id"]))
    return render_template("upload.html")


@bp.route("/records/<record_id>/revoke", methods=["POST"])
def revoke(record_id):
    db = get_db()
    student_id = request.form.get("student_id", "")
    try:
        db.execute("BEGIN IMMEDIATE")
        result = revoke_record(db, record_id)
        db.execute("COMMIT")
    except ApiError as e:
        rollback_quietly(db)
        flash(e.message, "err")
        return redirect(request.referrer or url_for("main.index"))
    except Exception as e:
        rollback_quietly(db)
        flash(f"撤销失败，已沿用原账：{e}", "err")
        return redirect(request.referrer or url_for("main.index"))
    flash(f"单号 {record_id} 已撤销并重核：该生已认定总学分 {result['total_credits']}", "ok")
    return redirect(url_for("main.student", student_id=student_id or result["student_id"]))


@bp.route("/admin/base", methods=["GET", "POST"])
def admin_base():
    db = get_db()
    if request.method == "POST":
        try:
            payload = _read_json_payload()
            db.execute("BEGIN IMMEDIATE")
            summary = import_base_data(db, payload)
            # 底表变更后，对已入账学生全部重核，保持核定底表一致（同事务）
            students = [r["student_id"] for r in db.execute("SELECT DISTINCT student_id FROM records")]
            for sid in students:
                recompute_student(db, sid)
            db.execute("COMMIT")
            flash(f"底表已导入：课程 {summary['courses']} 门、前置课 {summary['prerequisites']} 条；"
                  f"并重核 {len(students)} 名学生", "ok")
        except ApiError as e:
            rollback_quietly(db)
            flash(e.message, "err")
        except Exception as e:
            rollback_quietly(db)
            flash(f"导入失败，底表未变更：{e}", "err")
        return redirect(url_for("main.admin_base"))
    courses = db.execute("SELECT * FROM courses ORDER BY course_id").fetchall()
    prereqs = db.execute(
        "SELECT p.course_id, c.name AS course_name, p.prereq_course_id, pc.name AS prereq_name"
        " FROM prerequisites p"
        " LEFT JOIN courses c ON c.course_id = p.course_id"
        " LEFT JOIN courses pc ON pc.course_id = p.prereq_course_id"
        " ORDER BY p.course_id, p.prereq_course_id"
    ).fetchall()
    return render_template("admin_base.html", courses=courses, prereqs=prereqs)


# ---------------------------------------------------------------- JSON API

@bp.route("/api/health")
def api_health():
    return jsonify({"ok": True})


@bp.route("/api/admin/base", methods=["POST"])
def api_import_base():
    db = get_db()
    try:
        payload = _read_json_payload()
        db.execute("BEGIN IMMEDIATE")
        summary = import_base_data(db, payload)
        students = [r["student_id"] for r in db.execute("SELECT DISTINCT student_id FROM records")]
        for sid in students:
            recompute_student(db, sid)
        db.execute("COMMIT")
    except ApiError as e:
        rollback_quietly(db)
        return jsonify({"ok": False, "error": e.message}), e.status
    except Exception as e:
        rollback_quietly(db)
        return jsonify({"ok": False, "error": f"导入失败，底表未变更：{e}"}), 500
    return jsonify({"ok": True, **summary, "students_recomputed": len(students)})


@bp.route("/api/transcripts", methods=["POST"])
def api_upload_transcript():
    db = get_db()
    try:
        payload = _read_json_payload()
        db.execute("BEGIN IMMEDIATE")
        result = apply_transcript_batch(db, payload, batch_id=uuid.uuid4().hex[:12])
        db.execute("COMMIT")
    except ApiError as e:
        rollback_quietly(db)
        return jsonify({"ok": False, "error": e.message}), e.status
    except Exception as e:
        rollback_quietly(db)
        return jsonify({"ok": False, "error": f"入账失败，已沿用原账：{e}"}), 500
    return jsonify({"ok": True, **result})


@bp.route("/api/records/<record_id>/revoke", methods=["POST"])
def api_revoke(record_id):
    db = get_db()
    try:
        db.execute("BEGIN IMMEDIATE")
        result = revoke_record(db, record_id)
        db.execute("COMMIT")
    except ApiError as e:
        rollback_quietly(db)
        return jsonify({"ok": False, "error": e.message}), e.status
    except Exception as e:
        rollback_quietly(db)
        return jsonify({"ok": False, "error": f"撤销失败，已沿用原账：{e}"}), 500
    return jsonify({"ok": True, **result})


@bp.route("/api/students/<student_id>")
def api_student(student_id):
    return jsonify(student_snapshot(get_db(), student_id))
