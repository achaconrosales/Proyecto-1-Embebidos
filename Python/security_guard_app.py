
import sys
import time

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QImage, QPixmap
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
)


# ============================================================
# CONFIGURACIÓN
# ============================================================

UDP_PORT = 5000

USERNAME = "guardia"
PASSWORD = "1234"

GSTREAMER_PIPELINE = f"""
udpsrc port={UDP_PORT}
caps="application/x-rtp,media=video,clock-rate=90000,encoding-name=JPEG,payload=26"
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


# ============================================================
# GSTREAMER
# ============================================================

class GStreamerReceiver:
    def __init__(self):
        Gst.init(None)

        self.pipeline = None
        self.appsink = None

        self.connected = False
        self.last_frame_time = 0

    def start(self):
        self.stop()

        try:
            print("Iniciando pipeline GStreamer...")

            self.pipeline = Gst.parse_launch(GSTREAMER_PIPELINE)

            self.appsink = self.pipeline.get_by_name("videosink")

            if self.appsink is None:
                print("ERROR: No se encontró appsink")
                return False

            self.pipeline.set_state(Gst.State.PLAYING)

            self.connected = True

            print("Pipeline iniciado correctamente")

            return True

        except Exception as e:
            print(f"Error iniciando GStreamer: {e}")

            self.pipeline = None
            self.appsink = None
            self.connected = False

            return False

    def get_frame(self):
        if self.appsink is None:
            return None

        try:
            # Importante:
            # En algunas versiones de PyGObject no existe
            # appsink.try_pull_sample().
            #
            # Por eso usamos la señal de GStreamer directamente.
            sample = self.appsink.emit("try-pull-sample", 0)

            if sample is None:
                return None

            buffer = sample.get_buffer()

            if buffer is None:
                return None

            caps = sample.get_caps()

            if caps is None:
                return None

            structure = caps.get_structure(0)

            width = structure.get_value("width")
            height = structure.get_value("height")

            success, map_info = buffer.map(Gst.MapFlags.READ)

            if not success:
                return None

            try:
                data = map_info.data

                # RGB = 3 bytes por pixel
                image = QImage(
                    data,
                    width,
                    height,
                    width * 3,
                    QImage.Format.Format_RGB888,
                ).copy()

                self.last_frame_time = time.time()

                return image

            finally:
                buffer.unmap(map_info)

        except Exception as e:
            print(f"Error recibiendo frame: {e}")
            return None

    def stop(self):
        if self.pipeline is not None:
            try:
                self.pipeline.set_state(Gst.State.NULL)
            except Exception:
                pass

        self.pipeline = None
        self.appsink = None
        self.connected = False

    def is_receiving(self):
        if self.last_frame_time == 0:
            return False

        # Consideramos que está recibiendo si llegó
        # un frame durante los últimos 2 segundos.
        return (time.time() - self.last_frame_time) < 2.0


# ============================================================
# LOGIN
# ============================================================

class LoginPage(QWidget):

    def __init__(self, login_callback):
        super().__init__()

        self.login_callback = login_callback

        self.setStyleSheet("""
            QWidget {
                background-color: #0b1118;
                color: #ffffff;
                font-family: Arial;
            }

            QLineEdit {
                background-color: #151e28;
                border: 1px solid #344454;
                border-radius: 6px;
                padding: 12px;
                color: white;
                font-size: 14px;
            }

            QLineEdit:focus {
                border: 1px solid #2ea3ff;
            }

            QPushButton {
                background-color: #1769aa;
                border: none;
                border-radius: 6px;
                padding: 12px;
                color: white;
                font-weight: bold;
                font-size: 14px;
            }

            QPushButton:hover {
                background-color: #2185d0;
            }
        """)

        main_layout = QVBoxLayout()
        main_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        title = QLabel("SECURE VISION")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)

        title.setStyleSheet("""
            font-size: 30px;
            font-weight: bold;
            color: #2ea3ff;
        """)

        subtitle = QLabel("CENTRO DE MONITOREO")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)

        subtitle.setStyleSheet("""
            font-size: 13px;
            color: #8b9aaa;
            letter-spacing: 2px;
        """)

        panel = QFrame()
        panel.setMaximumWidth(400)

        panel.setStyleSheet("""
            QFrame {
                background-color: #101923;
                border: 1px solid #263646;
                border-radius: 10px;
            }
        """)

        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(35, 35, 35, 35)
        panel_layout.setSpacing(15)

        login_title = QLabel("Identificación del guardia")
        login_title.setStyleSheet("""
            font-size: 18px;
            font-weight: bold;
            color: white;
        """)

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
        self.error_label.setStyleSheet("""
            color: #ff5c5c;
            font-size: 12px;
        """)

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
        username = self.username.text()
        password = self.password.text()

        if username == USERNAME and password == PASSWORD:
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

        self.setStyleSheet("""
            QWidget {
                background-color: #080e14;
                color: white;
                font-family: Arial;
            }

            QPushButton {
                background-color: #172432;
                border: 1px solid #2b3d4f;
                border-radius: 5px;
                padding: 9px 15px;
                color: white;
            }

            QPushButton:hover {
                background-color: #213448;
            }
        """)

        self.build_ui()

        # Timer para obtener frames de GStreamer.
        self.frame_timer = QTimer()
        self.frame_timer.timeout.connect(self.update_frame)
        self.frame_timer.start(15)

        # Timer para actualizar información.
        self.status_timer = QTimer()
        self.status_timer.timeout.connect(self.update_status)
        self.status_timer.start(500)

        # Timer del reloj.
        self.clock_timer = QTimer()
        self.clock_timer.timeout.connect(self.update_clock)
        self.clock_timer.start(1000)

        self.start_receiver()

    # --------------------------------------------------------
    # UI
    # --------------------------------------------------------

    def build_ui(self):

        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(20, 20, 20, 20)
        main_layout.setSpacing(15)

        # ================= HEADER =================

        header = QHBoxLayout()

        title = QLabel("SECURE VISION")
        title.setStyleSheet("""
            font-size: 24px;
            font-weight: bold;
            color: #2ea3ff;
        """)

        system = QLabel("SISTEMA DE MONITOREO")
        system.setStyleSheet("""
            color: #8291a1;
            font-size: 12px;
        """)

        self.clock_label = QLabel("--:--:--")
        self.clock_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.clock_label.setStyleSheet("""
            color: #b9c5d0;
            font-size: 14px;
            font-weight: bold;
        """)

        header.addWidget(title)
        header.addSpacing(15)
        header.addWidget(system)

        header.addStretch()

        header.addWidget(self.clock_label)

        main_layout.addLayout(header)

        # ================= INFO BAR =================

        info_bar = QFrame()

        info_bar.setStyleSheet("""
            QFrame {
                background-color: #101923;
                border: 1px solid #263646;
                border-radius: 6px;
            }
        """)

        info_layout = QHBoxLayout(info_bar)

        camera_label = QLabel("CÁMARA: FRONT")

        self.status_label = QLabel("● OFFLINE")

        self.status_label.setStyleSheet("""
            color: #ff5c5c;
            font-weight: bold;
        """)

        port_label = QLabel(f"UDP PORT: {UDP_PORT}")

        codec_label = QLabel("CODEC: JPEG/RTP")

        for label in [camera_label, port_label, codec_label]:
            label.setStyleSheet("""
                color: #aab7c4;
                font-size: 12px;
            """)

        info_layout.addWidget(camera_label)
        info_layout.addStretch()
        info_layout.addWidget(port_label)
        info_layout.addSpacing(20)
        info_layout.addWidget(codec_label)
        info_layout.addSpacing(20)
        info_layout.addWidget(self.status_label)

        main_layout.addWidget(info_bar)

        # ================= VIDEO =================

        self.video_frame = QLabel()

        self.video_frame.setMinimumSize(640, 480)

        self.video_frame.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.video_frame.setText(
            "ESPERANDO TRANSMISIÓN...\n\n"
            "UDP :5000"
        )

        self.video_frame.setStyleSheet("""
            QLabel {
                background-color: #020508;
                border: 1px solid #263646;
                border-radius: 6px;
                color: #596979;
                font-size: 16px;
            }
        """)

        main_layout.addWidget(self.video_frame, 1)

        # ================= CONTROLS =================

        controls = QHBoxLayout()

        self.reconnect_button = QPushButton("↻  RECONECTAR")
        self.reconnect_button.clicked.connect(self.reconnect)

        self.capture_button = QPushButton("▣  CAPTURAR EVIDENCIA")
        self.capture_button.clicked.connect(self.capture_frame)

        self.logout_button = QPushButton("CERRAR SESIÓN")
        self.logout_button.clicked.connect(self.logout)

        controls.addWidget(self.reconnect_button)
        controls.addWidget(self.capture_button)

        controls.addStretch()

        controls.addWidget(self.logout_button)

        main_layout.addLayout(controls)

        # ================= FOOTER =================

        footer = QLabel(
            "Secure Vision  •  Sistema de monitoreo UDP/RTP"
        )

        footer.setStyleSheet("""
            color: #526272;
            font-size: 11px;
        """)

        footer.setAlignment(Qt.AlignmentFlag.AlignCenter)

        main_layout.addWidget(footer)

        self.setLayout(main_layout)

    # --------------------------------------------------------
    # GStreamer
    # --------------------------------------------------------

    def start_receiver(self):

        success = self.receiver.start()

        if not success:
            self.status_label.setText("● ERROR")
            self.status_label.setStyleSheet("""
                color: #ff5c5c;
                font-weight: bold;
            """)

    def reconnect(self):

        self.status_label.setText("● RECONECTANDO...")
        self.status_label.setStyleSheet("""
            color: #ffaa00;
            font-weight: bold;
        """)

        QApplication.processEvents()

        self.receiver.stop()

        time.sleep(0.2)

        self.start_receiver()

    # --------------------------------------------------------
    # Frame
    # --------------------------------------------------------

    def update_frame(self):

        image = self.receiver.get_frame()

        if image is None:
            return

        self.last_image = image

        pixmap = QPixmap.fromImage(image)

        pixmap = pixmap.scaled(
            self.video_frame.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )

        self.video_frame.setPixmap(pixmap)

    # --------------------------------------------------------
    # Estado
    # --------------------------------------------------------

    def update_status(self):

        if self.receiver.is_receiving():

            self.status_label.setText("● LIVE")

            self.status_label.setStyleSheet("""
                color: #36d399;
                font-weight: bold;
            """)

        else:

            self.status_label.setText("● OFFLINE")

            self.status_label.setStyleSheet("""
                color: #ff5c5c;
                font-weight: bold;
            """)

    # --------------------------------------------------------
    # Reloj
    # --------------------------------------------------------

    def update_clock(self):

        current_time = time.strftime("%H:%M:%S")

        self.clock_label.setText(current_time)

    # --------------------------------------------------------
    # Captura
    # --------------------------------------------------------

    def capture_frame(self):

        if self.last_image is None:

            QMessageBox.warning(
                self,
                "Sin transmisión",
                "No hay ningún frame disponible para capturar.",
            )

            return

        filename = time.strftime(
            "evidence_%Y%m%d_%H%M%S.png"
        )

        success = self.last_image.save(filename)

        if success:

            QMessageBox.information(
                self,
                "Evidencia guardada",
                f"La captura fue guardada como:\n\n{filename}",
            )

        else:

            QMessageBox.warning(
                self,
                "Error",
                "No fue posible guardar la captura.",
            )

    # --------------------------------------------------------
    # Logout
    # --------------------------------------------------------

    def logout(self):

        self.receiver.stop()

        self.logout_callback()

    # --------------------------------------------------------
    # Cierre
    # --------------------------------------------------------

    def closeEvent(self, event):

        self.receiver.stop()

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
