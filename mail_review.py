"""Read-only, offline review of the isolated mail queue. No mail/accounting calls."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import uuid
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
from html import escape
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname

from expense_documents import decimal_text, net_invoice_issues, parse_invoice_money, sha256_file
from invoice_processing import recognize_invoice
from mail_import import (
    APPLICATION_ID, TYPE_LABELS, MailImportError, classify_mail_document,
    mail_review_warnings, recognition_status,
)


FIELD_LABELS = {
    "supplier_name": "Поставщик / составитель",
    "invoice_number": "Номер фактуры / чека",
    "document_date": "Дата документа",
    "due_date": "Срок оплаты",
    "net_amount": "Без НДС",
    "vat_amount": "НДС",
    "gross_amount": "С НДС, до округления",
    "rounding_amount": "Указанное округление",
    "amount_due": "К оплате (не сумма уже оплаченного чека)",
    "currency_code": "Валюта",
    "document_type": "Тип",
}
STATUSES = {
    "checked": "Сверено с оригиналом",
    "decision": "Нужно решение перед будущим учётом",
    "excluded": "Не является финансовым документом",
    "not_checked": "С оригиналом ещё не сверено",
}
RUN_FIELDS = (
    "started_at", "since", "until", "messages_checked", "messages_in_period",
    "attachments_found", "documents_added", "duplicates_skipped", "errors", "completed",
)


def allocation_review(fields: dict, catalog: dict | None = None) -> dict:
    orders = fields.get("order_proposals") or []
    rows = []
    for order in orders:
        rows.append({
            key: order.get(key) for key in (
                "order_id", "project_code", "original_marker", "net_amount", "vat_amount",
                "gross_amount", "ambiguous", "vat_items",
            )
        } | {
            "pages": sorted(set(order.get("pages") or [])
                            | {source["page"] for source in order.get("sources", []) if "page" in source}
                            | {item["page"] for item in order.get("vat_items", []) if "page" in item}),
            "object_id": order.get("object_id") if catalog is not None else None,
            "destination": order.get("destination", "unselected") if catalog is not None else "unselected",
            "expense_category": order.get("expense_category") if catalog is not None else None,
            "object_name": (catalog or {}).get(order.get("object_id"), {}).get("name"),
        })
    def total(key):
        values = [parse_invoice_money(row.get(key)) for row in rows]
        return decimal_text(sum(values, Decimal(0))) if rows and all(v is not None for v in values) else None

    net_total, vat_total = total("net_amount"), total("vat_amount")
    return {
        "orders": rows, "net_total": net_total, "vat_total": vat_total,
        "net_matches": bool(rows) and net_total == fields.get("net_amount"),
        "vat_matches": bool(rows) and vat_total is not None and vat_total == fields.get("vat_amount"),
        "items_count": sum(len(row.get("vat_items") or []) for row in rows),
        "issues": net_invoice_issues(fields, rows) if rows else [],
        "warnings": fields.get("order_proposal_warnings") or [],
    }


def collect_review(root: Path, assessments: dict | None = None, recognitions: dict | None = None) -> dict:
    root = root.resolve()
    assessments = assessments or {}
    unknown = set(assessments) - {
        path.stem for path in (root / "originals").iterdir() if path.is_file()
    }
    if unknown:
        raise MailImportError("Assessment references an absent original; review stopped.")
    database = root / "mail_queue.sqlite3"
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        if (conn.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID
                or conn.execute("PRAGMA user_version").fetchone()[0] != 1):
            raise MailImportError("Not a supported mail trial database.")
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise MailImportError("Mail queue integrity check failed.")
        if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise MailImportError("Mail queue source links failed validation.")
        documents = []
        seen = set()
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        has_trial = {"trial_objects", "trial_allocation_revisions"} <= tables
        catalog = {row["id"]: dict(row) for row in conn.execute("SELECT * FROM trial_objects")} if has_trial else {}
        for row in conn.execute("SELECT * FROM documents ORDER BY id"):
            digest = row["sha256"]
            if digest in seen:
                raise MailImportError("Repeated document hash in queue.")
            seen.add(digest)
            original = (root / row["original"]).resolve()
            if not original.is_relative_to((root / "originals").resolve()):
                raise MailImportError("Original is outside isolated originals directory.")
            if not original.is_file() or sha256_file(original) != digest:
                raise MailImportError(f"Original checksum mismatch for document {row['id']}.")
            assessment = assessments.get(digest, {})
            checked = assessment.get("fields", {})
            if set(checked) - set(FIELD_LABELS):
                raise MailImportError("Unknown reviewed field.")
            if assessment.get("status", "not_checked") not in STATUSES:
                raise MailImportError("Unknown review status.")
            saved_result = json.loads(row["result_json"])
            fields = saved_result.get("fields", {})
            extracted = {key: fields.get(key) for key in FIELD_LABELS}
            current_result = (recognitions or {}).get(digest)
            current = current_result["fields"] if current_result else fields
            revisions = conn.execute(
                "SELECT * FROM trial_allocation_revisions WHERE document_id=? ORDER BY revision",
                (row["id"],),
            ).fetchall() if has_trial else []
            draft_history = []
            for revision in revisions:
                if revision["original_sha256"] != digest:
                    raise MailImportError("Draft revision does not match the original SHA.")
                payload = json.loads(revision["payload_json"])
                draft_history.append({
                    "revision": revision["revision"], "created_at": revision["created_at"],
                    "fields": {key: payload.get(key) for key in FIELD_LABELS},
                    "allocation": allocation_review(payload, catalog),
                    "warnings": mail_review_warnings(payload),
                })
            draft = draft_history[-1] if draft_history else None
            documents.append({
                "id": row["id"], "sha256": digest, "original_uri": original.as_uri(),
                "sha_ok": True, "extracted": extracted, "checked": checked,
                "status": assessment.get("status", "not_checked"),
                "notes": assessment.get("notes", []),
                "evidence": assessment.get("evidence", ""),
                "sources_count": conn.execute(
                    "SELECT count(*) FROM sources WHERE document_id=?", (row["id"],)
                ).fetchone()[0],
                # Only field codes, not raw OCR, mail envelopes or arbitrary exception text.
                "unconfirmed_fields": [key for key in FIELD_LABELS
                                       if extracted[key] in (None, "", "UNKNOWN")],
                "current": {key: current.get(key) for key in FIELD_LABELS},
                "review_context": {key: current.get(key) for key in (
                    "prepaid", "reverse_charge", "tax_reviewed",
                    "due_date_kind", "due_date_review_required", "due_date_reviewed",
                )},
                "page_count": current_result.get("page_count") if current_result else None,
                "recognition_state": current_result.get("state") if current_result else row["state"],
                "receipt_status": "Оригинал получен и сохранён; SHA-256 проверен",
                "recognition": recognition_status(saved_result, row["state"]),
                "allocation": draft["allocation"] if draft else allocation_review(current),
                "draft": draft, "draft_history": draft_history,
                "warnings": mail_review_warnings(current),
            })
        if set(assessments) - seen:
            raise MailImportError("Assessment is not linked to a queued document.")
        source_count = conn.execute("SELECT count(*) FROM sources").fetchone()[0]
        issues_count = conn.execute("SELECT count(*) FROM issues").fetchone()[0]
    runs = []
    for path in sorted((root / "runs").glob("*.json")):
        run = json.loads(path.read_text(encoding="utf-8"))
        runs.append({key: run.get(key) for key in RUN_FIELDS})
    return {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "documents_count": len(documents), "sources_count": source_count,
        "issues_count": issues_count, "unique_hashes": len(seen), "integrity": "ok",
        "statuses": dict(Counter(item["status"] for item in documents)),
        "documents": documents, "runs": runs,
        "scope": "Исходные записи импорта и оригиналы сохранены. Черновики — только в тестовой очереди; рабочий учёт не подключён.",
    }


def refresh_recognition(root: Path, recognizer=recognize_invoice) -> dict:
    baseline = collect_review(root)
    results = {}
    for document in baseline["documents"]:
        path = Path(url2pathname(urlsplit(document["original_uri"]).path))
        result = recognizer([path])
        classify_mail_document(result["fields"])
        results[document["sha256"]] = result
    return results


def build_original_previews(output: Path, report: dict) -> None:
    import pypdfium2 as pdfium
    from PIL import Image

    for document in report["documents"]:
        original = Path(url2pathname(urlsplit(document["original_uri"]).path))
        folder = output / "pages" / document["sha256"]
        folder.mkdir(parents=True, exist_ok=True)
        if original.suffix.casefold() == ".pdf":
            pdf = pdfium.PdfDocument(str(original))
            try:
                count = len(pdf)
                for index in range(count):
                    page = pdf[index]
                    try:
                        bitmap = page.render(scale=1.5)
                        try:
                            with bitmap.to_pil() as image:
                                image.save(folder / f"page-{index + 1}.png")
                        finally:
                            bitmap.close()
                    finally:
                        page.close()
            finally:
                pdf.close()
        else:
            with Image.open(original) as image:
                # Match the existing extractor: one image attachment is one page.
                image.convert("RGB").save(folder / "page-1.png")
            count = 1
        images = "".join(
            f'<section id="page-{page}"><p>Страница {page} / {count}</p>'
            f'<img loading="lazy" src="page-{page}.png" alt="Страница {page} оригинала"></section>'
            for page in range(1, count + 1)
        )
        (folder / "index.html").write_text(
            '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
            '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; img-src \'self\' file:; '
            'style-src \'unsafe-inline\'; base-uri \'none\'">'
            '<style>body{margin:0;background:#e0e5eb;font:16px system-ui}section{margin:8px 0}'
            'p{margin:8px}img{display:block;width:100%;height:auto;background:white}</style></head><body>'
            + images + "</body></html>", encoding="utf-8",
        )
        document["preview"] = f"pages/{document['sha256']}/index.html"
        document["original_page_count"] = count


def render_allocations(item: dict) -> str:
    allocation = item.get("allocation", {})
    orders = allocation.get("orders") or []
    if not orders:
        return "<p>Предложения распределения по заказам не найдены. Назначение не выбрано.</p>"
    rows = []
    for order in orders:
        preview = item.get("preview")
        pages = " ".join(
            f'<a href="{escape(preview or item["original_uri"], quote=True)}'
            f'{"#page-" if preview else "#page="}{page}" target="original-{item["id"]}">Стр. {page}</a>'
            for page in order["pages"]
        )
        items = "".join(
            f'<tr><td>{part["page"]}</td><td>{escape(str(part["item"]))}</td>'
            f'<td>{escape(str(part["net_amount"]))}</td><td>{escape(str(part["vat_rate"]))}%</td>'
            f'<td>{escape(str(part["vat_amount"]))}</td></tr>'
            for part in order.get("vat_items") or []
        )
        destination = order["destination"]
        if destination == "object":
            selection = escape(str(order.get("object_name") or "Объект не найден в тестовом каталоге"))
        elif destination == "company":
            selection = "Общие расходы ТЕСТОВОЙ фирмы"
        else:
            selection = "<strong>Выберите объект или общие расходы фирмы</strong>"
        category = escape(str(order.get("expense_category") or ""))
        rows.append(
            f'<tr><td>{escape(str(order.get("order_id") or "Не указан"))}</td>'
            f'<td>{escape(str(order.get("original_marker") or "Не определён"))}</td>'
            f'<td>{selection}<br>{category}</td>'
            f'<td>{escape(display("net_amount", order.get("net_amount")))}</td>'
            f'<td>{escape(display("vat_amount", order.get("vat_amount")))}</td><td>{pages}</td></tr>'
            + (f'<tr><td colspan="6"><details><summary>Позиции заказа: {len(order.get("vat_items") or [])}</summary>'
               f'<table><tr><th>Страница</th><th>Позиция</th><th>Без НДС</th><th>Ставка</th><th>НДС</th></tr>{items}</table>'
               '</details></td></tr>' if items else "")
        )
    notices = list(dict.fromkeys([*allocation.get("warnings", []), *allocation.get("issues", [])]))
    return (
        '<h3>Существующий механизм разноски — предложения, не расходы</h3>'
        f'<p>Заказов: {len(orders)}; позиций: {allocation["items_count"]}; '
        f'нетто заказов: {escape(str(allocation["net_total"]))}; '
        f'НДС заказов: {escape(str(allocation["vat_total"]))}. '
        f'Нетто сходится: {"да" if allocation["net_matches"] else "НЕТ"}; '
        f'НДС сходится: {"да" if allocation["vat_matches"] else "не подтверждено"}.</p>'
        '<p>Распределение сверяется с нетто фактуры. Округление фактуры не добавляется к заказам. '
        'Повторяющиеся итоговые строки не суммируются повторно. Маркировки 1035 и P1035 не объявляются '
        'одним объектом без подтверждённого соответствия. Все объекты здесь тестовые. '
        'Редактирование доступно в окне «Тестовая почтовая очередь». Подтверждение и перенос отсутствуют.</p>'
        '<table><tr><th>Заказ</th><th>Маркер оригинала</th><th>Назначение</th><th>Без НДС</th><th>НДС</th><th>Источник</th></tr>'
        + "".join(rows) + "</table><ul>" + "".join(f"<li>{escape(note)}</li>" for note in notices) + "</ul>"
    )


def display(key: str, value) -> str:
    if value in (None, "", "UNKNOWN"):
        return "Не указано / неизвестно"
    return str(TYPE_LABELS.get(value, value) if key == "document_type" else value)


def render_review(report: dict) -> str:
    cards = []
    for item in report["documents"]:
        rows = []
        draft = item.get("draft")
        for key, label in FIELD_LABELS.items():
            extracted = item["extracted"][key]
            reviewed = item["checked"].get(key)
            is_checked = key in item["checked"]
            different = is_checked and extracted != reviewed
            rows.append(
                f'<tr class="{"changed" if different else ""}"><th>{escape(label)}</th>'
                f'<td>{escape(display(key, extracted))}</td>'
                f'<td>{escape(display(key, item.get("current", item["extracted"])[key]))}</td>'
                f'<td>{escape(display(key, reviewed)) if is_checked else "Не сверено"}</td>'
                + (f'<td>{escape(display(key, draft["fields"].get(key)))}</td>' if draft else "") + '</tr>'
            )
        notes = "".join(f"<li>{escape(note)}</li>" for note in item["notes"])
        history = "".join(
            f'<li>{escape(event["at"])} — {escape(event["description"])}</li>'
            for event in item.get("history", [])
        ) or "<li>Исходное распознавание сохранено в очереди.</li>"
        draft_history = "".join(
            f'<li>{escape(revision["created_at"])} — сохранён тестовый черновик, ревизия {revision["revision"]}; '
            f'строк: {len(revision["allocation"]["orders"])}; нетто: {escape(str(revision["allocation"]["net_total"]))}.'
            '<details><summary>Значения и назначения этой ревизии</summary><pre>'
            f'{escape(json.dumps({"fields": revision["fields"], "allocation": revision["allocation"]}, ensure_ascii=False, indent=2))}'
            '</pre></details></li>' for revision in item.get("draft_history", [])
        )
        draft_note = (
            f'<p><strong>Сохранён тестовый черновик, ревизия {draft["revision"]}.</strong> '
            'Разноска ниже показывает сохранённые назначения. Проверенные значения не перезаписаны.</p>'
        ) if draft else "<p>Тестовая разноска ещё не сохранена.</p>"
        warnings = "".join(
            f"<li>{escape(warning)}</li>" for warning in (
                draft.get("warnings", []) if draft else item.get("warnings", [])
            )
        )
        cards.append(
            f'<article id="doc-{item["id"]}"><h2>Документ №{item["id"]}: '
            f'{escape(STATUSES[item["status"]])}</h2>'
            f'<p>Оригинал цел, SHA-256 совпадает. Источников: {item["sources_count"]}. '
            f'<a href="{escape(item["original_uri"], quote=True)}">Открыть оригинал на компьютере</a></p>'
            f'<p><strong>Получение:</strong> {escape(item.get("receipt_status", "Оригинал сохранён"))}.<br>'
            f'<strong>Распознавание сохранённого результата:</strong> '
            f'{escape(item.get("recognition", {}).get("label", "Требуется ручная проверка"))}. '
            f'{escape(", ".join(item.get("recognition", {}).get("missing_fields", [])))}</p>'
            f'<p>{escape(item["evidence"])}</p><ul>{notes}</ul><div class="comparison"><section>'
            f'{draft_note}'
            f'<ul>{warnings}</ul>'
            f'<p>Страниц в обработке: {item.get("page_count") or "см. оригинал"}. '
            f'Текущее состояние: {escape(str(item.get("recognition_state") or "requires_review"))}.</p>'
            '<table><thead><tr><th>Поле</th><th>Первичное распознавание</th>'
            '<th>Парсер сейчас</th><th>Проверено по оригиналу</th>'
            + ('<th>Сохранённый тестовый черновик</th>' if draft else '')
            + f'</tr></thead><tbody>{"".join(rows)}</tbody></table>'
            f'<details><summary>История проверки и исправлений</summary><ul>{history}{draft_history}</ul></details>'
            f'{render_allocations(item)}</section><aside><p>Оригинал хранится только локально. '
            'В нём могут быть персональные и платёжные данные.</p>'
            f'<iframe loading="lazy" name="original-{item["id"]}" title="Оригинал документа {item["id"]}" '
            f'src="{escape(item.get("preview") or item["original_uri"], quote=True)}"></iframe>'
            '</aside></div></article>'
        )
    totals = " · ".join(
        f"{escape(STATUSES[key])}: {count}" for key, count in report["statuses"].items()
    )
    decision_links = " · ".join(
        f'<a href="#doc-{item["id"]}">№{item["id"]}</a>'
        for item in report["documents"] if item["status"] == "decision"
    ) or "Нет"
    run_rows = "".join(
        "<tr>" + "".join(f"<td>{escape(str(run.get(key)))}</td>" for key in (
            "started_at", "messages_checked", "messages_in_period", "attachments_found",
            "documents_added", "duplicates_skipped", "errors", "completed",
        )) + "</tr>" for run in report["runs"]
    )
    return (
        '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; frame-src file: \'self\'; img-src file: \'self\'; style-src \'unsafe-inline\'; base-uri \'none\'; form-action \'none\'">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>SmartReport — проверка тестовой очереди</title>'
        '<style>body{font:16px system-ui;max-width:1900px;margin:30px auto;padding:0 20px;color:#172638;background:#f3f6fa}'
        'article,header{background:white;padding:22px;margin:20px 0;border-radius:12px}'
        'table{border-collapse:collapse;width:100%;font-size:14px}th,td{border:1px solid #ccd5df;padding:9px;text-align:left}'
        '.changed{background:#fff2cc}li{margin:8px 0}a{color:#135ca6}h2{font-size:21px}'
        '.comparison{display:grid;grid-template-columns:minmax(0,1fr) minmax(450px,1fr);gap:20px}'
        'iframe{width:100%;height:90vh;border:1px solid #ccd5df;position:sticky;top:10px}'
        'section{min-width:0;overflow-wrap:anywhere}aside{min-width:0}'
        '@media(max-width:1100px){.comparison{display:block}iframe{height:700px;position:static}}'
        '</style></head><body>'
        '<header><h1>Проверка тестовой очереди фактур</h1>'
        '<p><strong>Это отчёт, не подтверждение расходов. Перенос в рабочий учёт отключён.</strong></p>'
        f'<p>Снимок проверки (UTC): {escape(report["created_at"])}. Последующие импорты в этот снимок не входят.</p>'
        f'<p>Оригиналов: {report["documents_count"]}; уникальных SHA: {report["unique_hashes"]}; '
        f'источников: {report["sources_count"]}; технических ошибок очереди: {report["issues_count"]}; SQLite: ok.</p>'
        f'<p>{totals}</p><p>Решения перед будущим учётом: {decision_links}</p>'
        '<p>Жёлтым выделены уточнения. Первичные результаты сохранены без изменения.'
        ' Неизвестный НДС не заменён нулём. Суммы чеков не выданы за непогашенный долг.</p>'
        '<p>В отчёт не включены пароли, тексты писем, отправители, адреса доставки, телефоны, '
        'банковские реквизиты и номера автомобилей. Сам отчёт содержит коммерческие данные — хранить локально. '
        'Оригиналы показываются рядом: локальные изображения всех страниц, без отправки в интернет. '
        'Каталог просмотра содержит персональные данные оригиналов и не предназначен для публикации. '
        'История сохраняется отдельными снимками; исходная очередь не перезаписывается.</p>'
        '<details><summary>Результаты запусков импорта (время UTC)</summary><table><thead><tr>'
        '<th>Запуск</th><th>Проверено писем</th><th>В периоде</th><th>Вложений</th>'
        '<th>Добавлено</th><th>Дубли</th><th>Ошибки обработки</th><th>Запуск завершён</th>'
        f'</tr></thead><tbody>{run_rows}</tbody></table></details></header>'
        '<p><strong>Завершённое получение и отсутствие технических ошибок не означают полного '
        'распознавания или разрешения учёта. Результат распознавания указан отдельно у каждого документа.</strong></p>'
        + "".join(cards) + "</body></html>"
    )


def export_review(root: Path, assessments: dict | None = None, recognitions: dict | None = None) -> Path:
    output = root.resolve() / "review"
    output.mkdir(exist_ok=True)
    previous_path = output / "audit.json"
    previous = json.loads(previous_path.read_text(encoding="utf-8")) if previous_path.exists() else None
    if assessments is None and previous:
        assessments = {doc["sha256"]: {
            "fields": doc.get("checked", {}), "status": doc["status"],
            "notes": doc.get("notes", []), "evidence": doc.get("evidence", ""),
        } for doc in previous["documents"]}
    if recognitions is None and previous:
        recognitions = {doc["sha256"]: {
            "fields": {**doc.get("current", doc["extracted"]),
                       **doc.get("review_context", {}),
                       "order_proposals": doc.get("allocation", {}).get("orders", []),
                       "order_proposal_warnings": doc.get("allocation", {}).get("warnings", [])},
            "state": doc.get("recognition_state", "requires_review"), "page_count": doc.get("page_count"),
        } for doc in previous["documents"]}
    report = collect_review(root, assessments, recognitions)
    build_original_previews(output, report)
    history_dir = output / "history"
    history_dir.mkdir(exist_ok=True)
    histories = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(history_dir.glob("*.json"))]
    if not histories and previous:
        histories.append(previous)
        with (history_dir / "000_initial.json").open("x", encoding="utf-8") as stream:
            json.dump(previous, stream, ensure_ascii=False, indent=2)
    for document in report["documents"]:
        events = []
        last_values = {}
        for snapshot in [*histories, report]:
            old_doc = next((doc for doc in snapshot["documents"] if doc["sha256"] == document["sha256"]), None)
            if old_doc is None:
                continue
            for name, label in (("extracted", "Первичное распознавание"),
                                ("checked", "Сверка с оригиналом"), ("current", "Повторная локальная обработка")):
                for key, value in old_doc.get(name, {}).items():
                    if (name, key) not in last_values or last_values[name, key] != value:
                        before = last_values.get((name, key))
                        change = (f"{display(key, before)} → {display(key, value)}"
                                  if (name, key) in last_values else display(key, value))
                        events.append({
                            "at": snapshot["created_at"],
                            "description": f"{label}: {FIELD_LABELS[key]}: {change}",
                        })
                        last_values[name, key] = value
        document["history"] = events
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    with (history_dir / f"{stamp}_{uuid.uuid4().hex}.json").open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    (output / "audit.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    target = output / "index.html"
    target.write_text(render_review(report), encoding="utf-8")
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    default = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "SmartReport" / "mail_faktura_trial"
    parser.add_argument("--data-dir", type=Path, default=default)
    parser.add_argument("--assessment", type=Path)
    parser.add_argument("--refresh", action="store_true", help="Reprocess local originals; never connects to mail.")
    args = parser.parse_args()
    assessments = json.loads(args.assessment.read_text(encoding="utf-8")) if args.assessment else None
    print(export_review(args.data_dir, assessments,
                        refresh_recognition(args.data_dir) if args.refresh else None))


if __name__ == "__main__":
    main()
