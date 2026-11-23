"""Flask application factory for the label-relabel reconciliation page."""

from __future__ import annotations

import os
from typing import Optional

from flask import Flask, jsonify, render_template, request

from .csvio import parse_review_csv
from .store import Store, StoreError


def _store(app: Flask) -> Store:
    return app.extensions["relabel_store"]


def create_app(db_path: Optional[str] = None) -> Flask:
    app = Flask(__name__, template_folder="../templates", static_folder="../static")
    app.config["MAX_CONTENT_LENGTH"] = 12 * 1024 * 1024
    app.json.ensure_ascii = False
    db_path = db_path or os.environ.get("RELABEL_DB", "/data/relabel.sqlite3")
    app.extensions["relabel_store"] = Store(db_path)

    def fail(message: str, status: int = 400, extra: Optional[dict] = None):
        payload = {"ok": False, "error": message}
        if extra:
            payload.update(extra)
        return jsonify(payload), status

    @app.errorhandler(StoreError)
    def _store_error(exc: StoreError):
        return fail(str(exc))

    @app.errorhandler(413)
    def _too_large(_exc):
        return fail("上传文件过大。", 413)

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/healthz")
    def healthz():
        return jsonify({"ok": True})

    @app.get("/api/summary")
    def summary():
        return jsonify({"ok": True, "summary": _store(app).summary()})

    @app.get("/api/sample/<int:bottle>")
    def get_sample(bottle: int):
        sample = _store(app).get_sample(bottle)
        if sample is None:
            return fail(f"瓶号 {bottle} 没有留样记录。", 404)
        return jsonify({"ok": True, "sample": sample})

    @app.post("/api/import")
    def import_csv():
        upload = request.files.get("file")
        if upload is None or not upload.filename:
            return fail("请选择一个复核 CSV 文件。")
        try:
            total = int(request.form.get("total_bottles", ""))
        except ValueError:
            return fail("请填写总瓶数（整数）。")
        label_raw = (request.form.get("label_cut") or "").strip()
        label_cut = None
        if label_raw:
            try:
                label_cut = int(label_raw)
            except ValueError:
                return fail("换标瓶号必须是整数。")
        try:
            rows, _lines = parse_review_csv(upload.read())
            info = _store(app).import_rows(total, label_cut, rows)
        except StoreError as exc:
            return fail(str(exc))
        except Exception as exc:  # write failure: old committed state remains
            return fail(f"导入失败，已保留原核对结果：{exc}", 500)
        return jsonify(
            {"ok": True, "import": info, "summary": _store(app).summary()}
        )
    @app.post("/api/label")
    def set_label():
        payload = request.get_json(silent=True) or {}
        try:
            label_cut = int(payload.get("label_cut"))
        except (TypeError, ValueError):
            return fail("换标瓶号必须是整数。")
        store = _store(app)
        try:
            info = store.set_label_cut(label_cut)
        except StoreError as exc:
            return fail(str(exc))
        except Exception as exc:
            return fail(f"写入失败，沿用原结果：{exc}", 500)
        return jsonify({"ok": True, "label": info, "summary": store.summary()})

    @app.post("/api/sample")
    def set_sample():
        payload = request.get_json(silent=True) or {}
        try:
            bottle = int(payload.get("bottle"))
        except (TypeError, ValueError):
            return fail("瓶号必须是整数。")
        verdict = str(payload.get("verdict", "")).strip()
        store = _store(app)
        try:
            info = store.set_verdict(bottle, verdict)
        except StoreError as exc:
            return fail(str(exc))
        except Exception as exc:
            committed = store.summary()
            return fail(
                f"写入失败，沿用原结果：{exc}",
                500,
                {"summary": committed, "applied": False},
            )
        return jsonify(
            {"ok": True, "update": info, "summary": store.summary()}
        )

    @app.post("/api/demo")
    def load_demo():
        """Seed the server with the deterministic demo shift."""
        from .demo import LABEL_CUT, TOTAL_BOTTLES, demo_rows

        store = _store(app)
        try:
            info = store.import_rows(TOTAL_BOTTLES, LABEL_CUT, demo_rows())
        except Exception as exc:
            return fail(f"演示数据写入失败：{exc}", 500)
        return jsonify({"ok": True, "import": info, "summary": store.summary()})

    @app.post("/api/reset")
    def reset():
        store = _store(app)
        store.reset()
        return jsonify({"ok": True, "summary": store.summary()})

    return app


def main() -> None:
    app = create_app()
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port, threaded=True)
