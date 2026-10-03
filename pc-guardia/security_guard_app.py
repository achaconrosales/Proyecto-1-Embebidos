"""
security_guard_app.py - Receptor del sistema Secure Vision (PC del guardia).

  * Muestra el video en vivo que envía la Raspberry Pi por RTP/JPEG sobre UDP.
  * Descarga por SSH los clips de eventos (acceso_*.avi) y la bitácora.
    La sincronización corre sola cada SYNC_INTERVAL_S segundos: la Raspberry
    guarda solo los últimos 10 clips, así que la PC debe traerlos antes de que
    se roten.
  * Acceso SSH: si hay una llave instalada en la Raspberry se usa la llave;
    si no, se usa la contraseña RPI_SSH_PASSWORD (vía SSH_ASKPASS, sin sshpass).
"""

import sys
import time
import os
import re
import json
import stat
import tempfile
import subprocess

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst

from PyQt6.QtCore import Qt, QTimer, QThread, pyqtSignal, QUrl
from PyQt6.QtGui import QImage, QPixmap, QDesktopServices
from PyQt6.QtWidgets import (
    QApplication,
    QWidget,
    QLabel,
    QPushButton,
    QLineEdit,
    QVBoxLayout,
    QHBoxLayout,
    QFrame,
    QMessageBox,
    QListWidget,
    QListWidgetItem,
    QDialog,
)


# ============================================================
# CONFIGURACIÓN
# (cada valor se puede sobreescribir con una variable de entorno)
# ============================================================

UDP_PORT = int(os.environ.get("SECUREVISION_UDP_PORT", "5000"))

USERNAME = "guardia"
PASSWORD = "1234"

# Raspberry Pi
RPI_HOST = os.environ.get("SECUREVISION_RPI_HOST", "root@10.208.1.62")
RPI_EVENTS_DIR = "/home/root/eventos"
RPI_BITACORA = "/home/root/bitacora_accesos.csv"

# Contraseña de root de la Raspberry. Es la que se fija en local.conf
# (EXTRA_USERS_PARAMS). Si se instala una llave con ssh-copy-id, ssh usa la
# llave primero y esta contraseña ya no se necesita.
RPI_SSH_PASSWORD = os.environ.get("SECUREVISION_RPI_PASSWORD", "SV-yMMr-uSfh-VC7Z")

# Sincronización automática de eventos (segundos). 0 = solo manual.
SYNC_INTERVAL_S = int(os.environ.get("SECUREVISION_SYNC_S", "15"))

# Días que se conservan los clips en esta PC (0 = sin límite). Ver política H7.
LOCAL_RETENTION_DAYS = int(os.environ.get("SECUREVISION_RETENCION_DIAS", "30"))

# Jitter buffer RTP en ms (0 = desactivado: menor latencia, más sensible a
# pérdidas y desorden en Wi-Fi).
JITTER_MS = int(os.environ.get("SECUREVISION_JITTER_MS", "0"))

# Carpeta local donde se guardarán los eventos
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOCAL_EVENTS_DIR = os.path.join(BASE_DIR, "eventos")

# Huella del servidor SSH de la Raspberry. Si se regraba la tarjeta SD la
# huella cambia: hay que borrar este archivo.
KNOWN_HOSTS = os.path.join(BASE_DIR, "known_hosts_rpi")

# Archivo que mantiene el registro de videos ya descargados
EVENTS_DATABASE = os.path.join(
    LOCAL_EVENTS_DIR,
    "eventos_descargados.json"
)

LOCAL_BITACORA = os.path.join(LOCAL_EVENTS_DIR, "bitacora_rpi.csv")

# Extensiones de video aceptadas
VIDEO_EXTENSIONS = (
    ".mp4",
    ".avi",
    ".mkv",
    ".mov",
    ".h264",
    ".m4v",
)

# Solo se descargan nombres seguros (evita inyección en el comando remoto)
SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")


# ============================================================
# GSTREAMER
# ============================================================

JITTER = (
    f"! rtpjitterbuffer latency={JITTER_MS} " if JITTER_MS > 0 else ""
)

GSTREAMER_PIPELINE = f"""
udpsrc port={UDP_PORT} buffer-size=4194304
caps="application/x-rtp,media=video,clock-rate=90000,encoding-name=JPEG,payload=26"
{JITTER}
!
rtpjpegdepay
!
jpegdec
!
videoconvert
!
video/x-raw,format=RGB
!
appsink name=videosink sync=false max-buffers=1 drop=true
"""


class GStreamerReceiver:

    def __init__(self):

        Gst.init(None)

        self.pipeline = None
        self.appsink = None
        self.bus = None

        self.connected = False
        self.last_frame_time = 0

        self.frames = 0
        self.fps = 0.0
        self._fps_t0 = time.time()

    def start(self):

        self.stop()

        try:

            print("Iniciando pipeline GStreamer...")

            self.pipeline = Gst.parse_launch(GSTREAMER_PIPELINE)
            self.appsink = self.pipeline.get_by_name("videosink")
            self.bus = self.pipeline.get_bus()

            if self.appsink is None:
                print("ERROR: No se encontró appsink")
                return False

            result = self.pipeline.set_state(Gst.State.PLAYING)

            if result == Gst.StateChangeReturn.FAILURE:
                print("ERROR: el pipeline no pasó a PLAYING")
                print(self.check_bus() or "")
                self.stop()
                return False

            self.connected = True
            print("Pipeline iniciado correctamente")
            return True

        except Exception as e:

            print(f"Error iniciando GStreamer: {e}")
            self.stop()
            return False

    def check_bus(self):
        """Atiende error, warning y eos del bus (checklist E1).

        Devuelve el texto del error si el pipeline falló, o None.
        """
        if self.bus is None:
            return None

        while True:
            msg = self.bus.pop_filtered(
                Gst.MessageType.ERROR
                | Gst.MessageType.WARNING
                | Gst.MessageType.EOS
            )
            if msg is None:
                return None

            source = msg.src.get_name() if msg.src else "?"

            if msg.type == Gst.MessageType.ERROR:
                err, debug = msg.parse_error()
                text = f"{source}: {err.message}"
                print(f"[GStreamer ERROR] {text} ({debug})")
                return text

            if msg.type == Gst.MessageType.EOS:
                print(f"[GStreamer EOS] {source}")
                return f"{source}: fin de flujo (EOS)"

            warn, debug = msg.parse_warning()
            print(f"[GStreamer WARNING] {source}: {warn.message} ({debug})")

    def get_frame(self):

        if self.appsink is None:
            return None

        try:

            # Usamos emit porque algunas versiones
            # de PyGObject no exponen try_pull_sample()
            sample = self.appsink.emit("try-pull-sample", 0)

            if sample is None:
                return None

            buffer = sample.get_buffer()
            caps = sample.get_caps()

            if buffer is None or caps is None:
                return None

            structure = caps.get_structure(0)
            width = structure.get_value("width")
            height = structure.get_value("height")

            success, map_info = buffer.map(Gst.MapFlags.READ)

            if not success:
                return None

            try:

                # Bytes por línea reales: GStreamer rellena cada línea RGB
                # hasta múltiplo de 4, así que no siempre es width * 3.
                bytes_per_line = map_info.size // height

                image = QImage(
                    map_info.data,
                    width,
                    height,
                    bytes_per_line,
                    QImage.Format.Format_RGB888,
                ).copy()

            finally:

                buffer.unmap(map_info)

            self.last_frame_time = time.time()
            self.frames += 1
            return image

        except Exception as e:

            print(f"Error recibiendo frame: {e}")
            return None

    def update_fps(self):
        now = time.time()
        elapsed = now - self._fps_t0
        if elapsed >= 1.0:
            self.fps = self.frames / elapsed
            self.frames = 0
            self._fps_t0 = now
        return self.fps

    def stop(self):

        if self.pipeline is not None:
            try:
                self.pipeline.set_state(Gst.State.NULL)
            except Exception:
                pass

        self.pipeline = None
        self.appsink = None
        self.bus = None
        self.connected = False
        self.last_frame_time = 0

    def is_receiving(self):

        if self.pipeline is None or self.last_frame_time == 0:
            return False

        return (time.time() - self.last_frame_time) < 2.0


# ============================================================
# SSH HACIA LA RASPBERRY PI
# ============================================================

class RaspberrySSH:
    """Ejecuta comandos en la Raspberry con ssh.

    Autenticación: primero llave pública (si existe), luego la contraseña
    RPI_SSH_PASSWORD. La contraseña se entrega con SSH_ASKPASS (un script
    temporal que la lee de una variable de entorno), así no hace falta
    instalar sshpass ni escribir la contraseña en un archivo.
    """

    _askpass = None

    @classmethod
    def _askpass_path(cls):
        if cls._askpass is None or not os.path.exists(cls._askpass):
            directory = tempfile.mkdtemp(prefix="securevision-")
            path = os.path.join(directory, "askpass.sh")
            with open(path, "w", encoding="utf-8") as file:
                file.write('#!/bin/sh\nprintf "%s\\n" "$SECUREVISION_ASKPASS_PW"\n')
            os.chmod(path, stat.S_IRWXU)
            cls._askpass = path
        return cls._askpass

    @classmethod
    def _environment(cls):
        env = os.environ.copy()
        env["SSH_ASKPASS"] = cls._askpass_path()
        env["SSH_ASKPASS_REQUIRE"] = "force"      # OpenSSH >= 8.4
        env.setdefault("DISPLAY", ":0")          # OpenSSH < 8.4 lo exige
        env["SECUREVISION_ASKPASS_PW"] = RPI_SSH_PASSWORD
        return env

    @staticmethod
    def _options():
        return [
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"UserKnownHostsFile={KNOWN_HOSTS}",
            "-o", "ConnectTimeout=5",
            "-o", "ServerAliveInterval=5",
            "-o", "ServerAliveCountMax=2",
            "-o", "NumberOfPasswordPrompts=1",
            "-o", "PreferredAuthentications=publickey,password",
        ]

    @classmethod
    def run(cls, remote_command, timeout, stdout=subprocess.PIPE):
        return subprocess.run(
            ["ssh", *cls._options(), RPI_HOST, remote_command],
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=subprocess.PIPE,
            timeout=timeout,
            env=cls._environment(),
            start_new_session=True,   # sin terminal: fuerza SSH_ASKPASS
        )

    @staticmethod
    def explain(stderr_bytes):
        text = stderr_bytes.decode("utf-8", "replace").strip()
        if "REMOTE HOST IDENTIFICATION HAS CHANGED" in text:
            return (
                "La huella SSH de la Raspberry cambió (¿se regrabó la SD?).\n"
                f"Si es la misma Raspberry, borre el archivo:\n{KNOWN_HOSTS}"
            )
        if "Permission denied" in text:
            return (
                "La Raspberry rechazó la contraseña. Revise RPI_SSH_PASSWORD "
                "o la variable SECUREVISION_RPI_PASSWORD."
            )
        if "timed out" in text or "No route to host" in text or "Connection refused" in text:
            return f"No hay conexión con {RPI_HOST}.\n{text}"
        return text or "No fue posible conectarse con la Raspberry Pi."


# ============================================================
# SINCRONIZACIÓN DE EVENTOS
# ============================================================


def list_local_videos():
    if not os.path.isdir(LOCAL_EVENTS_DIR):
        return []
    videos = [
        f for f in os.listdir(LOCAL_EVENTS_DIR)
        if f.lower().endswith(VIDEO_EXTENSIONS)
    ]
    videos.sort(reverse=True)   # más reciente primero
    return videos


class EventSyncWorker(QThread):

    finished = pyqtSignal(list)
    error = pyqtSignal(str)
    progress = pyqtSignal(str)

    def run(self):

        try:

            os.makedirs(LOCAL_EVENTS_DIR, exist_ok=True)

            self.progress.emit("Consultando eventos en la Raspberry Pi...")

            # ------------------------------------------------
            # 1. Nombre, tamaño y fecha de todos los clips
            #    en UNA sola conexión (compatible con busybox)
            # ------------------------------------------------

            listing = (
                f"cd '{RPI_EVENTS_DIR}' 2>/dev/null || exit 0; "
                "for f in *.avi *.mp4 *.mkv *.mov *.h264 *.m4v; do "
                "[ -f \"$f\" ] && stat -c '%n %s %Y' \"$f\"; "
                "done; exit 0"
            )

            result = RaspberrySSH.run(listing, timeout=30)

            if result.returncode != 0:
                raise Exception(RaspberrySSH.explain(result.stderr))

            remote_files = []

            for line in result.stdout.decode("utf-8", "replace").splitlines():

                parts = line.strip().rsplit(" ", 2)

                if len(parts) != 3:
                    continue

                filename, size, mtime = parts

                if not SAFE_NAME.match(filename):
                    continue

                if not filename.lower().endswith(VIDEO_EXTENSIONS):
                    continue

                try:
                    remote_files.append({
                        "filename": filename,
                        "size": int(size),
                        "mtime": float(mtime),
                    })
                except ValueError:
                    continue

            # ------------------------------------------------
            # 2. Registro local de lo ya descargado
            # ------------------------------------------------

            downloaded = {}

            if os.path.exists(EVENTS_DATABASE):
                try:
                    with open(EVENTS_DATABASE, "r", encoding="utf-8") as file:
                        downloaded = json.load(file)
                except Exception:
                    downloaded = {}

            # ------------------------------------------------
            # 3. Nuevos o modificados. Un clip borrado a mano en la PC
            #    no se vuelve a descargar (lo decidió el guardia).
            # ------------------------------------------------

            new_files = []

            for remote in remote_files:
                previous = downloaded.get(remote["filename"])
                if (
                    previous is None
                    or previous.get("size") != remote["size"]
                    or previous.get("mtime") != remote["mtime"]
                ):
                    new_files.append(remote)

            # ------------------------------------------------
            # 4. Descarga: ssh cat -> archivo .part -> renombrar.
            #    No depende de scp ni de sftp-server en la Raspberry.
            # ------------------------------------------------

            failures = []
            total = len(new_files)

            for index, remote in enumerate(new_files, start=1):

                filename = remote["filename"]

                self.progress.emit(f"Descargando {index}/{total}: {filename}")

                local_path = os.path.join(LOCAL_EVENTS_DIR, filename)
                partial = local_path + ".part"

                with open(partial, "wb") as out:
                    result = RaspberrySSH.run(
                        f"cat '{RPI_EVENTS_DIR}/{filename}'",
                        timeout=120,
                        stdout=out,
                    )

                size = os.path.getsize(partial)

                if result.returncode != 0 or size != remote["size"]:
                    # Puede haberse rotado en la RPi entre el listado y la descarga
                    os.remove(partial)
                    failures.append(filename)
                    continue

                os.replace(partial, local_path)

                # La fecha del archivo local = fecha del evento (para la retención)
                os.utime(local_path, (remote["mtime"], remote["mtime"]))

                downloaded[filename] = {
                    "size": remote["size"],
                    "mtime": remote["mtime"],
                }

                self._save_database(downloaded)

            # ------------------------------------------------
            # 5. Bitácora de la Raspberry (no es crítica)
            # ------------------------------------------------

            try:
                with open(LOCAL_BITACORA + ".part", "wb") as out:
                    result = RaspberrySSH.run(
                        f"cat '{RPI_BITACORA}'", timeout=30, stdout=out
                    )
                if result.returncode == 0:
                    os.replace(LOCAL_BITACORA + ".part", LOCAL_BITACORA)
                else:
                    os.remove(LOCAL_BITACORA + ".part")
            except Exception as e:
                print(f"No se pudo descargar la bitácora: {e}")

            # ------------------------------------------------
            # 6. Retención local (H7)
            # ------------------------------------------------

            removed = self._apply_retention()

            # Olvidar entradas que ya no existen en ningún lado
            remote_names = {r["filename"] for r in remote_files}
            local_names = set(list_local_videos())
            for name in list(downloaded):
                if name not in remote_names and name not in local_names:
                    del downloaded[name]
            self._save_database(downloaded)

            summary = f"{total - len(failures)} evento(s) nuevo(s)"
            if removed:
                summary += f", {removed} eliminado(s) por retención"
            if failures:
                summary += f", {len(failures)} no disponible(s)"
            self.progress.emit(summary)

            self.finished.emit(list_local_videos())

        except subprocess.TimeoutExpired:

            self.error.emit("La conexión con la Raspberry Pi tardó demasiado.")

        except Exception as e:

            self.error.emit(str(e))

    @staticmethod
    def _save_database(downloaded):
        tmp = EVENTS_DATABASE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as file:
            json.dump(downloaded, file, indent=4)
        os.replace(tmp, EVENTS_DATABASE)

    @staticmethod
    def _apply_retention():
        if LOCAL_RETENTION_DAYS <= 0:
            return 0
        limit = time.time() - LOCAL_RETENTION_DAYS * 86400
        removed = 0
        for filename in os.listdir(LOCAL_EVENTS_DIR):
            is_video = filename.lower().endswith(VIDEO_EXTENSIONS)
            is_capture = filename.startswith("evidence_") and filename.endswith(".png")
            if not (is_video or is_capture):
                continue
            path = os.path.join(LOCAL_EVENTS_DIR, filename)
            if os.path.getmtime(path) < limit:
                os.remove(path)
                removed += 1
        return removed


# ============================================================
# VENTANA DE EVENTOS
# ============================================================

class EventsWindow(QDialog):

    refresh_requested = pyqtSignal()

    def __init__(self, parent=None):

        super().__init__(parent)

        self.setWindowTitle("Secure Vision - Eventos")
        self.resize(700, 500)

        self.setStyleSheet("""
            QDialog { background-color: #080e14; color: white; }
            QLabel { color: white; }
            QListWidget {
                background-color: #101923; border: 1px solid #263646;
                border-radius: 6px; color: white; padding: 5px; font-size: 13px;
            }
            QListWidget::item { padding: 12px; border-bottom: 1px solid #263646; }
            QListWidget::item:selected { background-color: #1769aa; }
            QPushButton {
                background-color: #172432; border: 1px solid #2b3d4f;
                border-radius: 5px; padding: 10px 15px; color: white;
            }
            QPushButton:hover { background-color: #213448; }
        """)

        layout = QVBoxLayout()

        title = QLabel("EVENTOS DE VIDEOVIGILANCIA")
        title.setStyleSheet("font-size: 22px; font-weight: bold; color: #2ea3ff;")

        subtitle = QLabel(
            "Videos descargados desde la Raspberry Pi · doble clic para reproducir"
        )
        subtitle.setStyleSheet("color: #8291a1; font-size: 12px;")

        self.status = QLabel("Cargando eventos...")
        self.status.setStyleSheet("color: #8291a1;")

        self.video_list = QListWidget()
        self.video_list.itemDoubleClicked.connect(self.open_video)

        buttons = QHBoxLayout()

        self.refresh_button = QPushButton("↻  ACTUALIZAR")
        self.refresh_button.clicked.connect(self.refresh_requested.emit)

        self.folder_button = QPushButton("ABRIR CARPETA")
        self.folder_button.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(LOCAL_EVENTS_DIR))
        )

        self.close_button = QPushButton("CERRAR")
        self.close_button.clicked.connect(self.close)

        buttons.addWidget(self.refresh_button)
        buttons.addWidget(self.folder_button)
        buttons.addStretch()
        buttons.addWidget(self.close_button)

        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addSpacing(10)
        layout.addWidget(self.status)
        layout.addWidget(self.video_list)
        layout.addLayout(buttons)

        self.setLayout(layout)

    def set_videos(self, videos):

        self.video_list.clear()

        for video in videos:

            path = os.path.join(LOCAL_EVENTS_DIR, video)
            try:
                size_mb = os.path.getsize(path) / (1024 * 1024)
                when = time.strftime("%d/%m/%Y %H:%M:%S", time.localtime(os.path.getmtime(path)))
                text = f"🎥  {video}    ·    {when}    ·    {size_mb:.1f} MB"
            except OSError:
                text = f"🎥  {video}"

            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, video)
            self.video_list.addItem(item)

        if len(videos) == 0:
            self.status.setText("No hay videos almacenados.")
        else:
            self.status.setText(f"{len(videos)} evento(s) almacenado(s)")

    def open_video(self, item):
        video = item.data(Qt.ItemDataRole.UserRole)
        QDesktopServices.openUrl(
            QUrl.fromLocalFile(os.path.join(LOCAL_EVENTS_DIR, video))
        )


# ============================================================
# LOGIN
# ============================================================

class LoginPage(QWidget):

    def __init__(self, login_callback):

        super().__init__()

        self.login_callback = login_callback

        self.setStyleSheet("""
            QWidget { background-color: #0b1118; color: #ffffff; font-family: Arial; }
            QLineEdit {
                background-color: #151e28; border: 1px solid #344454;
                border-radius: 6px; padding: 12px; color: white; font-size: 14px;
            }
            QLineEdit:focus { border: 1px solid #2ea3ff; }
            QPushButton {
                background-color: #1769aa; border: none; border-radius: 6px;
                padding: 12px; color: white; font-weight: bold; font-size: 14px;
            }
            QPushButton:hover { background-color: #2185d0; }
        """)

        main_layout = QVBoxLayout()
        main_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        title = QLabel("SECURE VISION")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet("font-size: 30px; font-weight: bold; color: #2ea3ff;")

        subtitle = QLabel("CENTRO DE MONITOREO")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        subtitle.setStyleSheet("font-size: 13px; color: #8b9aaa; letter-spacing: 2px;")

        panel = QFrame()
        panel.setMaximumWidth(400)
        panel.setStyleSheet("""
            QFrame { background-color: #101923; border: 1px solid #263646; border-radius: 10px; }
        """)

        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(35, 35, 35, 35)
        panel_layout.setSpacing(15)

        login_title = QLabel("Identificación del guardia")
        login_title.setStyleSheet("font-size: 18px; font-weight: bold; color: white;")

        self.username = QLineEdit()
        self.username.setPlaceholderText("Usuario")

        self.password = QLineEdit()
        self.password.setPlaceholderText("Contraseña")
        self.password.setEchoMode(QLineEdit.EchoMode.Password)

        self.login_button = QPushButton("INICIAR SESIÓN")
        self.login_button.clicked.connect(self.check_login)
        self.password.returnPressed.connect(self.check_login)

        self.error_label = QLabel("")
        self.error_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.error_label.setStyleSheet("color: #ff5c5c; font-size: 12px;")

        panel_layout.addWidget(login_title)
        panel_layout.addWidget(self.username)
        panel_layout.addWidget(self.password)
        panel_layout.addWidget(self.login_button)
        panel_layout.addWidget(self.error_label)

        main_layout.addWidget(title)
        main_layout.addWidget(subtitle)
        main_layout.addSpacing(30)
        main_layout.addWidget(panel)

        self.setLayout(main_layout)

    def check_login(self):

        if (
            self.username.text() == USERNAME
            and self.password.text() == PASSWORD
        ):
            self.error_label.setText("")
            self.login_callback()
        else:
            self.error_label.setText("Usuario o contraseña incorrectos")
            self.password.clear()


# ============================================================
# DASHBOARD
# ============================================================

class Dashboard(QWidget):

    def __init__(self, logout_callback):

        super().__init__()

        self.logout_callback = logout_callback

        self.receiver = GStreamerReceiver()

        self.last_image = None

        self.event_worker = None
        self.events_window = None
        self.sync_interactive = False

        self.latency_mode = False

        self.setStyleSheet("""
            QWidget { background-color: #080e14; color: white; font-family: Arial; }
            QPushButton {
                background-color: #172432; border: 1px solid #2b3d4f;
                border-radius: 5px; padding: 9px 15px; color: white;
            }
            QPushButton:hover { background-color: #213448; }
            QPushButton:disabled { background-color: #0f171f; color: #596979; }
        """)

        self.build_ui()

        # Frames de GStreamer
        self.frame_timer = QTimer()
        self.frame_timer.timeout.connect(self.update_frame)
        self.frame_timer.start(15)

        # Estado + bus de GStreamer
        self.status_timer = QTimer()
        self.status_timer.timeout.connect(self.update_status)
        self.status_timer.start(500)

        # Reloj
        self.clock_timer = QTimer()
        self.clock_timer.timeout.connect(self.update_clock)
        self.clock_timer.start(1000)

        # Sincronización automática de eventos
        self.sync_timer = QTimer()
        self.sync_timer.timeout.connect(lambda: self.start_sync(interactive=False))
        if SYNC_INTERVAL_S > 0:
            self.sync_timer.start(SYNC_INTERVAL_S * 1000)
            QTimer.singleShot(1000, lambda: self.start_sync(interactive=False))

        self.start_receiver()

    # --------------------------------------------------------
    # UI
    # --------------------------------------------------------

    def build_ui(self):

        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(20, 20, 20, 20)
        main_layout.setSpacing(15)

        # HEADER

        header = QHBoxLayout()

        title = QLabel("SECURE VISION")
        title.setStyleSheet("font-size: 24px; font-weight: bold; color: #2ea3ff;")

        system = QLabel("SISTEMA DE MONITOREO")
        system.setStyleSheet("color: #8291a1; font-size: 12px;")

        self.clock_label = QLabel("--:--:--")
        self.clock_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.clock_label.setStyleSheet("color: #b9c5d0; font-size: 14px; font-weight: bold;")

        header.addWidget(title)
        header.addSpacing(15)
        header.addWidget(system)
        header.addStretch()
        header.addWidget(self.clock_label)

        main_layout.addLayout(header)

        # INFO BAR

        info_bar = QFrame()
        info_bar.setStyleSheet("""
            QFrame { background-color: #101923; border: 1px solid #263646; border-radius: 6px; }
        """)

        info_layout = QHBoxLayout(info_bar)

        camera_label = QLabel("CÁMARA: FRONT")
        port_label = QLabel(f"UDP PORT: {UDP_PORT}")
        codec_label = QLabel("CODEC: JPEG/RTP")
        self.fps_label = QLabel("FPS: --")
        self.sync_label = QLabel("SYNC: --")
        self.status_label = QLabel("● OFFLINE")

        for label in (camera_label, port_label, codec_label, self.fps_label, self.sync_label):
            label.setStyleSheet("color: #aab7c4; font-size: 12px;")

        self.status_label.setStyleSheet("color: #ff5c5c; font-weight: bold;")

        info_layout.addWidget(camera_label)
        info_layout.addStretch()
        info_layout.addWidget(port_label)
        info_layout.addSpacing(20)
        info_layout.addWidget(codec_label)
        info_layout.addSpacing(20)
        info_layout.addWidget(self.fps_label)
        info_layout.addSpacing(20)
        info_layout.addWidget(self.sync_label)
        info_layout.addSpacing(20)
        info_layout.addWidget(self.status_label)

        main_layout.addWidget(info_bar)

        # CRONÓMETRO PARA MEDIR LATENCIA (checklist D2)

        self.latency_label = QLabel("")
        self.latency_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.latency_label.setStyleSheet("""
            color: #ffffff; background-color: #000000;
            font-family: monospace; font-size: 42px; font-weight: bold; padding: 6px;
        """)
        self.latency_label.hide()
        main_layout.addWidget(self.latency_label)

        # VIDEO

        self.video_frame = QLabel()
        self.video_frame.setMinimumSize(640, 480)
        self.video_frame.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.video_frame.setText(f"ESPERANDO TRANSMISIÓN...\n\nUDP :{UDP_PORT}")
        self.video_frame.setStyleSheet("""
            QLabel {
                background-color: #020508; border: 1px solid #263646;
                border-radius: 6px; color: #596979; font-size: 16px;
            }
        """)

        main_layout.addWidget(self.video_frame, 1)

        # CONTROLES

        controls = QHBoxLayout()

        self.reconnect_button = QPushButton("↻  RECONECTAR")
        self.reconnect_button.clicked.connect(self.reconnect)

        self.capture_button = QPushButton("▣  CAPTURAR EVIDENCIA")
        self.capture_button.clicked.connect(self.capture_frame)

        self.events_button = QPushButton("▶  VER EVENTOS")
        self.events_button.clicked.connect(self.show_events)

        self.latency_button = QPushButton("⏱  MEDIR LATENCIA")
        self.latency_button.setCheckable(True)
        self.latency_button.toggled.connect(self.toggle_latency)

        self.logout_button = QPushButton("CERRAR SESIÓN")
        self.logout_button.clicked.connect(self.logout)

        controls.addWidget(self.reconnect_button)
        controls.addWidget(self.capture_button)
        controls.addWidget(self.events_button)
        controls.addWidget(self.latency_button)
        controls.addStretch()
        controls.addWidget(self.logout_button)

        main_layout.addLayout(controls)

        # FOOTER

        footer = QLabel("Secure Vision  •  Sistema de monitoreo UDP/RTP")
        footer.setStyleSheet("color: #526272; font-size: 11px;")
        footer.setAlignment(Qt.AlignmentFlag.AlignCenter)

        main_layout.addWidget(footer)

        self.setLayout(main_layout)

    # --------------------------------------------------------
    # GSTREAMER
    # --------------------------------------------------------

    def start_receiver(self):

        success = self.receiver.start()

        if not success:
            self.status_label.setText("● ERROR")
            self.status_label.setStyleSheet("color: #ff5c5c; font-weight: bold;")

    def reconnect(self):

        self.status_label.setText("● RECONECTANDO...")
        self.status_label.setStyleSheet("color: #ffaa00; font-weight: bold;")

        QApplication.processEvents()

        self.receiver.stop()
        time.sleep(0.2)
        self.start_receiver()

    # --------------------------------------------------------
    # FRAME
    # --------------------------------------------------------

    def update_frame(self):

        if self.latency_mode:
            now = time.time()
            self.latency_label.setText(
                time.strftime("%H:%M:%S", time.localtime(now)) + f".{int((now % 1) * 1000):03d}"
            )

        image = self.receiver.get_frame()

        if image is None:
            return

        self.last_image = image

        pixmap = QPixmap.fromImage(image).scaled(
            self.video_frame.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )

        self.video_frame.setPixmap(pixmap)

    # --------------------------------------------------------
    # ESTADO
    # --------------------------------------------------------

    def update_status(self):

        error = self.receiver.check_bus()

        if error is not None:
            # El pipeline falló: se muestra y se reintenta solo en 3 s
            self.status_label.setText("● ERROR")
            self.status_label.setToolTip(error)
            self.status_label.setStyleSheet("color: #ff5c5c; font-weight: bold;")
            self.receiver.stop()
            QTimer.singleShot(3000, self.start_receiver)
            return

        self.fps_label.setText(f"FPS: {self.receiver.update_fps():.0f}")

        if self.receiver.is_receiving():
            self.status_label.setText("● LIVE")
            self.status_label.setToolTip("")
            self.status_label.setStyleSheet("color: #36d399; font-weight: bold;")
        elif self.receiver.pipeline is not None:
            self.status_label.setText("● OFFLINE")
            self.status_label.setStyleSheet("color: #ff5c5c; font-weight: bold;")

    # --------------------------------------------------------
    # RELOJ Y LATENCIA
    # --------------------------------------------------------

    def update_clock(self):
        self.clock_label.setText(time.strftime("%H:%M:%S"))

    def toggle_latency(self, enabled):
        """Cronómetro con milisegundos: apunte la cámara a esta pantalla y
        capture la ventana. La diferencia entre el cronómetro y su imagen en
        el video es la latencia de extremo a extremo (checklist D2)."""
        self.latency_mode = enabled
        self.latency_label.setVisible(enabled)

    # --------------------------------------------------------
    # CAPTURA
    # --------------------------------------------------------

    def capture_frame(self):

        os.makedirs(LOCAL_EVENTS_DIR, exist_ok=True)

        if self.latency_mode:
            # Evidencia de latencia: ventana completa (cronómetro + video)
            filepath = os.path.join(
                LOCAL_EVENTS_DIR, time.strftime("latencia_%Y%m%d_%H%M%S.png")
            )
            success = self.grab().save(filepath)

        else:

            if self.last_image is None:
                QMessageBox.warning(
                    self,
                    "Sin transmisión",
                    "No hay ningún frame disponible para capturar.",
                )
                return

            filepath = os.path.join(
                LOCAL_EVENTS_DIR, time.strftime("evidence_%Y%m%d_%H%M%S.png")
            )
            success = self.last_image.save(filepath)

        if success:
            QMessageBox.information(
                self, "Evidencia guardada", f"La captura fue guardada en:\n\n{filepath}"
            )
        else:
            QMessageBox.warning(self, "Error", "No fue posible guardar la captura.")

    # --------------------------------------------------------
    # EVENTOS
    # --------------------------------------------------------

    def show_events(self):

        # Evita crear múltiples ventanas
        if self.events_window is None:
            self.events_window = EventsWindow(self)
            self.events_window.refresh_requested.connect(
                lambda: self.start_sync(interactive=True)
            )

        self.events_window.set_videos(list_local_videos())
        self.events_window.show()
        self.events_window.raise_()
        self.events_window.activateWindow()

        self.start_sync(interactive=True)

    def start_sync(self, interactive):

        # Si ya existe una sincronización, no iniciamos otra.
        if self.event_worker is not None and self.event_worker.isRunning():
            self.sync_interactive = self.sync_interactive or interactive
            return

        self.sync_interactive = interactive

        if interactive:
            self.events_button.setEnabled(False)
            self.events_button.setText("⟳  CARGANDO EVENTOS...")
            if self.events_window is not None:
                self.events_window.status.setText("Conectando con Raspberry Pi...")

        self.event_worker = EventSyncWorker()
        self.event_worker.finished.connect(self.events_sync_finished)
        self.event_worker.error.connect(self.events_sync_error)
        self.event_worker.progress.connect(self.events_sync_progress)
        self.event_worker.start()

    def _sync_done(self):
        self.events_button.setEnabled(True)
        self.events_button.setText("▶  VER EVENTOS")
        self.event_worker = None

    def events_sync_progress(self, message):

        if self.events_window is not None and self.events_window.isVisible():
            self.events_window.status.setText(message)

    def events_sync_finished(self, videos):

        self.sync_label.setText(f"SYNC: OK {time.strftime('%H:%M:%S')}")
        self.sync_label.setToolTip("")

        if self.events_window is not None:
            status = self.events_window.status.text()
            self.events_window.set_videos(videos)
            if self.sync_interactive:
                self.events_window.status.setText(
                    f"{status} · {len(videos)} evento(s) almacenado(s)"
                )

        self._sync_done()

    def events_sync_error(self, message):

        self.sync_label.setText("SYNC: ERROR")
        self.sync_label.setToolTip(message)
        print(f"[SYNC] {message}")

        if self.events_window is not None:
            self.events_window.status.setText("Error de conexión.")

        if self.sync_interactive:
            QMessageBox.warning(
                self,
                "Error al cargar eventos",
                "No fue posible obtener los videos desde la Raspberry Pi.\n\n"
                f"{message}\n\n"
                "Compruebe que puede ejecutar:\n"
                f"ssh {RPI_HOST}",
            )

        self._sync_done()

    # --------------------------------------------------------
    # LOGOUT
    # --------------------------------------------------------

    def logout(self):

        self.receiver.stop()
        self.logout_callback()

    # --------------------------------------------------------
    # CIERRE
    # --------------------------------------------------------

    def closeEvent(self, event):

        for timer in (self.frame_timer, self.status_timer, self.clock_timer, self.sync_timer):
            timer.stop()

        self.receiver.stop()

        if self.event_worker is not None and self.event_worker.isRunning():
            self.event_worker.wait(5000)

        event.accept()


# ============================================================
# APLICACIÓN PRINCIPAL
# ============================================================

class SecurityApp:

    def __init__(self):

        self.app = QApplication(sys.argv)
        self.app.setApplicationName("Secure Vision")

        self.login_page = LoginPage(self.show_dashboard)
        self.dashboard = None

        self.login_page.resize(900, 650)
        self.login_page.show()

    def show_dashboard(self):

        self.login_page.hide()

        self.dashboard = Dashboard(self.show_login)
        self.dashboard.resize(1100, 750)
        self.dashboard.show()

    def show_login(self):

        if self.dashboard is not None:
            self.dashboard.close()
            self.dashboard = None

        self.login_page.username.clear()
        self.login_page.password.clear()
        self.login_page.show()

    def run(self):
        sys.exit(self.app.exec())


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    application = SecurityApp()
    application.run()
