"""Shared identifiers for built-in time entry categories."""

UNPAID_LEAVE = "unpaid_leave"
UNPAID_LEAVE_NAME = "Obetald ledighet"
UNPAID_LEAVE_LABEL_RU = "Отпуск за свой счёт"

SERVICE_OBJECTS = {
    "atf": ("Arbetstidsförkortning", "p1000"),
    "sick": ("Sjuk", "p1001"),
    "vacation": ("Semestertimmar", "p1002"),
    "holiday": (None, "p1004"),
    UNPAID_LEAVE: (UNPAID_LEAVE_NAME, "p1003"),
}
SERVICE_TYPE_MARKINGS = {kind: marking for kind, (_, marking) in SERVICE_OBJECTS.items()}
ABSENCE_TYPES = tuple(SERVICE_OBJECTS)
