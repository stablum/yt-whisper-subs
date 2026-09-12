"""Central stylesheet for the native dark library interface.

Example: `window.setStyleSheet(library_theme.STYLE)`.
"""

STYLE = """
QMainWindow, QDialog { background: #171a21; color: #eef2f7; }
QWidget { color: #eef2f7; font-family: "Segoe UI"; font-size: 10pt; }
QFrame#sidebar { background: #11141a; border-right: 1px solid #2a303b; }
QLabel#appTitle { font-size: 18pt; font-weight: 700; color: #ffffff; }
QLabel#appSubtitle, QLabel#statLabel, QLabel#settingsNote { color: #8f9aaa; }
QListWidget { background: transparent; border: 0; outline: 0; padding: 4px; }
QListWidget::item { padding: 9px 10px; margin: 2px 0; border-radius: 6px; }
QListWidget::item:selected { background: #26364d; color: #8fc7ff; }
QLineEdit, QComboBox, QDoubleSpinBox {
  background: #222730; border: 1px solid #343b47; border-radius: 7px; padding: 7px 9px;
}
QLineEdit:focus, QComboBox:focus, QDoubleSpinBox:focus { border-color: #4ea1f3; }
QPushButton { background: #2b313c; border: 1px solid #3a424f; border-radius: 7px; padding: 7px 13px; }
QPushButton:hover { background: #353d49; }
QPushButton:disabled { color: #697381; background: #22262d; }
QPushButton#primaryButton { background: #2474c6; border-color: #328be2; color: white; font-weight: 600; }
QPushButton#primaryButton:hover { background: #2b83db; }
QPushButton#primaryButton:disabled { color: #697381; background: #22262d; border-color: #343b47; }
QPushButton#dangerButton { background: #49292d; border-color: #87434b; color: #ffd9dc; font-weight: 600; }
QPushButton#dangerButton:hover { background: #633238; border-color: #b65661; color: white; }
QPushButton#dangerButton:disabled { color: #8b676a; background: #2d2427; border-color: #493136; }
QFrame#filterBar { background: #1e232c; border: 1px solid #303844; border-radius: 9px; }
QLabel#filterEyebrow { color: #778292; font-size: 8pt; font-weight: 700; padding: 0 5px 0 2px; }
QLabel#filterResult { color: #8f9aaa; padding: 0 3px 0 8px; }
QPushButton#filterChip {
  background: #272d37; border: 1px solid #353e4b; border-radius: 13px;
  color: #aeb8c6; padding: 5px 10px;
}
QPushButton#filterChip:hover { background: #303946; border-color: #485567; color: #e6edf5; }
QPushButton#filterChip:checked {
  background: #254f78; border-color: #4ea1f3; color: #ffffff; font-weight: 600;
}
QPushButton#filterChip:disabled { background: #20252d; border-color: #2b313b; color: #5f6875; }
QPushButton#clearFilters { background: transparent; border: 0; color: #8fc7ff; padding: 5px 7px; }
QPushButton#clearFilters:hover { background: #293440; color: white; }
QTableView { background: #1c2028; alternate-background-color: #191d24; border: 1px solid #2d3440; border-radius: 8px; gridline-color: transparent; }
QTableView::item { padding: 7px; border-bottom: 1px solid #262c35; }
QTableView::item:selected { background: #294467; color: white; }
QHeaderView::section { background: #222730; color: #aeb8c6; border: 0; border-bottom: 1px solid #363e4a; padding: 8px; font-weight: 600; }
QFrame#detailPanel { background: #20252e; border: 1px solid #2d3440; border-radius: 9px; }
QFrame#chapterPanel { background: #1b2028; border-left: 1px solid #303844; }
QLabel#detailTitle, QLabel#dialogHeading { font-size: 14pt; font-weight: 700; color: white; }
QLabel#detailFacts { color: #8fc7ff; }
QTextEdit#detailDescription {
  background: transparent; color: #b4bdc9; border: 0; padding: 0;
  selection-background-color: #294467;
}
QTextEdit#detailDescription QScrollBar:vertical {
  background: #1b2028; width: 9px; margin: 0;
}
QTextEdit#detailDescription QScrollBar::handle:vertical {
  background: #485567; border-radius: 4px; min-height: 28px;
}
QTextEdit#detailDescription QScrollBar::handle:vertical:hover { background: #5e7188; }
QTextEdit#detailDescription QScrollBar::add-line:vertical,
QTextEdit#detailDescription QScrollBar::sub-line:vertical { height: 0; }
QTextEdit#detailDescription QScrollBar::add-page:vertical,
QTextEdit#detailDescription QScrollBar::sub-page:vertical { background: transparent; }
QSplitter#catalogSplitter::handle {
  background: #171a21; border-top: 1px solid #303844; border-bottom: 1px solid #303844;
}
QSplitter#catalogSplitter::handle:hover { background: #26364d; border-color: #4ea1f3; }
QLabel#chapterHeading { color: #f2f5f9; font-size: 10pt; font-weight: 700; }
QLabel#chapterHint { color: #8f9aaa; }
QPushButton#chapterGenerate { color: #f5bde0; background: transparent; border: 0; padding: 4px 7px; }
QPushButton#chapterGenerate:hover { background: #352a3b; }
QListWidget#chapterList { background: transparent; border: 0; padding: 0; }
QListWidget#chapterList::item { padding: 7px 9px; margin: 1px 0; border-radius: 5px; }
QListWidget#chapterList::item:hover { background: #29313c; }
QListWidget#chapterList::item:selected { background: #3b3150; color: white; }
QDockWidget { color: #d8dee8; background: #151920; }
QDockWidget::title { background: #20252e; padding: 7px 10px; border-top: 1px solid #303744; }
QLabel#traceNote { color: #8f9aaa; }
QPlainTextEdit#activityTrace {
  background: #0f1217; color: #c9d2df; border: 1px solid #303744;
  selection-background-color: #294467; font-family: Consolas; font-size: 9pt;
}
QStatusBar { background: #11141a; color: #9ba6b4; border-top: 1px solid #2a303b; }
QMenu { background: #222730; border: 1px solid #3a424f; padding: 5px; }
QMenu::item { padding: 7px 26px; border-radius: 4px; }
QMenu::item:selected { background: #294467; }
QToolTip { background: #2b313c; color: white; border: 1px solid #4b5564; }
"""
