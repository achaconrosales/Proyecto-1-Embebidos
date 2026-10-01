SUMMARY = "Configuracion Wi-Fi de la Raspberry (Secure Vision)"
DESCRIPTION = "Instala wpa_supplicant-wlan0.conf y la red de systemd-networkd, \
y habilita wpa_supplicant@wlan0 para que la placa se conecte sola al arrancar. \
Antes de compilar, poner el nombre y la clave de la red en files/wpa_supplicant-wlan0.conf."
LICENSE = "CLOSED"

SRC_URI = "file://wpa_supplicant-wlan0.conf file://25-wlan.network"
S = "${@d.getVar('UNPACKDIR') or d.getVar('WORKDIR')}"

inherit allarch

do_install() {
    install -d ${D}${sysconfdir}/wpa_supplicant
    install -m 0600 ${S}/wpa_supplicant-wlan0.conf ${D}${sysconfdir}/wpa_supplicant/
    install -d ${D}${sysconfdir}/systemd/network
    install -m 0644 ${S}/25-wlan.network ${D}${sysconfdir}/systemd/network/
    # Activa wpa_supplicant@wlan0 al arrancar
    install -d ${D}${sysconfdir}/systemd/system/multi-user.target.wants
    ln -sf ${systemd_system_unitdir}/wpa_supplicant@.service \
        ${D}${sysconfdir}/systemd/system/multi-user.target.wants/wpa_supplicant@wlan0.service
}

CONFFILES:${PN} = "${sysconfdir}/wpa_supplicant/wpa_supplicant-wlan0.conf"
RDEPENDS:${PN} = "wpa-supplicant"
