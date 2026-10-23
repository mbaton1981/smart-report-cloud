"""Explicit bearer-only employee-card access; legacy sync keys never authorize it."""
import json
import sqlite3
from flask import jsonify, request
from werkzeug.security import generate_password_hash

import employee_cards as cards


def register_employee_cards(app, connect, authenticate, status_action):
    def authorized():
        local = (request.remote_addr in {"127.0.0.1", "::1"}
                 and request.host.split(":")[0] in {"localhost", "127.0.0.1"})
        if not request.is_secure and not local:
            return None
        user = authenticate()
        if not user or user["role"] not in cards.CARD_ROLES:
            return None
        return user

    @app.after_request
    def private_card_responses(response):
        if request.path.startswith("/api/employee-cards"):
            response.headers["Cache-Control"] = "no-store, private"
            response.headers["Pragma"] = "no-cache"
            response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.route("/api/employee-cards", methods=["GET"])
    def employee_card_list():
        if not authorized():
            return jsonify(ok=False, error="Доступ к карточкам запрещён."), 403
        conn = connect()
        try:
            cards.cipher()
            rows = conn.execute("SELECT id,name,is_active FROM meta_employees ORDER BY name,id").fetchall()
            result = []
            for row in rows:
                record = cards.card_record(conn.cursor(), row["id"])
                result.append(dict(id=row["id"], name=row["name"], is_active=bool(row["is_active"]),
                                   missing=record["missing"], warnings=record["warnings"],
                                   revision=record["revision"]))
            return jsonify(ok=True, employees=result, version=1)
        except cards.CardError:
            return jsonify(ok=False, error="Карточки недоступны: проверьте ключ защищённого хранения."), 503
        except sqlite3.Error:
            return jsonify(ok=False, error="Хранилище карточек временно недоступно."), 503
        finally:
            conn.close()

    @app.route("/api/employee-cards/<int:employee_id>", methods=["GET", "PUT"])
    def employee_card(employee_id):
        actor = authorized()
        if not actor:
            return jsonify(ok=False, error="Доступ к карточкам запрещён."), 403
        conn = connect()
        try:
            cards.cipher()
            if request.method == "PUT":
                if request.content_length is None or request.content_length > 16000:
                    return jsonify(ok=False, error="Превышен допустимый размер карточки."), 400
                conn.execute("BEGIN IMMEDIATE")
                result = cards.save_card(conn.cursor(), employee_id, request.get_json(silent=True), actor, status_action)
                conn.commit()
            else:
                result = cards.card_record(conn.cursor(), employee_id)
            return jsonify(ok=True, employee=result)
        except cards.CardConflict as error:
            conn.rollback()
            return jsonify(ok=False, error=str(error), code="card_conflict"), 409
        except cards.CardUnavailable:
            conn.rollback()
            return jsonify(ok=False, error="Защищённые карточки недоступны. Проверьте ключ восстановления."), 503
        except cards.CardError as error:
            conn.rollback()
            return jsonify(ok=False, error=str(error)), 400
        except (sqlite3.Error, ValueError):
            conn.rollback()
            return jsonify(ok=False, error="Карточка не сохранена. Проверьте соединение и повторно откройте её."), 503
        finally:
            conn.close()

    @app.route("/api/employee-cards/<int:employee_id>/audit", methods=["GET"])
    def employee_card_audit(employee_id):
        if not authorized():
            return jsonify(ok=False, error="Доступ к истории реквизитов запрещён."), 403
        conn = connect()
        try:
            rows = conn.execute("""SELECT a.actor_id,a.actor_role,a.changed_at,a.fields_json,a.revision,
                    u.username AS actor
                FROM employee_card_audit a LEFT JOIN users u ON u.id=a.actor_id
                WHERE a.employee_id=? ORDER BY a.id DESC LIMIT 200""", (employee_id,)).fetchall()
            return jsonify(ok=True, changes=[dict(
                actor=row["actor"] or f"ID {row['actor_id']}", role=row["actor_role"],
                date=row["changed_at"], revision=row["revision"],
                fields=[cards.FIELDS.get(key, "Статус занятости") for key in json.loads(row["fields_json"])]
            ) for row in rows])
        finally:
            conn.close()

    @app.route("/api/employee-cards/accountants", methods=["GET", "POST"])
    def create_card_accountant():
        actor = authorized()
        if not actor or actor["role"] != "admin":
            return jsonify(ok=False, error="Назначать бухгалтера может только администратор."), 403
        if request.method == "GET":
            conn = connect()
            try:
                rows = conn.execute("SELECT id,username,is_active FROM users WHERE role='accountant' ORDER BY username").fetchall()
                return jsonify(ok=True, accountants=[dict(row) for row in rows])
            finally:
                conn.close()
        data = request.get_json(silent=True) or {}
        name, password = data.get("username"), data.get("password")
        if (not isinstance(name, str) or not name.strip() or len(name) > 100
                or not isinstance(password, str) or not 12 <= len(password) <= 200):
            return jsonify(ok=False, error="Укажите отдельный логин и пароль не короче 12 символов."), 400
        conn = connect()
        try:
            conn.execute("""INSERT INTO users(username,password_hash,role,is_active,created_at)
                VALUES(?,?,'accountant',1,datetime('now'))""", (name.strip(), generate_password_hash(password)))
            conn.commit()
            return jsonify(ok=True), 201
        except sqlite3.IntegrityError:
            conn.rollback()
            return jsonify(ok=False, error="Логин уже занят. Права существующей учётной записи не изменены."), 409
        finally:
            conn.close()

    @app.route("/api/employee-cards/accountants/<int:user_id>", methods=["DELETE"])
    def revoke_card_accountant(user_id):
        actor = authorized()
        if not actor or actor["role"] != "admin":
            return jsonify(ok=False, error="Отзывать доступ может только администратор."), 403
        conn = connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            changed = conn.execute("UPDATE users SET is_active=0 WHERE id=? AND role='accountant'", (user_id,)).rowcount
            if not changed:
                return jsonify(ok=False, error="Учётная запись бухгалтера не найдена."), 404
            conn.execute("UPDATE api_tokens SET revoked=1 WHERE user_id=?", (user_id,))
            conn.commit()
            return jsonify(ok=True)
        finally:
            conn.close()
