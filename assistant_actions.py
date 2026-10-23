"""Shared, explicit capabilities; no arbitrary method dispatch from model output."""

CAPABILITIES = {
    "search": ("Смены и часы", "read"),
    "hours_summary": ("Кто сколько отработал", "read"),
    "check": ("Проверка сохранённых ставок и данных", "read"),
    "anomalies": ("Дубли и смены без клиентской ставки", "read"),
    "transport_check": ("Проверка клиентского транспорта", "read"),
    "explain": ("Пояснение сохранённых базовых сумм", "read"),
    "employees": ("Справочник сотрудников без реквизитов", "read"),
    "objects": ("Справочник фирм и объектов", "read"),
    "directories": ("Редактирование справочников в штатном разделе", "native_section"),
    "payroll_pdf": ("Предпросмотр зарплатного отчёта", "native_preview"),
    "customer_pdf": ("Предпросмотр клиентского отчёта", "native_preview"),
    "journal_pdf": ("Предпросмотр журнала работ", "native_preview"),
    "catalog_preview": ("Предпросмотр применения цен справочника", "native_preview"),
    "employee_card": ("Защищённая карточка сотрудника", "native_dialog"),
    "accountant_export": ("Данные сотрудника для бухгалтера", "native_dialog"),
    "balance": ("Баланс текущего локального профиля", "local_read"),
    "expenses": ("Журнал расходов текущего локального профиля", "local_read"),
    "expense_documents": ("Документы расходов", "native_section"),
    "payroll_history": ("История зарплатных событий и штатные подтверждения", "native_dialog"),
    "shift_form": ("Форма новой смены без сохранения", "native_section"),
    "mail_prepare": ("Черновик письма с PDF, без отправки", "native_dialog"),
    "incoming_documents": ("Входящие документы", "native_dialog"),
    "invoice_period": ("Раздел выставления и открытия периода", "native_section"),
}

NATIVE_ACTIONS = {key for key, (_, mode) in CAPABILITIES.items()
                  if mode in {"native_dialog", "native_section", "local_read"}}
REPORT_ACTIONS = {"payroll_pdf", "customer_pdf", "journal_pdf", "catalog_preview"}
OBJECT_ACTIONS = {"customer_pdf", "journal_pdf", "catalog_preview", "invoice_period"}


def capabilities():
    return [dict(action=key, title=title, mode=mode,
                 notice="Открывает штатную форму; ничего не сохраняет автоматически."
                 if mode in {"native_dialog", "native_section"} else
                 "Изменения только в штатном предпросмотре с подтверждением."
                 if key == "catalog_preview" else "Только чтение.")
            for key, (title, mode) in CAPABILITIES.items()]
