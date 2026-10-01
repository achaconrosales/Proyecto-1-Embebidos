#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
control_acceso.py - Emisor del sistema Secure Vision (Raspberry Pi 4).

Qué hace:
  * Captura la cámara USB con un único pipeline de GStreamer que alimenta
    tres ramas: análisis (detección de rostros), red (RTP/JPEG por UDP hacia
    la PC del guardia) y grabación (clips de eventos).
  * Cualquier rostro detectado abre la puerta durante DURACION_EVENTO segundos
    y graba un clip AVI (MJPEG) de esa misma duración.
  * Guarda como máximo MAX_EVENTOS clips: al guardar el siguiente se borra el
    más viejo (FIFO). La PC del guardia los descarga por SSH.
  * Lleva una bitácora CSV persistente con cada acceso, clip y error.

Decisiones de ciclo de vida (checklist E y H):
  * Error de pipeline o cámara desconectada -> se cierra la puerta, se registra
    y el proceso termina con código 1; systemd lo reinicia (Restart=on-failure).
  * SIGTERM/SIGINT (systemctl stop) -> se cierra la puerta, se envía EOS al clip
    en curso y se espera a que avimux cierre el archivo, luego EOS al pipeline.
  * Si el ciclo de decisión se cuelga, un hilo vigilante cierra la puerta y
    systemd (WatchdogSec) mata y reinicia el proceso.

La configuración se lee de variables de entorno (ver /etc/default/control-acceso).
"""

import argparse
import csv
import datetime
import logging
import os
import shutil
import signal
import socket
import sys
import threading
import time

import numpy as np
import cv2
import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402


# ============================================================
# CONFIGURACIÓN
# ============================================================

def _env(nombre, defecto):
    valor = os.environ.get(nombre)
    return defecto if valor is None or valor.strip() == "" else valor.strip()


def _env_int(nombre, defecto):
    return int(_env(nombre, str(defecto)))


def _env_float(nombre, defecto):
    return float(_env(nombre, str(defecto)))


class Config:
    def __init__(self):
        # Red: a dónde se transmite el video en vivo
        self.pc_host = _env("SV_PC_HOST", "10.249.212.93")
        self.udp_port = _env_int("SV_UDP_PORT", 5000)

        # Cámara
        #   /dev/videoN      cámara real
        #   test             videotestsrc (pruebas sin cámara)
        #   imagen:<ruta>    imagen fija como fuente viva (pruebas de detección)
        self.dispositivo = _env("SV_DISPOSITIVO", "/dev/video0")
        self.formato_camara = _env("SV_FORMATO_CAMARA", "raw")   # raw | mjpeg
        self.ancho = _env_int("SV_ANCHO", 640)
        self.alto = _env_int("SV_ALTO", 480)
        self.fps = _env_int("SV_FPS", 30)

        # Codificación JPEG (rama de red y de grabación)
        self.jpegenc = _env("SV_JPEGENC", "jpegenc")             # jpegenc | v4l2jpegenc
        self.jpeg_calidad = _env_int("SV_JPEG_CALIDAD", 85)

        # Análisis
        self.analisis_fps = _env_int("SV_ANALISIS_FPS", 10)
        self.analisis_ancho = _env_int("SV_ANALISIS_ANCHO", 320)
        self.analisis_alto = int(round(self.alto * self.analisis_ancho / self.ancho))
        self.cara_minima = _env_int("SV_CARA_MINIMA", 24)        # px en la imagen de análisis
        self.cascade = _env("SV_CASCADE", "")

        # Decisión (H2): si detectar tarda más que esto, se niega el acceso
        self.tiempo_max_decision = _env_float("SV_TIEMPO_MAX_DECISION", 0.5)
        # Vigilante (H3): si el ciclo no late en este tiempo, se cierra la puerta
        self.tiempo_max_ciclo = _env_float("SV_TIEMPO_MAX_CICLO", 2.0)
        # Sin cuadros durante este tiempo = cámara caída (E2)
        self.timeout_camara = _env_float("SV_TIMEOUT_CAMARA", 3.0)

        # Eventos
        self.dir_eventos = _env("SV_DIR_EVENTOS", "/home/root/eventos")
        self.bitacora = _env("SV_BITACORA", "/home/root/bitacora_accesos.csv")
        self.max_eventos = _env_int("SV_MAX_EVENTOS", 10)
        self.duracion_evento = _env_float("SV_DURACION_EVENTO", 5.0)
        self.min_libre_mb = _env_int("SV_MIN_LIBRE_MB", 100)

        # GPIO (rojo = cerrado, verde = abierto)
        self.gpio_chip = _env("SV_GPIO_CHIP", "/dev/gpiochip0")
        self.pin_rojo = _env_int("SV_PIN_ROJO", 17)
        self.pin_verde = _env_int("SV_PIN_VERDE", 27)
        self.simular_gpio = _env("SV_SIMULAR_GPIO", "0") == "1"

        # Estadísticas al journal cada N segundos
        self.intervalo_estadisticas = _env_float("SV_INTERVALO_ESTADISTICAS", 60.0)

        if self.formato_camara not in ("raw", "mjpeg"):
            raise ValueError("SV_FORMATO_CAMARA debe ser raw o mjpeg")
        if self.max_eventos < 1:
            raise ValueError("SV_MAX_EVENTOS debe ser >= 1")

    @property
    def dir_temporal(self):
        # Los clips se escriben aquí y se mueven a dir_eventos al cerrarse,
        # así la PC nunca descarga un archivo a medio escribir.
        return os.path.join(self.dir_eventos, ".grabando")


log = logging.getLogger("control_acceso")


# ============================================================
# PIPELINE
# ============================================================

def construir_pipeline(cfg, para_gst_launch=False):
    """Devuelve la descripción del pipeline principal.

    Con para_gst_launch=True los appsink se cambian por fakesink para poder
    ejecutar el mismo grafo con gst-launch-1.0 (medición de caps, latencia,
    grafo .dot) sin el programa de Python.
    """
    w, h, fps = cfg.ancho, cfg.alto, cfg.fps
    aw, ah = cfg.analisis_ancho, cfg.analisis_alto

    # ---------------- Fuente ----------------
    if cfg.dispositivo == "test":
        fuente = (f"videotestsrc is-live=true pattern=ball "
                  f"! video/x-raw,width={w},height={h},framerate={fps}/1")
        crudo = True
    elif cfg.dispositivo.startswith("imagen:"):
        ruta = cfg.dispositivo.split(":", 1)[1]
        fuente = (f'filesrc location="{ruta}" ! decodebin ! videoconvert ! videoscale '
                  f"! video/x-raw,width={w},height={h} "
                  f"! imagefreeze is-live=true ! video/x-raw,framerate={fps}/1")
        crudo = True
    elif cfg.formato_camara == "mjpeg":
        fuente = (f"v4l2src device={cfg.dispositivo} do-timestamp=true "
                  f"! image/jpeg,width={w},height={h},framerate={fps}/1")
        crudo = False
    else:
        fuente = (f"v4l2src device={cfg.dispositivo} do-timestamp=true "
                  f"! video/x-raw,width={w},height={h},framerate={fps}/1")
        crudo = True

    if para_gst_launch:
        sink_analisis = "fakesink name=analisis sync=false"
        sink_grabacion = "fakesink name=grabacion sync=false"
    else:
        sink_analisis = "appsink name=analisis sync=false drop=true max-buffers=1"
        sink_grabacion = ("appsink name=grabacion sync=false drop=false max-buffers=0 "
                          "emit-signals=true")

    # Rama de análisis: pierde cuadros a propósito (leaky) y baja a 10 fps,
    # 320x240 y escala de grises, que es lo único que necesita el detector.
    cola_analisis = ("queue name=q_analisis leaky=downstream max-size-buffers=2 "
                     "max-size-bytes=0 max-size-time=0")
    reduccion = (f"videorate drop-only=true max-rate={cfg.analisis_fps} ! videoscale ! videoconvert "
                 f"! video/x-raw,format=GRAY8,width={aw},height={ah}")

    # RTP/JPEG (RFC 2435) solo transporta YUV 4:2:0 o 4:2:2. Este filtro deja
    # pasar sin convertir lo que ya sea compatible (YUY2 de la cámara) y
    # obliga a convertir cualquier otra cosa (RGB, 4:4:4...).
    formatos_rtp = "video/x-raw,format=(string){YUY2,UYVY,I420,NV12}"
    if cfg.jpegenc == "jpegenc":
        codificador = f"{formatos_rtp} ! jpegenc quality={cfg.jpeg_calidad}"
    else:
        codificador = f"{formatos_rtp} ! {cfg.jpegenc}"

    # Rama de red: puede perder cuadros (video en vivo, lo importante es la latencia).
    rama_red = ("j. ! queue name=q_red leaky=downstream max-size-buffers=4 max-size-bytes=0 "
                "max-size-time=0 "
                f"! rtpjpegpay ! udpsink host={cfg.pc_host} port={cfg.udp_port} sync=false async=false")
    # Rama de grabación: NO pierde cuadros; 30 buffers (1 s a 30 fps) absorben jitter.
    rama_grabacion = ("j. ! queue name=q_grabacion max-size-buffers=30 max-size-bytes=0 "
                      f"max-size-time=0 ! {sink_grabacion}")

    if crudo:
        return (
            f"{fuente} ! tee name=t "
            f"t. ! {cola_analisis} ! {reduccion} ! {sink_analisis} "
            "t. ! queue name=q_codificador leaky=downstream max-size-buffers=4 max-size-bytes=0 "
            f"max-size-time=0 ! videoconvert ! {codificador} ! tee name=j "
            f"{rama_red} {rama_grabacion}"
        )
    # La cámara ya entrega JPEG: no se recodifica, solo se decodifica para analizar.
    return (
        f"{fuente} ! tee name=j "
        f"j. ! {cola_analisis} ! jpegdec ! {reduccion} ! {sink_analisis} "
        f"{rama_red} {rama_grabacion}"
    )


class ErrorPipeline(Exception):
    pass


class Estadisticas:
    """Contadores para el journal: fps reales, tiempos de decisión, callback."""

    def __init__(self):
        self._lock = threading.Lock()
        self.reiniciar()

    def reiniciar(self):
        self.t0 = time.monotonic()
        self.cuadros_camara = 0
        self.cuadros_analisis = 0
        self.deteccion_total = 0.0
        self.deteccion_max = 0.0
        self.edad_total = 0.0
        self.edad_max = 0.0
        self.callback_max = 0.0
        self.eventos = 0
        self.negados_por_tiempo = 0

    def cuadro_camara(self, dt_callback):
        with self._lock:
            self.cuadros_camara += 1
            if dt_callback > self.callback_max:
                self.callback_max = dt_callback

    def cuadro_analisis(self, dt_deteccion, edad):
        with self._lock:
            self.cuadros_analisis += 1
            self.deteccion_total += dt_deteccion
            self.deteccion_max = max(self.deteccion_max, dt_deteccion)
            self.edad_total += edad
            self.edad_max = max(self.edad_max, edad)

    def resumen(self):
        with self._lock:
            dt = max(time.monotonic() - self.t0, 1e-6)
            n = max(self.cuadros_analisis, 1)
            texto = (
                f"estadisticas: ventana={dt:.0f}s "
                f"fps_camara={self.cuadros_camara / dt:.1f} "
                f"fps_analisis={self.cuadros_analisis / dt:.1f} "
                f"deteccion_ms_prom={1000 * self.deteccion_total / n:.1f} "
                f"deteccion_ms_max={1000 * self.deteccion_max:.1f} "
                f"captura_a_decision_ms_prom={1000 * (self.edad_total + self.deteccion_total) / n:.1f} "
                f"captura_a_decision_ms_max={1000 * (self.edad_max + self.deteccion_max):.1f} "
                f"callback_grabacion_us_max={1e6 * self.callback_max:.0f} "
                f"eventos={self.eventos} negados_por_tiempo={self.negados_por_tiempo}"
            )
            self.reiniciar()
            return texto


class Captura:
    """Pipeline principal: cámara -> análisis / red / grabación."""

    def __init__(self, cfg, al_recibir_jpeg, estadisticas):
        self.cfg = cfg
        self._al_recibir_jpeg = al_recibir_jpeg
        self._estadisticas = estadisticas
        self._avisos = {}

        descripcion = construir_pipeline(cfg)
        log.info("pipeline: %s", descripcion)
        try:
            self.pipeline = Gst.parse_launch(descripcion)
        except Exception as e:  # GLib.Error: elemento o propiedad inexistente
            raise ErrorPipeline(f"no se pudo construir el pipeline: {e}") from e

        self.analisis = self.pipeline.get_by_name("analisis")
        self.grabacion = self.pipeline.get_by_name("grabacion")
        self.grabacion.connect("new-sample", self._on_nuevo_jpeg)
        self.bus = self.pipeline.get_bus()

    # --- rama de grabación: callback en el hilo de streaming (B5) ---
    def _on_nuevo_jpeg(self, sink):
        t0 = time.monotonic()
        muestra = sink.emit("pull-sample")
        if muestra is not None:
            try:
                self._al_recibir_jpeg(muestra)
            except Exception:  # nunca propagar al hilo de GStreamer
                log.exception("error en callback de grabación")
        self._estadisticas.cuadro_camara(time.monotonic() - t0)
        return Gst.FlowReturn.OK

    def iniciar(self):
        if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            self.revisar_bus()
            raise ErrorPipeline("el pipeline no pudo pasar a PLAYING")

    def revisar_bus(self):
        """Atiende error, warning y eos (E1). Se llama en cada vuelta del ciclo."""
        while True:
            msg = self.bus.pop_filtered(
                Gst.MessageType.ERROR | Gst.MessageType.WARNING | Gst.MessageType.EOS)
            if msg is None:
                return
            origen = msg.src.get_name() if msg.src else "?"
            if msg.type == Gst.MessageType.ERROR:
                err, dbg = msg.parse_error()
                raise ErrorPipeline(f"{origen}: {err.message} ({dbg})")
            if msg.type == Gst.MessageType.EOS:
                raise ErrorPipeline(f"{origen}: fin de flujo inesperado (EOS)")
            if msg.type == Gst.MessageType.WARNING:
                w, dbg = msg.parse_warning()
                clave = (origen, w.message)
                ahora = time.monotonic()
                # Un udpsink sin ruta puede avisar 30 veces por segundo: se limita.
                if ahora - self._avisos.get(clave, -1e9) > 60:
                    self._avisos[clave] = ahora
                    log.warning("aviso de GStreamer %s: %s (%s)", origen, w.message, dbg)

    def tiempo_actual(self):
        """Tiempo de ejecución del pipeline (mismo reloj que los PTS)."""
        reloj = self.pipeline.get_clock()
        if reloj is None:
            return None
        return reloj.get_time() - self.pipeline.get_base_time()

    def siguiente_cuadro(self, timeout_s):
        """Devuelve (imagen_gris, edad_s) o None si no llegó cuadro."""
        muestra = self.analisis.emit("try-pull-sample", int(timeout_s * Gst.SECOND))
        if muestra is None:
            return None
        buf = muestra.get_buffer()
        estructura = muestra.get_caps().get_structure(0)
        ancho = estructura.get_value("width")
        alto = estructura.get_value("height")
        ok, info = buf.map(Gst.MapFlags.READ)
        if not ok:
            return None
        try:
            paso = info.size // alto  # stride real (puede incluir relleno)
            imagen = (np.frombuffer(info.data, dtype=np.uint8, count=paso * alto)
                      .reshape(alto, paso)[:, :ancho].copy())
        finally:
            buf.unmap(info)
        edad = 0.0
        ahora = self.tiempo_actual()
        if ahora is not None and buf.pts != Gst.CLOCK_TIME_NONE:
            edad = max(0.0, (ahora - buf.pts) / Gst.SECOND)
        return imagen, edad

    def detener(self, timeout_s=3.0):
        """EOS a todas las ramas y espera a que lleguen a los sinks (B6)."""
        try:
            self.pipeline.send_event(Gst.Event.new_eos())
            self.bus.timed_pop_filtered(int(timeout_s * Gst.SECOND),
                                        Gst.MessageType.EOS | Gst.MessageType.ERROR)
        finally:
            self.pipeline.set_state(Gst.State.NULL)


# ============================================================
# GRABACIÓN DE EVENTOS
# ============================================================

class Grabacion:
    """Un clip: appsrc (JPEG ya codificado) -> avimux -> filesink.

    Los JPEG salen del mismo jpegenc que alimenta la red, así que grabar no
    cuesta una segunda codificación. Al terminar se envía EOS y se espera a
    que avimux escriba el índice: el archivo queda reproducible (E4).
    """

    def __init__(self, ruta_tmp, ruta_final, duracion_s):
        self.ruta_tmp = ruta_tmp
        self.ruta_final = ruta_final
        self._duracion_ns = int(duracion_s * Gst.SECOND)
        self._lock = threading.Lock()
        self._cerrada = False
        self._pts0 = None
        self.cuadros = 0
        self.completa = False

        self.pipeline = Gst.Pipeline.new("clip")
        self.src = Gst.ElementFactory.make("appsrc", "src")
        mux = Gst.ElementFactory.make("avimux", "mux")
        sink = Gst.ElementFactory.make("filesink", "archivo")
        if self.src is None or mux is None or sink is None:
            raise ErrorPipeline("faltan los plugins app o avi para grabar")
        self.src.set_property("is-live", True)
        self.src.set_property("format", Gst.Format.TIME)
        self.src.set_property("block", False)
        self.src.set_property("max-bytes", 32 * 1024 * 1024)
        sink.set_property("location", ruta_tmp)
        sink.set_property("sync", False)
        sink.set_property("async", False)
        for e in (self.src, mux, sink):
            self.pipeline.add(e)
        # El appsrc todavía no tiene caps: si se enlaza con link() a secas,
        # avimux puede darle su pad de audio. Se pide el pad de video.
        pedir = getattr(mux, "request_pad_simple", None) or mux.get_request_pad
        pad_video = pedir("video_%u")
        if (pad_video is None
                or self.src.get_static_pad("src").link(pad_video) != Gst.PadLinkReturn.OK
                or not mux.link(sink)):
            raise ErrorPipeline("no se pudo enlazar appsrc ! avimux ! filesink")

    def arrancar(self):
        if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            self.pipeline.set_state(Gst.State.NULL)
            raise ErrorPipeline(f"no se pudo abrir {self.ruta_tmp}")

    def empujar(self, muestra):
        """Se llama desde el hilo de streaming; debe retornar rápido."""
        with self._lock:
            if self._cerrada or self.completa:
                return
            buf = muestra.get_buffer()
            pts = buf.pts
            if pts == Gst.CLOCK_TIME_NONE:
                pts = int(time.monotonic() * Gst.SECOND)
            if self._pts0 is None:
                self._pts0 = pts
                self.src.set_property("caps", muestra.get_caps())
            relativo = pts - self._pts0
            if relativo >= self._duracion_ns:
                self.completa = True
                return
            nuevo = Gst.Buffer.new_wrapped(buf.extract_dup(0, buf.get_size()))
            nuevo.pts = relativo
            nuevo.duration = buf.duration
            if self.src.emit("push-buffer", nuevo) == Gst.FlowReturn.OK:
                self.cuadros += 1

    def finalizar(self, timeout_s=5.0):
        """Devuelve (ok, detalle). Si ok, el clip ya está en ruta_final."""
        with self._lock:
            self._cerrada = True
        self.src.emit("end-of-stream")
        msg = self.pipeline.get_bus().timed_pop_filtered(
            int(timeout_s * Gst.SECOND), Gst.MessageType.EOS | Gst.MessageType.ERROR)
        error = None
        if msg is None:
            error = "avimux no confirmó el cierre (timeout de EOS)"
        elif msg.type == Gst.MessageType.ERROR:
            error = msg.parse_error()[0].message
        self.pipeline.set_state(Gst.State.NULL)

        if error is None and self.cuadros == 0:
            error = "clip sin cuadros"
        if error is not None:
            _borrar_silencioso(self.ruta_tmp)
            return False, error
        try:
            with open(self.ruta_tmp, "rb") as f:
                os.fsync(f.fileno())
            os.replace(self.ruta_tmp, self.ruta_final)
            _fsync_directorio(os.path.dirname(self.ruta_final))
        except OSError as e:
            _borrar_silencioso(self.ruta_tmp)
            return False, f"no se pudo mover el clip: {e}"
        return True, f"{self.cuadros} cuadros"


def _borrar_silencioso(ruta):
    try:
        os.remove(ruta)
    except OSError:
        pass


def _fsync_directorio(ruta):
    try:
        fd = os.open(ruta, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


class Grabador:
    """Administra el clip activo, la rotación (máx. N clips) y el espacio."""

    def __init__(self, cfg, bitacora):
        self.cfg = cfg
        self.bitacora = bitacora
        self._lock = threading.Lock()
        self._lock_archivos = threading.Lock()
        self._actual = None
        self._hilos = []
        os.makedirs(cfg.dir_eventos, exist_ok=True)
        os.makedirs(cfg.dir_temporal, exist_ok=True)
        self._limpiar_temporales()
        self.rotar()

    def _limpiar_temporales(self):
        # Restos de un apagado brusco (kill -9, corte de energía)
        for nombre in os.listdir(self.cfg.dir_temporal):
            _borrar_silencioso(os.path.join(self.cfg.dir_temporal, nombre))
            self.bitacora.registrar("clip_incompleto_descartado", "apagado brusco previo", nombre)

    def clips(self):
        """Clips terminados, del más viejo al más nuevo."""
        rutas = []
        for nombre in os.listdir(self.cfg.dir_eventos):
            if nombre.startswith("acceso_") and nombre.endswith(".avi"):
                ruta = os.path.join(self.cfg.dir_eventos, nombre)
                try:
                    rutas.append((os.path.getmtime(ruta), nombre, ruta))
                except OSError:
                    pass
        rutas.sort()
        return [r[2] for r in rutas]

    def rotar(self):
        """FIFO: deja como máximo max_eventos clips."""
        with self._lock_archivos:
            clips = self.clips()
            while len(clips) > self.cfg.max_eventos:
                viejo = clips.pop(0)
                _borrar_silencioso(viejo)
                log.info("clip rotado (límite %d): %s", self.cfg.max_eventos, viejo)
                self.bitacora.registrar("clip_rotado", f"limite={self.cfg.max_eventos}",
                                        os.path.basename(viejo))

    def _asegurar_espacio(self):
        """E5: si queda poco disco, borra los clips más viejos. False si no alcanza."""
        minimo = self.cfg.min_libre_mb * 1024 * 1024
        with self._lock_archivos:
            clips = self.clips()
            while shutil.disk_usage(self.cfg.dir_eventos).free < minimo and clips:
                viejo = clips.pop(0)
                _borrar_silencioso(viejo)
                self.bitacora.registrar("clip_borrado_por_espacio",
                                        f"libre<{self.cfg.min_libre_mb}MB", os.path.basename(viejo))
            return shutil.disk_usage(self.cfg.dir_eventos).free >= minimo

    def iniciar(self, nombre):
        """Crea el clip. Devuelve el nombre o None si no se pudo grabar."""
        try:
            if not self._asegurar_espacio():
                log.error("sin espacio para grabar %s", nombre)
                self.bitacora.registrar("sin_grabacion", "disco lleno", nombre)
                return None
            g = Grabacion(os.path.join(self.cfg.dir_temporal, nombre),
                          os.path.join(self.cfg.dir_eventos, nombre),
                          self.cfg.duracion_evento)
            g.arrancar()
        except (ErrorPipeline, OSError) as e:
            log.error("no se pudo iniciar la grabación: %s", e)
            self.bitacora.registrar("sin_grabacion", str(e), nombre)
            return None
        with self._lock:
            self._actual = g
        return nombre

    def empujar(self, muestra):
        g = self._actual
        if g is not None:
            g.empujar(muestra)

    def terminar(self, esperar=False):
        with self._lock:
            g, self._actual = self._actual, None
        if g is None:
            return
        hilo = threading.Thread(target=self._finalizar, args=(g,), name="cierre-clip")
        hilo.start()
        self._hilos = [h for h in self._hilos if h.is_alive()] + [hilo]
        if esperar:
            hilo.join(timeout=10)

    def _finalizar(self, g):
        ok, detalle = g.finalizar()
        nombre = os.path.basename(g.ruta_final)
        if ok:
            log.info("clip guardado: %s (%s)", g.ruta_final, detalle)
            self.bitacora.registrar("clip_guardado", detalle, nombre)
            self.rotar()
        else:
            log.error("clip descartado %s: %s", nombre, detalle)
            self.bitacora.registrar("clip_descartado", detalle, nombre)

    def esperar(self, timeout_s=10.0):
        limite = time.monotonic() + timeout_s
        for h in self._hilos:
            h.join(timeout=max(0.0, limite - time.monotonic()))


# ============================================================
# PUERTA (GPIO)
# ============================================================

class Puerta:
    """Rojo encendido = cerrado. Verde encendido = abierto.

    La cerradura debe cablearse a la salida VERDE activa en alto: si el
    proceso muere o el Pi se apaga, la línea queda en bajo = cerrada.
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self._lock = threading.Lock()
        self.abierta = False
        self._lineas = None
        if cfg.simular_gpio:
            log.warning("GPIO simulado (SV_SIMULAR_GPIO=1): no se acciona ninguna salida")
            return
        import gpiod
        from gpiod.line import Direction, Value
        self._valor = Value
        self._lineas = gpiod.request_lines(
            cfg.gpio_chip,
            consumer="control-acceso",
            config={
                cfg.pin_rojo: gpiod.LineSettings(direction=Direction.OUTPUT,
                                                 output_value=Value.ACTIVE),
                cfg.pin_verde: gpiod.LineSettings(direction=Direction.OUTPUT,
                                                  output_value=Value.INACTIVE),
            },
        )

    def _escribir(self, rojo, verde):
        if self._lineas is None:
            return
        v = self._valor
        self._lineas.set_values({
            self.cfg.pin_rojo: v.ACTIVE if rojo else v.INACTIVE,
            self.cfg.pin_verde: v.ACTIVE if verde else v.INACTIVE,
        })

    def abrir(self):
        with self._lock:
            self._escribir(rojo=False, verde=True)
            self.abierta = True

    def cerrar(self):
        with self._lock:
            try:
                self._escribir(rojo=True, verde=False)
            except Exception:
                log.exception("no se pudo escribir el GPIO al cerrar")
            self.abierta = False

    def liberar(self):
        self.cerrar()
        with self._lock:
            if self._lineas is not None:
                try:
                    self._lineas.release()
                except Exception:
                    pass
                self._lineas = None


# ============================================================
# BITÁCORA (H6)
# ============================================================

class Bitacora:
    """CSV persistente: fecha;evento;detalle;archivo. Se rota al pasar 1 MB."""

    CAMPOS = ["fecha", "evento", "detalle", "archivo"]

    def __init__(self, ruta, max_bytes=1_000_000):
        self.ruta = ruta
        self.max_bytes = max_bytes
        self._lock = threading.Lock()
        directorio = os.path.dirname(ruta)
        if directorio:
            os.makedirs(directorio, exist_ok=True)

    def registrar(self, evento, detalle="", archivo=""):
        fecha = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
        with self._lock:
            try:
                if os.path.exists(self.ruta) and os.path.getsize(self.ruta) > self.max_bytes:
                    os.replace(self.ruta, self.ruta + ".1")
                nuevo = not os.path.exists(self.ruta)
                with open(self.ruta, "a", newline="", encoding="utf-8") as f:
                    escritor = csv.writer(f)
                    if nuevo:
                        escritor.writerow(self.CAMPOS)
                    escritor.writerow([fecha, evento, detalle, archivo])
                    f.flush()
                    os.fsync(f.fileno())
            except OSError as e:
                # Con el disco lleno la bitácora puede fallar; el journal sigue.
                log.error("no se pudo escribir la bitácora: %s", e)


# ============================================================
# SYSTEMD Y VIGILANTE
# ============================================================

def sd_notify(mensaje):
    """Protocolo sd_notify sin depender de python3-systemd."""
    direccion = os.environ.get("NOTIFY_SOCKET")
    if not direccion:
        return
    if direccion.startswith("@"):
        direccion = "\0" + direccion[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(direccion)
            s.sendall(mensaje.encode())
    except OSError:
        pass


class Vigilante(threading.Thread):
    """Cierra la puerta si el ciclo de decisión deja de latir (H2, H3).

    También hace de respaldo del temporizador de cierre: si por cualquier
    motivo la puerta sigue abierta 1 s después de lo previsto, la cierra.
    """

    def __init__(self, puerta, bitacora, limite_s):
        super().__init__(name="vigilante", daemon=True)
        self.puerta = puerta
        self.bitacora = bitacora
        self.limite_s = limite_s
        self.detener = threading.Event()
        self._latido = time.monotonic()
        self.cierre_previsto = None
        self._disparado = False

    def latir(self):
        self._latido = time.monotonic()

    def run(self):
        while not self.detener.wait(0.2):
            ahora = time.monotonic()
            atraso = ahora - self._latido
            if atraso > self.limite_s:
                if not self._disparado:
                    self._disparado = True
                    self.puerta.cerrar()
                    log.error("el ciclo de decisión no responde (%.1f s): puerta cerrada", atraso)
                    self.bitacora.registrar("ciclo_colgado", f"{atraso:.1f}s sin latido")
            else:
                self._disparado = False
            previsto = self.cierre_previsto
            if self.puerta.abierta and previsto is not None and ahora > previsto + 1.0:
                self.puerta.cerrar()
                log.error("cierre de respaldo por el vigilante")
                self.bitacora.registrar("cierre_respaldo", "el ciclo no cerró a tiempo")


# ============================================================
# PROGRAMA PRINCIPAL
# ============================================================

def cargar_cascade(cfg):
    candidatos = [
        cfg.cascade,
        "/usr/share/control-acceso/haarcascade_frontalface_default.xml",
        "/usr/share/opencv4/haarcascades/haarcascade_frontalface_default.xml",
        "/usr/share/opencv/haarcascades/haarcascade_frontalface_default.xml",
    ]
    datos = getattr(cv2, "data", None)
    if datos is not None and getattr(datos, "haarcascades", None):
        candidatos.append(os.path.join(datos.haarcascades, "haarcascade_frontalface_default.xml"))
    for ruta in candidatos:
        if ruta and os.path.isfile(ruta):
            cascade = cv2.CascadeClassifier(ruta)
            if not cascade.empty():
                log.info("clasificador: %s", ruta)
                return cascade
    raise ErrorPipeline("no se encontró haarcascade_frontalface_default.xml")


def ejecutar(cfg):
    detener = threading.Event()

    def al_recibir_senal(signum, _frame):
        log.info("señal %s recibida: deteniendo", signal.Signals(signum).name)
        detener.set()

    signal.signal(signal.SIGTERM, al_recibir_senal)
    signal.signal(signal.SIGINT, al_recibir_senal)

    bitacora = Bitacora(cfg.bitacora)
    puerta = None
    captura = None
    grabador = None
    vigilante = None
    codigo = 0
    estadisticas = Estadisticas()

    try:
        cascade = cargar_cascade(cfg)
        puerta = Puerta(cfg)           # arranca cerrada: rojo=1, verde=0
        grabador = Grabador(cfg, bitacora)
        vigilante = Vigilante(puerta, bitacora, cfg.tiempo_max_ciclo)
        captura = Captura(cfg, grabador.empujar, estadisticas)
        captura.iniciar()

        # Esperar el primer cuadro antes de declararse listo
        limite = time.monotonic() + 10.0
        primero = None
        while primero is None and time.monotonic() < limite and not detener.is_set():
            captura.revisar_bus()
            primero = captura.siguiente_cuadro(0.5)
        if primero is None and not detener.is_set():
            raise ErrorPipeline("la cámara no entregó ningún cuadro en 10 s")

        vigilante.start()
        sd_notify("READY=1")
        bitacora.registrar("inicio", f"destino={cfg.pc_host}:{cfg.udp_port} "
                                     f"max_eventos={cfg.max_eventos}")
        log.info("sistema de control de acceso en marcha")

        ultimo_cuadro = time.monotonic()
        ultimo_watchdog = 0.0
        ultimas_estadisticas = time.monotonic()
        ultimo_negado = 0.0
        cierre_en = None
        clip_actual = None

        while not detener.is_set():
            ahora = time.monotonic()
            vigilante.latir()
            if ahora - ultimo_watchdog >= 1.0:
                sd_notify("WATCHDOG=1")
                ultimo_watchdog = ahora
            if ahora - ultimas_estadisticas >= cfg.intervalo_estadisticas:
                log.info(estadisticas.resumen())
                ultimas_estadisticas = ahora

            captura.revisar_bus()

            # Fin del evento: cerrar puerta y cerrar el clip
            if cierre_en is not None and ahora >= cierre_en:
                puerta.cerrar()
                vigilante.cierre_previsto = None
                grabador.terminar()
                bitacora.registrar("cierre", "fin del evento", clip_actual or "")
                cierre_en = None
                clip_actual = None

            resultado = captura.siguiente_cuadro(0.2)
            if resultado is None:
                if time.monotonic() - ultimo_cuadro > cfg.timeout_camara:
                    raise ErrorPipeline(
                        f"sin cuadros de la cámara durante {cfg.timeout_camara:.0f} s")
                continue
            ultimo_cuadro = time.monotonic()

            if cierre_en is not None:
                continue  # evento en curso: la puerta ya está abierta

            gris, edad = resultado
            t0 = time.monotonic()
            gris = cv2.equalizeHist(gris)
            caras = cfg_detectar(cascade, gris, cfg)
            dt = time.monotonic() - t0
            estadisticas.cuadro_analisis(dt, edad)

            if len(caras) == 0:
                continue

            # H2: decisión vencida -> se niega (no se abre)
            if edad + dt > cfg.tiempo_max_decision:
                estadisticas.negados_por_tiempo += 1
                if time.monotonic() - ultimo_negado > 10:
                    ultimo_negado = time.monotonic()
                    log.warning("decisión vencida (%.0f ms): acceso negado", 1000 * (edad + dt))
                    bitacora.registrar("decision_vencida", f"{1000 * (edad + dt):.0f}ms")
                continue

            # Cualquier rostro abre: se registra el evento y se graba
            marca = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            nombre = f"acceso_{marca}.avi"
            puerta.abrir()
            cierre_en = time.monotonic() + cfg.duracion_evento
            vigilante.cierre_previsto = cierre_en
            clip_actual = grabador.iniciar(nombre)
            estadisticas.eventos += 1
            log.info("rostro detectado (%d): puerta abierta, %s", len(caras),
                     f"grabando {clip_actual}" if clip_actual else "SIN grabación")
            bitacora.registrar("acceso", f"rostros={len(caras)} "
                                         f"decision_ms={1000 * (edad + dt):.0f}",
                               clip_actual or "")

    except ErrorPipeline as e:
        log.error("error: %s", e)
        bitacora.registrar("error", str(e))
        codigo = 1
    except Exception as e:  # cualquier otra cosa: cerrar y dejar que systemd reinicie
        log.exception("error inesperado")
        bitacora.registrar("error", repr(e))
        codigo = 1
    finally:
        sd_notify("STOPPING=1")
        if vigilante is not None:
            vigilante.detener.set()   # el ciclo ya no late durante el apagado
        if puerta is not None:
            puerta.cerrar()
        if grabador is not None:
            grabador.terminar(esperar=True)   # EOS al clip en curso: queda reproducible
            grabador.esperar()
        if captura is not None:
            captura.detener()
        if puerta is not None:
            puerta.liberar()
        bitacora.registrar("apagado", f"codigo={codigo}")
        log.info("sistema detenido (código %d)", codigo)
    return codigo


def cfg_detectar(cascade, gris, cfg):
    return cascade.detectMultiScale(
        gris, scaleFactor=1.1, minNeighbors=5,
        minSize=(cfg.cara_minima, cfg.cara_minima))


def main():
    parser = argparse.ArgumentParser(description="Emisor Secure Vision")
    parser.add_argument("--pipeline", action="store_true",
                        help="imprime el pipeline que usa el servicio y termina")
    parser.add_argument("--gst-launch", action="store_true",
                        help="imprime el pipeline con fakesink para gst-launch-1.0")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stdout)
    try:
        cfg = Config()
    except ValueError as e:
        log.error("configuración inválida: %s", e)
        return 2

    if args.pipeline or args.gst_launch:
        print(construir_pipeline(cfg, para_gst_launch=args.gst_launch))
        return 0

    Gst.init(None)
    return ejecutar(cfg)


if __name__ == "__main__":
    sys.exit(main())
