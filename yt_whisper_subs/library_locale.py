"""Apply the interface locale to Qt's native controls at application startup.

Example: `install(app, "fr")` also translates Save/Cancel and calendar navigation.
"""

from PySide6 import QtCore

from yt_whisper_subs import i18n


def install(app: QtCore.QCoreApplication, code: str) -> None:
    """Keep the Qt translator alive for the event loop; e.g. a French file dialog uses French buttons."""

    i18n.configure(code)
    QtCore.QLocale.setDefault(QtCore.QLocale(i18n.language()))
    previous = app.findChild(QtCore.QTranslator, "uiQtTranslator")
    if previous is not None:
        app.removeTranslator(previous)
        previous.deleteLater()
    translator = QtCore.QTranslator(app)
    translator.setObjectName("uiQtTranslator")
    directory = QtCore.QLibraryInfo.path(QtCore.QLibraryInfo.LibraryPath.TranslationsPath)
    if translator.load(f"qtbase_{i18n.language()}", directory):
        app.installTranslator(translator)
