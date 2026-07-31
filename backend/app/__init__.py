from flask import Flask
from flask_cors import CORS

try:
    from .tickets import (
        persist_ticket_to_db,
        fetch_all_tickets,
        update_ticket_status,
    )
except ImportError:  # pragma: no cover
    from app.tickets import (
        persist_ticket_to_db,
        fetch_all_tickets,
        update_ticket_status,
    )

try:
    from .services.workflow import run_pipeline
except ImportError:  # pragma: no cover
    from app.services.workflow import run_pipeline


def create_app():
    app = Flask(__name__)
    CORS(app)

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.post("/api/tickets")
    def create_ticket():
        from flask import request

        payload = request.get_json(force=True, silent=True) or {}
        if not payload:
            return {"error": "empty payload"}, 400

        record_id = persist_ticket_to_db(payload)
        return {"ok": True, "record_id": record_id}, 201

    @app.get("/api/tickets")
    def list_tickets():
        return {"tickets": fetch_all_tickets()}, 200

    @app.patch("/api/tickets/<report_id>")
    def patch_ticket(report_id):
        from flask import request

        payload = request.get_json(force=True, silent=True) or {}
        status = payload.get("status")
        if not status:
            return {"error": "status is required"}, 400

        updated = update_ticket_status(report_id, status)
        if updated == 0:
            return {"error": "ticket not found"}, 404

        return {"ok": True, "updated": updated}, 200

    @app.post("/api/tickets/<report_id>/run")
    def run_ticket(report_id):
        from flask import request

        payload = request.get_json(force=True, silent=True) or {}
        url = payload.get("url")
        environment = payload.get("environment", "desktop")
        ticket_context = payload.get("ticket_context", {})
        focus_region = payload.get("focus_region")

        if not url:
            return {"error": "url is required"}, 400

        update_ticket_status(report_id, "inprocess")

        try:
            result = run_pipeline(
                report_id=report_id,
                url=url,
                environment=environment,
                ticket_context=ticket_context,
                focus_region=focus_region,
            )
        except Exception as exc:
            update_ticket_status(report_id, "failed")
            return {"ok": False, "error": str(exc)}, 500

        if result.get("status") in {"review", "validated"}:
            update_ticket_status(report_id, "review")
        elif result.get("status") in {"generated", "no_rules"}:
            update_ticket_status(report_id, "review")
        else:
            update_ticket_status(report_id, "failed")

        return {"ok": True, "result": result}, 200

    return app


app = create_app()
