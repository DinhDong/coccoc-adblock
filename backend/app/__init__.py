from flask import Flask
from flask_cors import CORS

try:
    from .tickets import persist_ticket_to_db
except ImportError:  # pragma: no cover
    from app.tickets import persist_ticket_to_db


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

    return app


app = create_app()
