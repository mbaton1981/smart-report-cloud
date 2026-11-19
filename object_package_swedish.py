"""Swedish labels for the object report; source documents remain untouched."""

import re


_LABELS = {
    "Полный отчёт по объекту": "Fullständig objektrapport",
    "Заказчик": "Beställare",
    "Объект": "Objekt",
    "Период": "Period",
    "Сформировано": "Upprättad",
    "Архивный объект": "Arkiverat objekt",
    "Объект активен": "Aktivt objekt",
    "Пакет предназначен для проверки выполненных работ и подготовки расчёта. "
    "Он не является финальной фактурой и не фиксирует оплату или прибыль; "
    "формирование не меняет записи учёта.": (
        "Underlaget är avsett för kontroll av utfört arbete och förberedelse av en "
        "kundberäkning. Det är inte en slutlig faktura och visar inte betalning eller vinst."
    ),
    "Пакет неполный": "Underlaget är ofullständigt",
    "Показатель": "Mått",
    "Значение": "Värde",
    "Рабочие часы, включая OB один раз": "Arbetade timmar, inklusive OB utan dubbelräkning",
    "Обычные часы": "Ordinarie timmar",
    "OB-часы (часть общего количества)": "OB-timmar (ingår i totalt antal timmar)",
    "Сотрудники": "Medarbetare",
    "Поездки": "Resor",
    "Сводка работ": "Sammanställning av arbetet",
    "Сотрудник": "Medarbetare",
    "Всего часов": "Totalt antal timmar",
    "Обычные": "Ordinarie",
    "Итоги по сотрудникам": "Summa per medarbetare",
    "Расчёт клиенту без НДС": "Kundberäkning exklusive moms",
    "Позиция из сохранённых клиентских условий": "Post enligt sparade kundvillkor",
    "Количество": "Antal",
    "Ставка": "Pris",
    "Сумма": "Belopp",
    "Работа": "Arbete",
    "Нет записей для расчёта": "Inga poster för beräkning",
    "Расчётная сумма": "Beräknat belopp",
    "Известная часть расчёта; итог неполон": "Känd del av beräkningen; totalsumman är ofullständig",
    "Нет рабочих записей для клиентского расчёта; сумма не определялась.": (
        "Det finns inga arbetsposter för kundberäkningen; inget belopp har fastställts."
    ),
    "Это расчёт по сохранённым клиентским условиям, не выставленный счёт, оплата или прибыль.": (
        "Beräkningen bygger på sparade kundvillkor och är inte en faktura, betalning eller vinst."
    ),
    "Примечание": "Anmärkning",
    "Подробные рабочие записи": "Detaljerade arbetsposter",
    "Включены записи типов work и OB вне зависимости от статуса; статус каждой записи показан "
    "в таблице. Часы отпусков, болезни и других нерабочих категорий исключены. "
    "Поездки указаны отдельно от часов.": (
        "Arbetsposter och OB-poster ingår oavsett status. Status visas för varje post. "
        "Semester, sjukfrånvaro och annan frånvaro ingår inte. Resor anges separat från arbetstiden."
    ),
    "Дата": "Datum",
    "Описание работ": "Arbetsbeskrivning",
    "Часы": "Timmar",
    "Работа / OB": "Arbete / OB",
    "Статус": "Status",
    "Нет рабочих записей": "Inga arbetsposter",
    "Реестр подтверждённых расходов": "Förteckning över bekräftade kostnader",
    "Приложение": "Bilaga",
    "Поставщик / эмитент": "Leverantör / utfärdare",
    "Тип / № / дата": "Typ / nr / datum",
    "Нетто": "Exklusive moms",
    "НДС": "Moms",
    "без НДС": "exkl. moms",
    "Сумма доли": "Objektets andel",
    "Округление": "Avrundning",
    "Валюта": "Valuta",
    "Неизвестно": "Okänt",
    "Не указан": "Ej angiven",
    "Неизвестный поставщик": "Okänd leverantör",
    "Дата неизвестна": "Datum saknas",
    "Не сохранено": "Ej sparat",
    "сумма": "belopp",
    "известная часть; неполно": "känd del; ofullständigt",
    "известная часть; итог неполон": "känd del; totalsumman är ofullständig",
    "Итого долей": "Summa andelar",
    "нетто": "exklusive moms",
    "Разные валюты не складываются.": "Olika valutor summeras inte.",
    "Подтверждённых распределений на этот объект в выбранном диапазоне не найдено.": (
        "Inga bekräftade kostnadsfördelningar på objektet hittades under perioden."
    ),
    "Расходы неизвестны: подтверждённый снимок реестра недоступен.": (
        "Kostnaderna är okända eftersom ett bekräftat registerutdrag inte är tillgängligt."
    ),
    "Документы, не включённые в сумму расходов": "Dokument som inte ingår i kostnadssumman",
    "Связанные с объектом документы перечислены отдельно; их распределения не включены "
    "в подтверждённый итог.": (
        "Dokument kopplade till objektet visas separat och ingår inte i den bekräftade summan."
    ),
    "Статус / причина": "Status / orsak",
    "Не подтверждён": "Inte bekräftat",
    "Оглавление приложений": "Bilageförteckning",
    "Поставщик / документ": "Leverantör / dokument",
    "Доля объекта": "Objektets andel",
    "Страница": "Sida",
    "Нет подтверждённых документов для приложения": "Inga bekräftade dokument att bifoga",
    "Каждое приложение начинается с отдельного листа; оригинальные страницы после него "
    "сохранены без добавления отметок.": (
        "Varje bilaga inleds med ett försättsblad. Originalens sidor återges utan ändringar."
    ),
    "Копии документов не включены по выбору пользователя.": "Dokumentkopior ingår inte.",
    "Обычная работа": "Ordinarie arbete",
    "OB; детализация не подтверждена": "OB; fördelningen är inte bekräftad",
    "Отмечено «в счёте»": "Markerad som fakturerad",
    "Часы подтверждены": "Timmarna är bekräftade",
    "В работе": "Ej bekräftad",
    "Дата неизвестна; невозможно проверить принадлежность выбранному периоду.": (
        "Datum saknas; det går inte att kontrollera om dokumentet tillhör perioden."
    ),
    "Есть конфликт синхронизации; локальная версия не включена.": (
        "Synkroniseringskonflikt; den lokala versionen ingår inte."
    ),
    "Изменение не подтверждено сервером; документ не включён.": (
        "Ändringen är inte bekräftad av servern; dokumentet ingår inte."
    ),
    "Документ требует проверки или ещё не подтверждён.": (
        "Dokumentet behöver granskas eller är ännu inte bekräftat."
    ),
    "Распределение не подтверждено.": "Fördelningen är inte bekräftad.",
    "Тип": "Typ",
    "Номер": "Nummer",
    "не указан": "ej angiven",
    "неизвестен": "okänd",
    "неизвестна": "okänt",
    "Округление доли": "Andelens avrundning",
    "Исходный документ распределён также на другие объекты или общие расходы фирмы. "
    "Указана только подтверждённая доля выбранного объекта, не полная сумма исходного документа.": (
        "Originaldokumentet är även fördelat på andra objekt eller företagets gemensamma "
        "kostnader. Endast objektets bekräftade andel visas, inte dokumentets hela belopp."
    ),
    "Копия отсутствует или неполна": "Kopian saknas eller är ofullständig",
    "Оригинал приложен целиком": "Originalet är bifogat i sin helhet",
    "Содержимое оригинала не изменено.": "Originalets innehåll har inte ändrats.",
    "стр.": "sidor",
    "за весь объект; фактический диапазон данных": "hela objektet; faktiskt dataintervall",
    "за весь объект; датированные записи отсутствуют": "hela objektet; daterade poster saknas",
    "Нет данных": "Uppgifter saknas",
    "Прочие расходы": "Övriga kostnader",
    "Материалы": "Material",
    "Материалы и закупки": "Material och inköp",
    "Работы подрядчиков": "Underentreprenörers arbete",
    "Аренда оборудования": "Hyra av maskiner och utrustning",
    "Работа и материалы подрядчика, без разбивки": (
        "Underentreprenör – arbete och material, ej specificerat"
    ),
    "Не классифицировано": "Ej kategoriserat",
    "Услуги": "Tjänster",
    "Аренда": "Hyra",
    "Транспорт": "Transport",
    "Инструменты": "Verktyg",
    "Фактура": "Faktura",
    "Чек": "Kvitto",
    "Использован последний локальный снимок расходов; он может быть устаревшим.": (
        "Det senaste lokala kostnadsutdraget används och kan vara inaktuellt."
    ),
    "Локальный снимок достиг лимита 1000 документов; полнота не подтверждена.": (
        "Det lokala utdraget har nått gränsen på 1 000 dokument; fullständigheten är inte bekräftad."
    ),
    "Распределение обычных и OB-часов не полностью подтверждено.": (
        "Fördelningen mellan ordinarie tid och OB-tid är inte helt bekräftad."
    ),
    "Число поездок в части рабочих записей неизвестно.": "Antalet resor saknas för vissa arbetsposter.",
    "Число поездок неизвестно; расчёт клиенту неполный.": (
        "Antalet resor är okänt; kundberäkningen är ofullständig."
    ),
    "Поездки - количество неизвестно": "Resor – antal saknas",
    "Расчёт клиенту неполный: есть неизвестные цены или доплаты.": (
        "Kundberäkningen är ofullständig: priser eller tillägg saknas."
    ),
    "У части подтверждённых распределений неизвестна сумма доли объекта.": (
        "Objektets andel saknas för vissa bekräftade fördelningar."
    ),
}

_ORDERED_LABELS = sorted(_LABELS.items(), key=lambda item: len(item[0]), reverse=True)


def swedish_label(value):
    """Translate report-generated text, retaining numbers and proper names."""
    text = str(value)
    match = re.fullmatch(
        r"Не включено в расходы документов, требующих проверки или подтверждения: (\d+)\.",
        text,
    )
    if match:
        return f"Dokument som behöver granskas eller bekräftas ingår inte i kostnaderna: {match.group(1)}."
    match = re.fullmatch(
        r"В выборке есть несинхронизированные или конфликтующие смены: (\d+); состояния: (.+)\.",
        text,
    )
    if match:
        return (
            f"Underlaget innehåller {match.group(1)} osynkroniserade eller "
            f"konfliktmarkerade arbetsposter. Registrerad status: {match.group(2)}."
        )
    if text in _LABELS:
        return _LABELS[text]
    for source, target in _ORDERED_LABELS:
        text = text.replace(source, target)
    text = re.sub(r"(?<=\d) ч\b", " tim", text)
    text = text.replace(" (+1 день)", " (+1 dag)")
    return text


def swedish_document_type(value):
    return {
        "invoice": "Faktura",
        "receipt": "Kvitto",
        "credit_note": "Kreditfaktura",
        "tax_decision": "Avgiftsbeslut",
        "manual": "Manuellt registrerat dokument",
        "": "Dokumenttyp ej angiven",
    }.get(str(value or "").strip(), "Dokumenttyp ej angiven")


def swedish_category(value):
    text = str(value or "")
    return _LABELS.get(text, text)
