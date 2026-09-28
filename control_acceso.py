import cv2
import time
import gpiod
import os
from gpiod.line import Direction, Value

# Configuracion de pines GPIO
PIN_ROJO = 17
PIN_VERDE = 27

# Inicializar GPIO
lineas = gpiod.request_lines(
    "/dev/gpiochip0",
    consumer="acceso",
    config={
        PIN_ROJO: gpiod.LineSettings(direction=Direction.OUTPUT, output_value=Value.ACTIVE),
        PIN_VERDE: gpiod.LineSettings(direction=Direction.OUTPUT, output_value=Value.INACTIVE)
    }
)

class PinWrapper:
    def __init__(self, pin):
        self.pin = pin
    def set_value(self, val):
        estado = Value.ACTIVE if val == 1 else Value.INACTIVE
        lineas.set_value(self.pin, estado)
    def release(self):
        pass 

line_rojo = PinWrapper(PIN_ROJO)
line_verde = PinWrapper(PIN_VERDE)

cascade_path = '/usr/share/opencv4/haarcascades/haarcascade_frontalface_default.xml'
face_cascade = cv2.CascadeClassifier(cascade_path)

# Pipeline: Camara -> OpenCV y Camara -> UDP (Streaming en vivo constante)
pipeline = (
    "v4l2src device=/dev/video0 ! videoconvert ! video/x-raw,width=640,height=480,framerate=30/1 ! tee name=t "
    "t. ! queue ! videoconvert ! video/x-raw,format=BGR ! appsink drop=true max-buffers=1 "
    "t. ! queue ! videoconvert ! jpegenc ! rtpjpegpay ! udpsink host=10.249.212.93 port=5000"
)

cap = cv2.VideoCapture(pipeline, cv2.CAP_GSTREAMER)

# Ruta unificada donde se guardaran todos los clips
DIR_EVENTOS = "/home/root/eventos"
os.makedirs(DIR_EVENTOS, exist_ok=True)

def limpiar_respaldos_viejos(directorio, max_archivos=100):
    """Mantiene solo los archivos mas recientes en la SD local."""
    archivos = sorted(
        [os.path.join(directorio, f) for f in os.listdir(directorio) if f.endswith('.avi')],
        key=os.path.getmtime
    )
    while len(archivos) > max_archivos:
        archivo_viejo = archivos.pop(0)
        os.remove(archivo_viejo)
        print(f"[*] Respaldo antiguo eliminado por rotacion: {archivo_viejo}")

try:
    print("Iniciando sistema de control de acceso...")
    while True:
        ret, frame = cap.read()
        if not ret:
            print("Error capturando video")
            break

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        faces = face_cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(30, 30))

        if len(faces) > 0:
            print("[!] Rostro detectado. Abriendo puerta y grabando evento...")
            line_rojo.set_value(0)
            line_verde.set_value(1)

            # Se asegura que todos caigan exactamente en DIR_EVENTOS
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            filename = f"{DIR_EVENTOS}/acceso_{timestamp}.avi"
            
            fourcc = cv2.VideoWriter_fourcc(*'MJPG')
            out = cv2.VideoWriter(filename, fourcc, 30.0, (640, 480))

            start_time = time.time()
            
            # Grabar exactamente 5 segundos
            while (time.time() - start_time) < 5.0:
                ret, frame = cap.read()
                if ret:
                    out.write(frame)
            
            out.release()
            print(f"[*] Grabacion finalizada: {filename}")

            line_rojo.set_value(1)
            line_verde.set_value(0)

            # Ejecutar limpieza local para proteger la memoria de la SD
            limpiar_respaldos_viejos(DIR_EVENTOS, max_archivos=100)
            
        else:
            line_rojo.set_value(1)
            line_verde.set_value(0)

except KeyboardInterrupt:
    print("Apagando sistema...")
finally:
    print("Liberando camara y pines...")
    cap.release()
    try:
        lineas.set_value(PIN_ROJO, Value.INACTIVE)
        lineas.set_value(PIN_VERDE, Value.INACTIVE)
    except Exception:
        pass
    lineas.release()
    print("Sistema detenido limpiamente.")