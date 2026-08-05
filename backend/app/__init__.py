from flask import Flask
from flask_cors import CORS

try:
    from .tickets import (
        persist_ticket_to_db,
        fetch_all_tickets,
        fetch_all_rules,
        fetch_token_usage,
        find_duplicate_targets,
        count_undecided_rules,
        delete_rules_bulk,
        merge_two_rules,
        update_ticket_status,
        update_ticket_details,
        get_ticket_status,
        delete_ticket,
        record_run_failure,
        add_manual_rule,
        edit_rule,
        delete_rule,
        _normalize_ticket_state as normalize_ticket_state,
    )
except ImportError:  # pragma: no cover
    from app.tickets import (
        persist_ticket_to_db,
        fetch_all_tickets,
        fetch_all_rules,
        fetch_token_usage,
        find_duplicate_targets,
        count_undecided_rules,
        delete_rules_bulk,
        merge_two_rules,
        update_ticket_status,
        update_ticket_details,
        get_ticket_status,
        delete_ticket,
        record_run_failure,
        add_manual_rule,
        edit_rule,
        delete_rule,
        _normalize_ticket_state as normalize_ticket_state,
    )

try:
    from .services.workflow import run_pipeline
except ImportError:  # pragma: no cover
    from app.services.workflow import run_pipeline

try:
    from .database import save_rule_decision
except ImportError:  # pragma: no cover
    from app.database import save_rule_decision


# Ticket fields a moderator may rewrite. The report id is excluded: every
# rule, crawl artefact and decision is filed under it.
EDITABLE_FIELDS = {"url", "env", "focus", "targets", "notes", "name"}


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

    @app.get("/api/rules")
    def list_rules():
        return {"rules": fetch_all_rules()}, 200

    @app.get("/api/usage")
    def token_usage():
        return fetch_token_usage(), 200

    @app.get("/api/tickets/duplicates")
    def duplicate_targets():
        from flask import request

        url = request.args.get("url", "")
        if not url:
            return {"error": "url is required"}, 400

        matches = find_duplicate_targets(url, request.args.get("exclude", ""))
        return {"duplicates": matches}, 200

    @app.post("/api/rules/merge")
    def merge_rules():
        from flask import request

        payload = request.get_json(force=True, silent=True) or {}
        rules = payload.get("rules")
        if not isinstance(rules, list) or len(rules) != 2:
            return {"error": "exactly two rules are required"}, 400

        try:
            result = merge_two_rules(
                rules[0],
                rules[1],
                preview=bool(payload.get("preview")),
            )
        except ValueError as exc:
            return {"error": str(exc)}, 400
        except LookupError as exc:
            return {"error": str(exc)}, 404

        return {"ok": True, **result}, 200

    @app.post("/api/rules/bulk-delete")
    def bulk_delete_rules():
        from flask import request

        payload = request.get_json(force=True, silent=True) or {}
        items = payload.get("rules")
        if not isinstance(items, list) or not items:
            return {"error": "rules must be a non-empty list"}, 400

        return {"ok": True, **delete_rules_bulk(items)}, 200

    @app.patch("/api/tickets/<report_id>")
    def patch_ticket(report_id):
        from flask import request

        payload = request.get_json(force=True, silent=True) or {}
        status = payload.get("status")
        fields = {k: v for k, v in payload.items() if k in EDITABLE_FIELDS}

        if not status and not fields:
            return {"error": "status or an editable field is required"}, 400

        if fields:
            current = get_ticket_status(report_id)
            if current is None:
                return {"error": "ticket not found"}, 404
            state, _ = normalize_ticket_state(current)
            if state == "done":
                return {"error": "a completed report cannot be edited"}, 409
            try:
                update_ticket_details(report_id, fields)
            except LookupError:
                return {"error": "ticket not found"}, 404

        if status:
            # Closing a review means every rule has been ruled on. Enforced
            # here as well as in the UI so the API cannot close a report with
            # rules still pending.
            state, _ = normalize_ticket_state(status)
            if state == "done":
                pending = count_undecided_rules(report_id)
                if pending:
                    return {
                        "error": "%d rule%s still undecided — approve or reject every rule first"
                        % (pending, "" if pending == 1 else "s"),
                        "undecided": pending,
                    }, 409

            updated = update_ticket_status(report_id, status)
            if updated == 0 and not fields:
                return {"error": "ticket not found"}, 404

        return {"ok": True}, 200

    @app.post("/api/tickets/<report_id>/run")
    def run_ticket(report_id):
        from flask import request

        payload = request.get_json(force=True, silent=True) or {}
        url = payload.get("url")
        environment = payload.get("environment", "desktop")
        ticket_context = payload.get("ticket_context", {})
        focus_region = payload.get("focus_region")
        # "discard" clears the domain's registry so known rules are proposed
        # again; "keep" (the default) dedupes against it as usual. Aborting is
        # handled by the caller simply not starting the run.
        duplicate_choice = payload.get("duplicate_choice")

        if not url:
            return {"error": "url is required"}, 400

        if duplicate_choice not in {None, "discard", "keep"}:
            return {"error": "duplicate_choice must be 'discard' or 'keep'"}, 400

        if get_ticket_status(report_id) is None:
            return {"error": "ticket not found; create it before starting the pipeline"}, 404

        update_ticket_status(report_id, "crawling")

        try:
            result = run_pipeline(
                report_id=report_id,
                url=url,
                environment=environment,
                ticket_context=ticket_context,
                focus_region=focus_region,
                duplicate_choice=duplicate_choice,
            )
        except Exception as exc:
            update_ticket_status(report_id, "failed")
            record_run_failure(report_id, str(exc))
            return {"ok": False, "error": str(exc)}, 500

        if result.get("status") in {"review", "validated", "generated", "no_rules", "ok"}:
            update_ticket_status(report_id, "review")
        elif result.get("status") == "crawl_failed":
            update_ticket_status(report_id, "crawl_failed")
            record_run_failure(
                report_id,
                "Crawl failed at stage '%s': %s"
                % (
                    result.get("crawl_stage", "unknown"),
                    result.get("crawl_error", "unknown error"),
                ),
            )
        else:
            update_ticket_status(report_id, "failed")
            record_run_failure(
                report_id,
                result.get("error") or "Pipeline returned status '%s'" % result.get("status"),
            )

        return {"ok": True, "result": result}, 200

    def _guard_exists(report_id):
        """
        Rules stay editable for the life of the report, including after it is
        closed — the library is where old rules get cleaned up. Only the
        ticket's own fields freeze on completion.
        """
        if get_ticket_status(report_id) is None:
            return {"error": "ticket not found"}, 404
        return None

    @app.post("/api/tickets/<report_id>/rules")
    def add_rule(report_id):
        from flask import request

        blocked = _guard_exists(report_id)
        if blocked:
            return blocked

        payload = request.get_json(force=True, silent=True) or {}
        try:
            result = add_manual_rule(
                report_id,
                payload.get("rule", ""),
                payload.get("rule_type"),
            )
        except ValueError as exc:
            return {"error": str(exc)}, 400
        except RuntimeError as exc:
            return {"error": str(exc)}, 404

        return {"ok": True, **result}, 201

    @app.patch("/api/tickets/<report_id>/rules")
    def patch_rule(report_id):
        from flask import request

        blocked = _guard_exists(report_id)
        if blocked:
            return blocked

        payload = request.get_json(force=True, silent=True) or {}
        rule = payload.get("rule")
        new_rule = payload.get("new_rule")
        if not rule:
            return {"error": "rule is required"}, 400

        try:
            result = edit_rule(report_id, rule, new_rule or "")
        except ValueError as exc:
            return {"error": str(exc)}, 400
        except LookupError as exc:
            return {"error": str(exc)}, 404

        return {"ok": True, **result}, 200

    @app.delete("/api/tickets/<report_id>/rules")
    def remove_rule(report_id):
        from flask import request

        blocked = _guard_exists(report_id)
        if blocked:
            return blocked

        payload = request.get_json(force=True, silent=True) or {}
        rule = payload.get("rule")
        if not rule:
            return {"error": "rule is required"}, 400

        try:
            result = delete_rule(report_id, rule)
        except LookupError as exc:
            return {"error": str(exc)}, 404

        return {"ok": True, **result}, 200

    @app.post("/api/tickets/<report_id>/decisions")
    def decide_rule(report_id):
        from flask import request

        payload = request.get_json(force=True, silent=True) or {}
        rule = payload.get("rule")
        decision = payload.get("decision")
        decided_by = payload.get("decided_by")

        if not rule:
            return {"error": "rule is required"}, 400

        # null/absent decision clears the rule back to undecided, matching the
        # frontend toggle where clicking the active choice deselects it.
        if decision not in {"approve", "reject", None}:
            return {"error": "decision must be 'approve', 'reject', or null"}, 400

        try:
            decisions = save_rule_decision(
                report_id=report_id,
                rule=rule,
                decision=decision,
                decided_by=decided_by,
            )
        except RuntimeError as exc:
            return {"error": str(exc)}, 404

        return {"ok": True, "decisions": decisions}, 200

    @app.delete("/api/tickets/<report_id>")
    def remove_ticket(report_id):
        deleted = delete_ticket(report_id)
        if deleted == 0:
            return {"error": "ticket not found"}, 404
        return {"ok": True, "deleted": deleted}, 200

    return app


app = create_app()
