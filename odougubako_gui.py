"""おどうぐばこ (odougubako) の専用ウィンドウ。

登録された道具の一覧と取説を見て、前版へ戻す・削除・
フォルダを開く、といった人の操作を行うための窓。
AI との連携は持たない (窓口は PHAL)。単体でも起動でき、
PHAL のボタンからも開かれる。
"""

import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

import odougubako_core as core

COLUMNS = ["名前", "分類", "説明", "版", "使用", "成功率", "評価",
           "最終使用", "印"]
EVAL_LABELS = {"usability": "使いやすさ", "findability": "選びやすさ",
               "expectation": "期待どおりか"}
IMPROVE_THRESHOLD = 3.0


def _eval_text(tool) -> str:
    averages = tool["eval"]["averages"]
    if not averages:
        return ""
    return " ".join(f"{EVAL_LABELS.get(k, k)} {v:g}"
                    for k, v in averages.items())


def _marks(tool) -> str:
    marks = []
    if tool["dormant"]:
        marks.append("休眠")
    averages = tool["eval"]["averages"]
    if averages and min(averages.values()) < IMPROVE_THRESHOLD:
        marks.append("改善候補")
    return " ".join(marks)


class OdougubakoWindow(QMainWindow):
    def __init__(self, root=None):
        super().__init__()
        self._root = Path(root) if root else core.root_dir()
        self._tools = []
        self.setWindowTitle("おどうぐばこ")
        self.resize(980, 560)

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        self._summary = QLabel()
        layout.addWidget(self._summary)

        self._table = QTableWidget(0, len(COLUMNS))
        self._table.setHorizontalHeaderLabels(COLUMNS)
        self._table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers)
        self._table.itemSelectionChanged.connect(self._on_select)
        layout.addWidget(self._table, 3)

        self._detail = QPlainTextEdit()
        self._detail.setReadOnly(True)
        layout.addWidget(self._detail, 2)

        buttons = QHBoxLayout()
        self._rollback_button = QPushButton("前版へ戻す")
        self._rollback_button.clicked.connect(self._on_rollback)
        buttons.addWidget(self._rollback_button)
        self._remove_button = QPushButton("削除")
        self._remove_button.clicked.connect(self._on_remove)
        buttons.addWidget(self._remove_button)
        self._open_button = QPushButton("フォルダを開く")
        self._open_button.clicked.connect(self._on_open_folder)
        buttons.addWidget(self._open_button)
        buttons.addStretch(1)
        refresh = QPushButton("更新")
        refresh.clicked.connect(self.refresh)
        buttons.addWidget(refresh)
        layout.addLayout(buttons)

        self.refresh()

    def refresh(self) -> None:
        self._tools = core.list_tools(self._root)
        self._table.setRowCount(len(self._tools))
        for row, tool in enumerate(self._tools):
            rate = tool["success_rate"]
            values = [
                tool["name"],
                tool["category"],
                tool["description"],
                str(tool["version"]),
                str(tool["count"]),
                f"{rate:.0%}" if rate is not None else "-",
                _eval_text(tool),
                (tool["last_used_at"] or "")[:10],
                _marks(tool),
            ]
            for column, value in enumerate(values):
                self._table.setItem(row, column, QTableWidgetItem(value))
        self._table.resizeColumnsToContents()
        self._summary.setText(
            f"道具 {len(self._tools)} 件 ・ 置き場 {self._root}")
        self._detail.clear()
        self._update_buttons()

    def _selected_tool(self):
        row = self._table.currentRow()
        if 0 <= row < len(self._tools):
            return self._tools[row]
        return None

    def _update_buttons(self) -> None:
        tool = self._selected_tool()
        self._rollback_button.setEnabled(bool(tool and tool["has_prev"]))
        self._remove_button.setEnabled(bool(tool))
        self._open_button.setEnabled(bool(tool))

    def _on_select(self) -> None:
        tool = self._selected_tool()
        self._update_buttons()
        if tool is None:
            self._detail.clear()
            return
        skill_path = Path(tool["folder"]) / core.SKILL_FILENAME
        try:
            text = skill_path.read_text(encoding="utf-8")
        except OSError:
            text = "(取説が読めません)"
        self._detail.setPlainText(text)

    def _on_rollback(self) -> None:
        tool = self._selected_tool()
        if tool is None:
            return
        answer = QMessageBox.question(
            self, "前版へ戻す",
            f"{tool['name']} を前版 (v{tool['version'] - 1}) へ戻しますか。\n"
            "今の版は前版として残るので、もう一度押すと戻れます。")
        if answer != QMessageBox.StandardButton.Yes:
            return
        result = core.rollback(tool["name"], self._root)
        if not result["ok"]:
            QMessageBox.warning(self, "戻せませんでした", result["reason"])
        self.refresh()

    def _on_remove(self) -> None:
        tool = self._selected_tool()
        if tool is None:
            return
        answer = QMessageBox.question(
            self, "削除",
            f"{tool['name']} をおどうぐばこから削除しますか。\n"
            "道具のフォルダと使用履歴が消えます。")
        if answer != QMessageBox.StandardButton.Yes:
            return
        result = core.remove_tool(tool["name"], self._root)
        if not result["ok"]:
            QMessageBox.warning(self, "削除できませんでした",
                                result["reason"])
        self.refresh()

    def _on_open_folder(self) -> None:
        tool = self._selected_tool()
        if tool is not None:
            QDesktopServices.openUrl(
                QUrl.fromLocalFile(tool["folder"]))


def main(argv=None) -> int:
    app = QApplication(list(argv) if argv else sys.argv)
    window = OdougubakoWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
