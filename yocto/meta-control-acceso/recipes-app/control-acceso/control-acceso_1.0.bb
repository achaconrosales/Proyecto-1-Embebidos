SUMMARY = "Secure Vision: control de acceso con deteccion de rostros y streaming RTP/JPEG"
DESCRIPTION = "Emisor que corre en la Raspberry Pi: detecta rostros, acciona la \
cerradura por GPIO, graba un clip AVI por evento (maximo 10) y transmite el \
video en vivo por RTP/UDP a la PC del guardia."
LICENSE = "CLOSED"

SRC_URI = " \
    file://control_acceso.py \
    file://control-acceso.service \
    file://control-acceso.default \
    file://journald-persistente.conf \
    file://haarcascade_frontalface_default.xml \
    file://verificar-pipeline.sh \
"

# Scarthgap desempaca los file:// en WORKDIR; Styhead y posteriores en UNPACKDIR.
S = "${@d.getVar('UNPACKDIR') or d.getVar('WORKDIR')}"

inherit systemd allarch

SYSTEMD_SERVICE:${PN} = "control-acceso.service"
SYSTEMD_AUTO_ENABLE = "enable"

do_install() {
    install -d ${D}${bindir}
    install -m 0755 ${S}/control_acceso.py ${D}${bindir}/control_acceso.py
    install -m 0755 ${S}/verificar-pipeline.sh ${D}${bindir}/verificar-pipeline.sh

    install -d ${D}${systemd_system_unitdir}
    install -m 0644 ${S}/control-acceso.service ${D}${systemd_system_unitdir}/control-acceso.service

    install -d ${D}${sysconfdir}/default
    install -m 0644 ${S}/control-acceso.default ${D}${sysconfdir}/default/control-acceso

    install -d ${D}${sysconfdir}/systemd/journald.conf.d
    install -m 0644 ${S}/journald-persistente.conf \
        ${D}${sysconfdir}/systemd/journald.conf.d/10-control-acceso.conf

    # Clasificador Haar incluido en la capa: no depende de como empaquete OpenCV
    # sus datos (conserva la licencia de Intel dentro del archivo).
    install -d ${D}${datadir}/control-acceso
    install -m 0644 ${S}/haarcascade_frontalface_default.xml ${D}${datadir}/control-acceso/
}

FILES:${PN} += " \
    ${systemd_system_unitdir}/control-acceso.service \
    ${sysconfdir}/systemd/journald.conf.d \
    ${datadir}/control-acceso \
"
CONFFILES:${PN} = "${sysconfdir}/default/control-acceso"

# Checklist G1: cada plugin que usa el pipeline, por subpaquete.
# Elementos -> paquete:
#   queue, tee, fakesink, tracers ........ gstreamer1.0
#   v4l2src .............................. gstreamer1.0-plugins-good-video4linux2
#   videoconvert, videoscale ............. gstreamer1.0-plugins-base-videoconvertscale
#   videorate ............................ gstreamer1.0-plugins-base-videorate
#   appsink, appsrc ...................... gstreamer1.0-plugins-base-app
#   jpegenc, jpegdec ..................... gstreamer1.0-plugins-good-jpeg
#   rtpjpegpay ........................... gstreamer1.0-plugins-good-rtp
#   udpsink .............................. gstreamer1.0-plugins-good-udp
#   avimux, avidemux ..................... gstreamer1.0-plugins-good-avi
RDEPENDS:${PN} = " \
    python3-core \
    python3-modules \
    python3-numpy \
    python3-opencv \
    python3-pygobject \
    python3-gpiod \
    gobject-introspection \
    gstreamer1.0 \
    gstreamer1.0-plugins-good-video4linux2 \
    gstreamer1.0-plugins-base-videoconvertscale \
    gstreamer1.0-plugins-base-videorate \
    gstreamer1.0-plugins-base-app \
    gstreamer1.0-plugins-good-jpeg \
    gstreamer1.0-plugins-good-rtp \
    gstreamer1.0-plugins-good-udp \
    gstreamer1.0-plugins-good-avi \
"
