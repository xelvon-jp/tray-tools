# photometa/phone.py
# スマホへ送る(PC→スマホの一方通行、QR コード経由)の共通部分。Qt に依存しない。
# あっふ(PySide6、filer_phone.py)と PhotoCuller(PyQt6)の両方から使う。
# 画面(QR の描画とダイアログ)はそれぞれのアプリ側に置く。
#
# 仕掛け: ダイアログを開いている間だけ、この PC で小さな Web サーバを立て、
# 選んだファイル専用の URL(推測できない合言葉=トークン入り)を QR コードで
# 見せる。スマホのカメラで読めば、同じ Wi-Fi の中だけでダウンロードできる。
# インターネットは通らない。ダイアログを閉じればサーバは止まり、URL は
# 無効になる。渡せるのは選んだファイルだけ(PC の中を覗く口は無い)。
#
# 画像はダウンロード用ページにそのまま表示する(iPhone では長押しで
# 「写真に追加」できる)。ダウンロード用のリンクも併記する。
#
# HTTPS とまとめて保存:
#   iPhone の Safari は Web Share API(navigator.share({files}))で、共有シートから
#   「n 枚の画像を保存」や他のアプリ(みてね など)へまとめて渡せる。ただしこれは
#   安全なコンテキスト(HTTPS)でしか使えない。そこで、この PC 専用の小さな
#   認証局(ローカル CA)を作り、サーバを立てるたびにいまの LAN の IP 入りの
#   サーバ証明書を発行して HTTPS で出す。CA 証明書は、ユーザーが iPhone に
#   一度だけ登録して信頼する(登録用の入口はダイアログの2つ目の QR。HTTP)。
#   cryptography が無い・証明書が作れないなどのときは、これまでどおり HTTP で出す。
#
#   CA の鍵は %LOCALAPPDATA%\traytools\phone_ca\ に置く(リポジトリの外。本人だけが
#   読めるよう ACL を絞る。あっふ と PhotoCuller で共通。iPhone に登録した
#   証明書がそのまま使えるようにするため)。この鍵が漏れると、信頼した iPhone に
#   対して LAN の中の偽サイトを作れてしまうので、コピーしたり人に渡したりしない
#   こと。ログにも出さない。作り直したいときはフォルダごと消す(次に開いたときに
#   作り直す。iPhone 側も入れ直し)。
import html
import ipaddress
import json
import mimetypes
import os
import secrets
import socket
import ssl
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}
AUTO_CLOSE_SECONDS = 15 * 60   # 開きっぱなしの保険(ダイアログが自動で閉じる)

# 共有シート(まとめて保存)に渡す種類。拡張子 → MIME。Windows の mimetypes は
# HEIC などを知らないことがあるので、ここで決め打ちにする。BMP は iPhone の
# 写真に入らないので外す(ページに表示はする)。
SHARE_TYPES = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".webp": "image/webp",
    ".heic": "image/heic", ".heif": "image/heif",
    ".mp4": "video/mp4", ".m4v": "video/x-m4v", ".mov": "video/quicktime",
}
# 共有は何件ずつに分けるか。iOS は件数や合計サイズが大きいと共有に失敗したり、
# Safari のメモリが足りなくなったりする(全部を Blob にしてから渡すため)。
SHARE_CHUNK = 10
# これ以下なら「全部まとめて」のボタンも出す。超えたら分けたボタンだけにする。
SHARE_ALL_MAX = 30

# --- ローカル CA ---
# 置き場所。None なら %LOCALAPPDATA%\traytools\phone_ca。テストでは一時フォルダへ
# 差し替える(ShareServer の ca_dir 引数でも渡せる)。
CA_DIR = None
CA_KEY_FILE = "ca_key.pem"     # 秘密鍵。外に出さない
CA_CERT_FILE = "ca_cert.pem"   # 公開部分。iPhone に入れるのはこちらだけ
CA_VALID_DAYS = 3650           # CA は長め(作り直すと iPhone 側も入れ直しになる)
# サーバ証明書はサーバを立てるたびに発行するので短くてよい。iOS の上限(825日)より
# ずっと短くしておく。PC と iPhone の時計のずれに備えて、開始は1日前にする。
SERVER_VALID_DAYS = 30
# CA に名前制約(Name Constraints、critical)を付けて、LAN の中(プライベート IP と
# .local)にしか使えない CA にする。万一鍵が漏れても、インターネットのサイトの
# 偽証明書には使えない。iOS で信頼設定がうまくいかないときは False にして、
# phone_ca フォルダを消してから作り直す(付くかどうかは作るときに決まる)。
NAME_CONSTRAINTS = True
PERMITTED_NETWORKS = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
# DNS の制約は RFC 5280 の書き方("local" で local とその下すべて)。
PERMITTED_DNS = ("local",)

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def is_image(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in IMAGE_EXTS


def share_type(path: str):
    """共有シートに渡せる種類なら MIME、渡さないなら None。"""
    return SHARE_TYPES.get(os.path.splitext(path)[1].lower())


def lan_ip() -> str:
    """この PC の LAN 側 IP。UDP の宛先を決めるだけで実トラフィックは出ない。
    取れなければ 127.0.0.1(スマホからは届かないので、画面で注意を出す)。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        pass
    finally:
        sock.close()
    try:
        return socket.gethostbyname(socket.gethostname())
    except OSError:
        return "127.0.0.1"


def _size_text(n: int) -> str:
    if n < 1024:
        return f"{n} bytes"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    if n < 1024 * 1024 * 1024:
        return f"{n / 1024 / 1024:.1f} MB"
    return f"{n / 1024 / 1024 / 1024:.2f} GB"


# ===============================================================
# ローカル CA とサーバ証明書(cryptography はここだけ、遅延 import)
# ===============================================================
def default_ca_dir() -> str:
    if CA_DIR:
        return CA_DIR
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "traytools", "phone_ca")


def _restrict_to_me(path: str, is_dir: bool = False) -> bool:
    """path を本人だけが読み書きできるようにする。Windows は ACL の継承を切って
    本人だけにする(先に本人へ付けてから継承を切る。順が逆だと、付けるのに
    失敗したとき誰も触れなくなる)。失敗しても止めない(戻り値で返すだけ)。"""
    if os.name != "nt":
        try:
            os.chmod(path, 0o700 if is_dir else 0o600)
            return True
        except OSError:
            return False
    user = os.environ.get("USERNAME")
    if not user:
        return False
    domain = os.environ.get("USERDOMAIN")
    who = f"{domain}\\{user}" if domain else user
    grant = f"{who}:(OI)(CI)F" if is_dir else f"{who}:F"
    try:
        for args in (["/grant:r", grant], ["/inheritance:r"]):
            done = subprocess.run(["icacls", path, *args], capture_output=True,
                                  creationflags=CREATE_NO_WINDOW, timeout=30,
                                  check=False)
            if done.returncode != 0:
                return False
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def _write_private(path: str, data: bytes) -> None:
    """鍵などを書く。一時名で書いて ACL を絞ってから本来の名前にする
    (絞る前の中身が、緩い権限で置かれている時間を作らない)。"""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp_")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        _restrict_to_me(tmp)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _now():
    import datetime
    return datetime.datetime.now(datetime.timezone.utc)


def _load_ca(directory: str):
    """(証明書, 鍵) を読む。無い・壊れている・期限が近い・組が合わないなら None。"""
    import datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    key_path = os.path.join(directory, CA_KEY_FILE)
    cert_path = os.path.join(directory, CA_CERT_FILE)
    try:
        with open(cert_path, "rb") as f:
            cert = x509.load_pem_x509_certificate(f.read())
        with open(key_path, "rb") as f:
            key = serialization.load_pem_private_key(f.read(), password=None)
    except (OSError, ValueError, TypeError):
        return None
    pub = serialization.PublicFormat.SubjectPublicKeyInfo
    enc = serialization.Encoding.DER
    if cert.public_key().public_bytes(enc, pub) != key.public_key().public_bytes(enc, pub):
        return None
    # 残りが1か月を切ったら作り直す(サーバ証明書がはみ出さないように)
    if cert.not_valid_after_utc - _now() < datetime.timedelta(days=SERVER_VALID_DAYS + 1):
        return None
    return cert, key


def ensure_ca(directory=None, name_constraints=None):
    """ローカル CA を用意する。あれば読み、無ければ作る。

    戻り値は (証明書, 鍵, 今回作ったか)。鍵は EC P-256(iOS が受け付ける)。
    cryptography が無ければ ImportError をそのまま上げる(呼び側で HTTP に戻す)。"""
    import datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    directory = directory or default_ca_dir()
    if name_constraints is None:
        name_constraints = NAME_CONSTRAINTS
    loaded = _load_ca(directory) if os.path.isdir(directory) else None
    if loaded:
        return loaded[0], loaded[1], False

    os.makedirs(directory, exist_ok=True)
    _restrict_to_me(directory, is_dir=True)
    key = ec.generate_private_key(ec.SECP256R1())
    host = socket.gethostname() or "PC"
    name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, f"あっふ スマホ送信 ローカルCA ({host})"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "tray-tools (local)"),
    ])
    now = _now()
    ski = x509.SubjectKeyIdentifier.from_public_key(key.public_key())
    builder = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=CA_VALID_DAYS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=True, content_commitment=False, key_encipherment=False,
            data_encipherment=False, key_agreement=False, key_cert_sign=True,
            crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
        .add_extension(ski, critical=False)
    )
    if name_constraints:
        permitted = [x509.IPAddress(ipaddress.ip_network(n)) for n in PERMITTED_NETWORKS]
        permitted += [x509.DNSName(d) for d in PERMITTED_DNS]
        builder = builder.add_extension(
            x509.NameConstraints(permitted_subtrees=permitted, excluded_subtrees=None),
            critical=True)
    cert = builder.sign(key, hashes.SHA256())
    # 鍵を先に、証明書を後に書く(証明書があること=作り終えた印)
    _write_private(os.path.join(directory, CA_KEY_FILE), key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    _write_private(os.path.join(directory, CA_CERT_FILE),
                   cert.public_bytes(serialization.Encoding.PEM))
    return cert, key, True


def ip_permitted(ca_cert, ip: str) -> bool:
    """CA の名前制約の範囲に ip が入るか(制約が無ければ常に True)。"""
    from cryptography import x509

    try:
        nc = ca_cert.extensions.get_extension_for_class(x509.NameConstraints).value
    except x509.ExtensionNotFound:
        return True
    nets = [g.value for g in (nc.permitted_subtrees or [])
            if isinstance(g, x509.IPAddress)]
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in nets if net.version == addr.version)


def issue_server_cert(ca_cert, ca_key, ip: str):
    """ip を SAN(iPAddress)に入れたサーバ証明書を発行する。(証明書, 鍵) を返す。
    鍵はメモリ上だけ(ファイルには残さない)。"""
    import datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    now = _now()
    cert = (
        x509.CertificateBuilder()
        # CN に IP をそのまま入れると、OpenSSL などはホスト名とみなして DNS の
        # 名前制約("local")に照らし、範囲外として弾く。CN は名前らしくない文字列にし、
        # IP は SAN だけに入れる(今の検証は SAN しか見ない)。
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,
                                                    "あっふ スマホ送信")]))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(min(now + datetime.timedelta(days=SERVER_VALID_DAYS),
                             ca_cert.not_valid_after_utc))
        .add_extension(x509.SubjectAlternativeName(
            [x509.IPAddress(ipaddress.ip_address(ip))]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=True, content_commitment=False, key_encipherment=False,
            data_encipherment=False, key_agreement=False, key_cert_sign=False,
            crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
                       critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
                       critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(
            ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    return cert, key


def server_ssl_context(cert, key, directory: str) -> ssl.SSLContext:
    """サーバ証明書と鍵から SSLContext を作る。ssl は鍵をファイルからしか読めない
    ので、ACL を絞った CA のフォルダに一時ファイルで書き、読んだらすぐ消す。"""
    from cryptography.hazmat.primitives import serialization

    pem = key.private_bytes(serialization.Encoding.PEM,
                            serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    pem += cert.public_bytes(serialization.Encoding.PEM)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".server_", suffix=".pem")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(pem)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(tmp)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    return context


def ca_cert_der(ca_cert) -> bytes:
    """iPhone に渡す CA 証明書(公開部分だけ、DER)。"""
    from cryptography.hazmat.primitives import serialization
    return ca_cert.public_bytes(serialization.Encoding.DER)


# ===============================================================
# サーバ
# ===============================================================
class _QuietServer(ThreadingHTTPServer):
    """接続が途中で切れた・証明書を信頼されず握手に失敗した、などを
    標準エラーへ書かない(スマホ側の都合でよく起きるので)。"""

    daemon_threads = True

    def handle_error(self, request, client_address):
        pass


class _TLSServer(_QuietServer):
    """受け付けた接続ごとに TLS で包む。握手は待ち受けのスレッドではなく、
    接続ごとのスレッドで行う(握手しない相手で全体が止まらないように)。"""

    ssl_context = None

    def get_request(self):
        sock, addr = self.socket.accept()
        return (self.ssl_context.wrap_socket(
            sock, server_side=True, do_handshake_on_connect=False), addr)


class ShareServer:
    """選んだファイルだけを、合言葉入りの URL で配る小さなサーバ。

    use_https が真なら HTTPS を試し、だめなら HTTP に戻る(https_error に理由)。
    HTTPS のときは、CA 証明書を配る HTTP の入口(別の合言葉)も立てる。

    イベント(表示・ダウンロード・証明書)は self.events に溜まる。画面側が
    タイマーで覗いて表示を更新する(サーバのスレッドから Qt を触らない)。"""

    def __init__(self, paths, use_https: bool = True, ca_dir=None, host_ip=None,
                 app_name: str = "あっふ", edits_provider=None, strip_gps: bool = True):
        self.files = [p for p in paths if os.path.isfile(p)]
        self.app_name = app_name
        # 補正（crs:）のある写真は、当てた JPEG を返すための入口。edits_provider は
        # パス → Edits | None を返す関数（無ければ従来どおり元のファイルを返す）。
        # 描画は photometa.render（品質 92、撮影日時は残す、GPS は strip_gps で消す）。
        self._edits_provider = edits_provider
        self._strip_gps = strip_gps
        self._rendered: dict[int, bytes] = {}
        self.token = secrets.token_urlsafe(18)
        self.ca_token = secrets.token_urlsafe(18)
        self.ip = host_ip or lan_ip()     # 証明書の SAN と URL で同じものを使う
        self.events: list = []
        self._lock = threading.Lock()
        self.https = False
        self.https_error = ""
        self.ca_created = False
        self._ca_der = b""
        self._ca_httpd = None
        context = None
        if use_https:
            try:
                context = self._prepare_https(ca_dir or default_ca_dir())
            except ImportError:
                self.https_error = "cryptography が入っていません"
            except Exception as e:  # noqa: BLE001  何があっても HTTP では出せるように
                self.https_error = f"証明書を用意できませんでした（{type(e).__name__}）"
        if context is not None:
            self._httpd = _TLSServer(("0.0.0.0", 0), self._make_handler())
            self._httpd.ssl_context = context
            self._ca_httpd = _QuietServer(("0.0.0.0", 0), self._make_ca_handler())
            self.https = True
        else:
            self._httpd = _QuietServer(("0.0.0.0", 0), self._make_handler())
        self.port = self._httpd.server_address[1]
        self.ca_port = self._ca_httpd.server_address[1] if self._ca_httpd else None
        self._threads = []

    def _prepare_https(self, directory: str):
        """CA を用意してサーバ証明書を発行し、SSLContext を返す。
        使えない事情があれば https_error に書いて None。"""
        ca_cert, ca_key, created = ensure_ca(directory)
        if not ip_permitted(ca_cert, self.ip):
            self.https_error = f"この IP（{self.ip}）は LAN 用の証明書の範囲外です"
            return None
        cert, key = issue_server_cert(ca_cert, ca_key, self.ip)
        context = server_ssl_context(cert, key, directory)
        self.ca_created = created
        self._ca_der = ca_cert_der(ca_cert)
        return context

    @property
    def url(self) -> str:
        scheme = "https" if self.https else "http"
        return f"{scheme}://{self.ip}:{self.port}/k/{self.token}/"

    @property
    def ca_url(self):
        """証明書の登録用の入口(HTTP)。HTTPS で出していないときは None。"""
        if not self._ca_httpd:
            return None
        return f"http://{self.ip}:{self.ca_port}/k/{self.ca_token}/"

    def start(self) -> None:
        for httpd, name in ((self._httpd, "filer-phone-share"),
                            (self._ca_httpd, "filer-phone-ca")):
            if httpd is None:
                continue
            thread = threading.Thread(target=httpd.serve_forever,
                                      daemon=True, name=name)
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        for httpd in (self._httpd, self._ca_httpd):
            if httpd is None:
                continue
            if self._threads:
                httpd.shutdown()
            httpd.server_close()

    def pop_events(self) -> list:
        with self._lock:
            events, self.events = self.events, []
        return events

    def _rendered_bytes(self, index: int):
        """補正のある写真なら、当てた JPEG のバイト列（作れたら覚える）。
        補正が無い・描画できないときは None（元のファイルを返す）。"""
        if self._edits_provider is None:
            return None
        if index in self._rendered:
            return self._rendered[index]
        try:
            edits = self._edits_provider(self.files[index])
            if edits is None or edits.is_empty():
                return None
            from . import render
            data = render.render_bytes(self.files[index], edits, quality=92,
                                       strip_gps=self._strip_gps)
        except Exception:  # noqa: BLE001  描画できなければ元のファイルを返す
            return None
        self._rendered[index] = data
        return data

    def _note(self, kind: str, name: str) -> None:
        with self._lock:
            self.events.append((kind, name))

    # --- 応答の中身 ---
    def _index_html(self) -> bytes:
        media = [{"i": i, "name": os.path.basename(p), "type": share_type(p)}
                 for i, p in enumerate(self.files) if share_type(p)]
        hint = "画像は長押しすると「写真に追加」できます。"
        parts = [
            "<!DOCTYPE html><html lang=\"ja\"><head><meta charset=\"utf-8\">",
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">",
            "<title>" + self.app_name + " から受け取る</title>",
            "<style>body{font-family:sans-serif;margin:16px;background:#14142b;"
            "color:#eee}h1{font-size:20px}h2{font-size:16px;word-break:break-all}"
            "img{max-width:100%;height:auto;background:#fff}"
            "a{color:#8ab4ff}section{margin:0 0 28px}"
            "#share{margin:0 0 24px}#share button{display:block;width:100%;"
            "font-size:17px;padding:12px;margin:8px 0;border:0;border-radius:10px;"
            "background:#3b6cff;color:#fff;touch-action:manipulation}"
            "#share button.part{background:#2a2f5a;font-size:16px}"
            "#share button:disabled{opacity:.6}#share-msg{color:#ffd27a}"
            "</style></head><body>",
            f"<h1>{html.escape(self.app_name)} から受け取る</h1>",
            f"<p>{len(self.files)}件のファイルです。"
            f"<span id=\"hint\">{hint}</span></p>",
            "<div id=\"share\" hidden><div id=\"share-buttons\"></div>"
            "<p id=\"share-msg\"></p></div>",
        ]
        for i, path in enumerate(self.files):
            name = html.escape(os.path.basename(path))
            size = _size_text(os.path.getsize(path))
            parts.append("<section>")
            parts.append(f"<h2>{name}</h2>")
            if is_image(path):
                parts.append(f"<img src=\"f/{i}\" alt=\"{name}\">")
            parts.append(f"<p>{size} ・ <a href=\"d/{i}\">ダウンロード</a></p>")
            parts.append("</section>")
        if media:
            data = json.dumps(media, ensure_ascii=False).replace("</", "<\\/")
            parts.append("<script>")
            parts.append(_SHARE_JS.replace("__MEDIA__", data)
                         .replace("__CHUNK__", str(SHARE_CHUNK))
                         .replace("__ALL_MAX__", str(SHARE_ALL_MAX)))
            parts.append("</script>")
        parts.append("</body></html>")
        return "".join(parts).encode("utf-8")

    def _ca_html(self) -> bytes:
        steps = [
            "下の「証明書をダウンロード」を押し、「許可」を選ぶ",
            "設定 → 一般 → VPN とデバイス管理 で、ダウンロードした"
            "プロファイルをインストール",
            "設定 → 一般 → 情報 → 証明書信頼設定 で、この証明書をオン",
            "PC の QR（https で始まるほう）を読み直す",
        ]
        items = "".join(f"<li>{html.escape(s)}</li>" for s in steps)
        return (
            "<!DOCTYPE html><html lang=\"ja\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            "<title>証明書の登録</title>"
            "<style>body{font-family:sans-serif;margin:16px;background:#14142b;"
            "color:#eee;line-height:1.6}a{color:#8ab4ff;font-size:18px}</style>"
            "</head><body><h1 style=\"font-size:20px\">証明書の登録（最初の1回だけ）</h1>"
            f"<p>この PC の {html.escape(self.app_name)} から HTTPS で受け取り、まとめて写真に保存できる"
            "ようにするための証明書です。この PC 専用で、LAN の中でしか使えません。</p>"
            f"<ol>{items}</ol>"
            "<p><a href=\"ca.crt\">証明書をダウンロード</a></p>"
            "</body></html>").encode("utf-8")

    def _make_handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            timeout = 60   # 握手や要求を送ってこない相手を切る

            def log_message(self, *_args):  # アクセスログは出さない
                pass

            def do_GET(self):  # noqa: N802 (http.server の流儀)
                path = self.path.split("?", 1)[0]
                seg = [s for s in path.split("/") if s]
                if len(seg) < 2 or seg[0] != "k" or seg[1] != server.token:
                    self._reply(404, b"not found", "text/plain; charset=utf-8")
                    return
                rest = seg[2:]
                if not rest:
                    self._reply(200, server._index_html(),
                                "text/html; charset=utf-8")
                    return
                if len(rest) == 2 and rest[0] in ("f", "d") and rest[1].isdigit():
                    self._serve(int(rest[1]), download=rest[0] == "d")
                    return
                self._reply(404, b"not found", "text/plain; charset=utf-8")

            def _reply(self, code, body, ctype):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _serve(self, index, download):
                if not 0 <= index < len(server.files):
                    self._reply(404, b"not found", "text/plain; charset=utf-8")
                    return
                path = server.files[index]
                name = os.path.basename(path)
                rendered = server._rendered_bytes(index)
                if rendered is not None:
                    server._note("download" if download else "view", name)
                    self.send_response(200)
                    self.send_header("Content-Type", "image/jpeg")
                    self.send_header("Content-Length", str(len(rendered)))
                    self.send_header(
                        "Content-Disposition",
                        f"{'attachment' if download else 'inline'}; "
                        f"filename*=UTF-8''{quote(name)}")
                    self.end_headers()
                    try:
                        self.wfile.write(rendered)
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    return
                try:
                    size = os.path.getsize(path)
                    stream = open(path, "rb")
                except OSError:
                    self._reply(404, b"gone", "text/plain; charset=utf-8")
                    return
                ctype = (share_type(path) or mimetypes.guess_type(path)[0]
                         or "application/octet-stream")
                disposition = "attachment" if download else "inline"
                server._note("download" if download else "view", name)
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Length", str(size))
                    self.send_header(
                        "Content-Disposition",
                        f"{disposition}; filename*=UTF-8''{quote(name)}")
                    self.end_headers()
                    while True:
                        chunk = stream.read(256 * 1024)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # スマホ側が途中で切った
                finally:
                    stream.close()

        return Handler

    def _make_ca_handler(self):
        """証明書の登録用の入口(HTTP)。返すのは案内のページと CA の公開証明書だけ。"""
        server = self

        class Handler(BaseHTTPRequestHandler):
            timeout = 60

            def log_message(self, *_args):
                pass

            def do_GET(self):  # noqa: N802
                path = self.path.split("?", 1)[0]
                seg = [s for s in path.split("/") if s]
                if len(seg) < 2 or seg[0] != "k" or seg[1] != server.ca_token:
                    self._reply(404, b"not found", "text/plain; charset=utf-8")
                    return
                rest = seg[2:]
                if not rest:
                    self._reply(200, server._ca_html(), "text/html; charset=utf-8")
                    return
                if rest == ["ca.crt"]:
                    # iOS はこの Content-Type でプロファイルとして受け取る
                    server._note("ca", "証明書")
                    self._reply(200, server._ca_der, "application/x-x509-ca-cert")
                    return
                self._reply(404, b"not found", "text/plain; charset=utf-8")

            def _reply(self, code, body, ctype):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return Handler


# 受け取りページの「まとめて写真に保存（共有）」。
# Safari は、ボタンを押してから時間が経つと navigator.share を断る(NotAllowedError)。
# 押してから全ファイルを fetch するとそうなりうるので、断られたら「準備できました。
# もう一度押してください」にして、2回目はすぐ共有する(読み込んだ分は持っておく)。
# 件数が多いと iOS が失敗したり Safari のメモリが足りなくなったりするので、
# SHARE_CHUNK 件ずつのボタンも出す(SHARE_ALL_MAX を超えたら分けたボタンだけ)。
# 共有し終えた分は手放す(メモリを空ける)。
_SHARE_JS = r"""
(function () {
  "use strict";
  var MEDIA = __MEDIA__;
  var CHUNK = __CHUNK__, ALL_MAX = __ALL_MAX__;
  if (!MEDIA.length || !window.isSecureContext || !navigator.share ||
      !navigator.canShare || typeof File === "undefined") {
    return;  // 使えない(HTTP のまま など)。長押しの案内のまま
  }
  try {
    var probe = new File([""], MEDIA[0].name, {type: MEDIA[0].type});
    if (!navigator.canShare({files: [probe]})) { return; }
  } catch (e) { return; }

  var box = document.getElementById("share");
  var buttons = document.getElementById("share-buttons");
  var msg = document.getElementById("share-msg");
  var cache = {};  // 番号 → File(読み込み済み)
  function say(text) { msg.textContent = text; }

  function fetchAll(items, progress) {
    var done = 0;
    return Promise.all(items.map(function (m) {
      if (cache[m.i]) { done++; progress(done); return Promise.resolve(cache[m.i]); }
      return fetch("f/" + m.i).then(function (r) {
        if (!r.ok) { throw new Error("HTTP " + r.status); }
        return r.blob();
      }).then(function (blob) {
        var file = new File([blob], m.name, {type: m.type});
        cache[m.i] = file;
        done++;
        progress(done);
        return file;
      });
    }));
  }

  function makeButton(items, label, cls) {
    var b = document.createElement("button");
    b.type = "button";
    b.textContent = label;
    if (cls) { b.className = cls; }
    var ready = null;
    function share(files) {
      var data = {files: files};
      if (!navigator.canShare(data)) {
        say("この件数・種類はまとめて共有できません。分けて試してください。");
        return;
      }
      navigator.share(data).then(function () {
        say(label + "：共有しました。");
        ready = null;
        b.textContent = label;
        items.forEach(function (m) { delete cache[m.i]; });
      }).catch(function (e) {
        var name = (e && e.name) || "Error";
        if (name === "AbortError") { return; }  // 共有シートを閉じただけ
        if (name === "NotAllowedError") {
          b.textContent = "準備できました。もう一度押すと共有します";
          say("うまくいかないときは、分けて共有してください。");
          return;
        }
        say("共有できませんでした（" + name + "）。分けて試してください。");
      });
    }
    b.addEventListener("click", function () {
      if (b.disabled) { return; }
      if (ready) { share(ready); return; }
      b.disabled = true;
      say("");
      fetchAll(items, function (n) {
        b.textContent = "準備中… " + n + "/" + items.length;
      }).then(function (files) {
        ready = files;
        b.disabled = false;
        b.textContent = label;
        share(files);
      }).catch(function (e) {
        b.disabled = false;
        b.textContent = label;
        say("読み込めませんでした（" + e.message + "）。");
      });
    });
    return b;
  }

  if (MEDIA.length <= ALL_MAX) {
    buttons.appendChild(makeButton(
      MEDIA, "まとめて写真に保存（共有）全" + MEDIA.length + "件", ""));
  }
  if (MEDIA.length > CHUNK) {
    var note = document.createElement("p");
    note.textContent = MEDIA.length <= ALL_MAX
        ? "うまくいかないときは、分けて共有してください:"
        : "件数が多いので、" + CHUNK + "件ずつ共有してください:";
    buttons.appendChild(note);
    for (var start = 0; start < MEDIA.length; start += CHUNK) {
      var part = MEDIA.slice(start, start + CHUNK);
      buttons.appendChild(makeButton(
        part, (start + 1) + "〜" + (start + part.length) + "件を共有", "part"));
    }
  }
  document.getElementById("hint").textContent =
      "共有シートの「画像を保存」で写真に入ります（ほかのアプリにも送れます）。";
  box.hidden = false;
})();
"""



def photo_edits(path: str):
    """写真の補正（crs:）を読む（読み取りキャッシュ経由。読めなければ None）。
    ShareServer の edits_provider として渡すための既定の関数。"""
    from . import metacache
    try:
        meta = metacache.read_meta_cached(path)
    except Exception:  # noqa: BLE001
        return None
    return meta.edits if meta else None


def frame_strip_gps() -> bool:
    """補正を当てた JPEG から GPS を消すか（共通設定 frame.strip_gps。既定は消す）。
    フォトフレーム同期と同じ設定に合わせる。"""
    from . import config
    try:
        cfg = config.load_config()
    except Exception:  # noqa: BLE001
        return True
    frame = cfg.get("frame") or {}
    return bool(frame.get("strip_gps", True))


def qr_matrix(text: str):
    """QR コードの点の行列(1 が黒)を返す。Qt に依存しないので、描画は
    呼び出し側(PySide6 / PyQt6)がこの行列から行う。segno は遅延 import。"""
    import segno

    qr = segno.make_qr(text.encode("utf-8"), error="m", mode="byte",
                       boost_error=False)
    return [list(row) for row in qr.matrix]
