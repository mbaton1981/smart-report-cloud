from PyQt6 import sip
from PyQt6.QtCore import QEvent


def snapshot_top_level_widgets(application):
    return {
        sip.unwrapinstance(widget)
        for widget in application.topLevelWidgets()
        if not sip.isdeleted(widget)
    }


def cleanup_new_top_level_widgets(application, before):
    new_widgets = [
        widget for widget in application.topLevelWidgets()
        if not sip.isdeleted(widget) and sip.unwrapinstance(widget) not in before
    ]
    new_handles = {sip.unwrapinstance(widget) for widget in new_widgets}
    for widget in new_widgets:
        if sip.isdeleted(widget):
            continue
        parent = widget.parentWidget()
        if parent is not None and not sip.isdeleted(parent) \
                and sip.unwrapinstance(parent) in new_handles:
            continue
        if widget.isVisible():
            widget.close()
        if not sip.isdeleted(widget):
            widget.deleteLater()
    application.sendPostedEvents(None, QEvent.Type.DeferredDelete)
