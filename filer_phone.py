# filer_phone.py
# あっふ の「スマホへ送る（QR）」の画面側(PySide6)。サーバ・ローカル CA・
# ページの中身は共通モジュール photometa/phone.py にあり、PhotoCuller と
# 共用する(CA の置き場所 phone_ca も共通。iPhone に入れた証明書がそのまま
# 使えるようにするため)。ここには QR の描画とダイアログだけを置く。
#
# 有効なのは「環境1」の PC だけ(settings.json の environment == 1。
# setup.py の環境選択で決める)。あっふ 側は filer.py が environment を見て、
# 有効なときだけ複写メニューの項目を出す。既定は無効で、項目も出ない。
from photometa.phone import (  # noqa: F401  テスト・呼び出し側は filer_phone 経由で使う
    AUTO_CLOSE_SECONDS, CA_CERT_FILE, CA_DIR, CA_KEY_FILE, IMAGE_EXTS,
    NAME_CONSTRAINTS, PERMITTED_DNS, PERMITTED_NETWORKS, SERVER_VALID_DAYS,
    SHARE_ALL_MAX, CA_VALID_DAYS, SHARE_CHUNK, SHARE_TYPES, ShareServer,
    ca_cert_der, default_ca_dir, ensure_ca, ip_permitted, is_image,
    issue_server_cert, lan_ip, qr_matrix, server_ssl_context, share_type)


# ===============================================================
# QR とダイアログ(PySide6 はここだけ、遅延 import)
# ===============================================================
def qr_pixmap(text: str, target: int = 264):
    """URL の QR を QPixmap で返す。描き方は diagnostics.qr_to_qimage と同じ
    (行列を白地に黒で塗る)。行列は共通側の photometa.phone.qr_matrix が作る。"""
    from PySide6.QtCore import QRect
    from PySide6.QtGui import QColor, QImage, QPainter, QPixmap

    matrix = qr_matrix(text)
    cells = len(matrix)
    border = 4
    scale = max(1, target // (cells + border * 2))
    size = (cells + border * 2) * scale
    image = QImage(size, size, QImage.Format_RGB32)
    image.fill(QColor("#ffffff"))
    painter = QPainter(image)
    try:
        black = QColor("#000000")
        for y, row in enumerate(matrix):
            for x, bit in enumerate(row):
                if bit:
                    painter.fillRect(
                        QRect((x + border) * scale, (y + border) * scale,
                              scale, scale), black)
    finally:
        painter.end()
    return QPixmap.fromImage(image)


def show_share_dialog(parent, paths) -> int:
    """QR ダイアログをモーダルで開く。開いている間だけサーバが動く。

    戻り値はダウンロードされた件数(呼び出し側のログ用)。"""
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtWidgets import (QDialog, QLabel, QPushButton, QVBoxLayout,
                                   QWidget)

    server = ShareServer(paths)
    server.start()
    url = server.url
    downloads = 0
    try:
        dialog = QDialog(parent)
        dialog.setWindowTitle("スマホへ送る")
        layout = QVBoxLayout(dialog)
        intro = QLabel(f"{len(server.files)}件のファイルを送れます。\n"
                       "スマホのカメラで QR コードを読んでください"
                       "（同じ Wi-Fi にいる必要があります）。")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        qr_label = QLabel()
        qr_label.setAlignment(Qt.AlignCenter)
        qr_label.setPixmap(qr_pixmap(url))
        layout.addWidget(qr_label)
        url_label = QLabel(url)
        url_label.setWordWrap(True)
        url_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(url_label)
        if server.ip.startswith("127."):
            warn = QLabel("⚠ LAN につながっていないようです"
                          "（スマホから届かないアドレスになっています）。")
            warn.setWordWrap(True)
            layout.addWidget(warn)

        if server.ca_url:
            # 証明書の登録用(最初の1回だけ)。初めて CA を作ったときは開いておく。
            ca_toggle = QPushButton("証明書の登録用 QR（iPhone で最初の1回だけ）")
            ca_toggle.setCheckable(True)
            layout.addWidget(ca_toggle)
            ca_box = QWidget()
            ca_layout = QVBoxLayout(ca_box)
            ca_layout.setContentsMargins(0, 0, 0, 0)
            ca_note = QLabel("これを iPhone で読み、出てきたページの手順で証明書を"
                             "登録すると、上の QR のページで「まとめて写真に保存」"
                             "が使えます。")
            ca_note.setWordWrap(True)
            ca_layout.addWidget(ca_note)
            ca_qr = QLabel()
            ca_qr.setAlignment(Qt.AlignCenter)
            ca_qr.setPixmap(qr_pixmap(server.ca_url, target=180))
            ca_layout.addWidget(ca_qr)
            ca_url_label = QLabel(server.ca_url)
            ca_url_label.setWordWrap(True)
            ca_url_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            ca_layout.addWidget(ca_url_label)
            layout.addWidget(ca_box)
            ca_toggle.toggled.connect(ca_box.setVisible)
            ca_toggle.setChecked(server.ca_created)
            ca_box.setVisible(server.ca_created)
        else:
            reason = server.https_error or "HTTPS を使わない設定です"
            fallback = QLabel(f"HTTP で出しています（{reason}）。"
                              "まとめて保存は使えません。画像は長押しで保存できます。")
            fallback.setWordWrap(True)
            fallback.setStyleSheet("color: gray; font-size: 11px;")
            layout.addWidget(fallback)

        status = QLabel("まだ受け取られていません。"
                        f"この画面は {AUTO_CLOSE_SECONDS // 60} 分で自動的に閉じます。")
        status.setWordWrap(True)
        layout.addWidget(status)
        close_button = QPushButton("閉じる")
        close_button.clicked.connect(dialog.accept)
        layout.addWidget(close_button)

        def poll():
            nonlocal downloads
            for kind, name in server.pop_events():
                if kind == "download":
                    downloads += 1
                    status.setText(f"受け取りました: {name}（これまで {downloads} 件）")
                elif kind == "ca":
                    status.setText("証明書を渡しました。iPhone の設定でインストールし、"
                                   "証明書信頼設定でオンにしてください。")

        timer = QTimer(dialog)
        timer.timeout.connect(poll)
        timer.start(500)
        QTimer.singleShot(AUTO_CLOSE_SECONDS * 1000, dialog.accept)
        dialog.exec()
    finally:
        server.stop()
    return downloads
