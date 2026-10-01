# Síntesis e instalación del sistema operativo con Yocto Project

**Proyecto Secure Vision: control de acceso sobre Raspberry Pi 4**
Taller de Sistemas Embebidos · Instituto Tecnológico de Costa Rica · II Semestre 2026

Este documento explica, paso a paso, cómo construir con Yocto Project la imagen de Linux del sistema de control de acceso y cómo instalarla en una Raspberry Pi 4.

La imagen parte de `core-image-base` (rama **Wrynose**) y agrega:

- La aplicación `control_acceso.py` como servicio de systemd. Captura la cámara, detecta rostros, acciona los GPIO, graba clips y transmite el video.
- GStreamer, OpenCV, Python y libgpiod, con solo los paquetes que la aplicación necesita.
- Servidor SSH con contraseña de root, conexión Wi-Fi automática y estado seguro de los GPIO desde el arranque.

La idea general del sistema está en [`docs/diseno.md`](docs/diseno.md).

## Estructura del repositorio

```
Proyecto-1-Embebidos/
├── README.md                      este tutorial
├── .gitignore
├── yocto/
│   ├── meta-control-acceso/       capa del proyecto (recetas de la aplicación y del Wi-Fi)
│   └── conf/
│       ├── local.conf             configuración usada para compilar
│       └── bblayers.conf          capas activas
├── pc-guardia/
│   ├── security_guard_app.py      aplicación del guardia
│   └── requirements.txt
├── docs/
│   ├── diseno.md                  idea general del sistema
│   └── evidencias/                mediciones traídas de la Raspberry
└── bitacoras/                     bitácoras individuales
```

Solo se versiona lo propio del proyecto (~1 MB). Las capas públicas (openembedded-core, meta-openembedded, meta-raspberrypi…) se descargan aparte, y `build/` (decenas de GB) no se sube.

> **Antes de empezar**
>
> - Este tutorial asume que ya tienen instalado un árbol de Yocto (rama Wrynose) con `bitbake`, `openembedded-core`, `meta-yocto`, `meta-openembedded` y `meta-raspberrypi` dentro de una carpeta `layers/`.
> - **Las rutas y direcciones son las del equipo donde se desarrolló el proyecto.** Ajústenlas a su caso:
>
> | En este documento | Qué es | Cámbienlo por |
> |---|---|---|
> | `/mnt/taller/taller-yocto` | Carpeta del árbol de Yocto (también en `yocto/conf/bblayers.conf`) | La ruta de su instalación |
> | `/mnt/taller/swapfile` | Archivo de swap | Cualquier disco con espacio |
> | `10.208.1.36` | IP de la PC del guardia | La IP de su PC (`hostname -I`) |
> | `10.208.1.181` | IP de la Raspberry (asignada por DHCP) | La IP que reciba su Raspberry |
> | `NOMBRE_RED` / `CLAVE_RED` | Red Wi-Fi | Su red y su clave (paso 6) |

---

## Contenido

1. [Requisitos](#2-requisitos)
2. [Preparar la máquina de compilación](#3-preparar-la-máquina-de-compilación)
3. [Obtener las capas](#4-obtener-las-capas)
4. [Activar el entorno y agregar las capas](#5-activar-el-entorno-y-agregar-las-capas)
5. [Configurar la red Wi-Fi y la IP de la PC](#6-configurar-la-red-wi-fi-y-la-ip-de-la-pc)
6. [Configurar `local.conf`](#7-configurar-localconf)
7. [Compilar la imagen](#8-compilar-la-imagen)
8. [Grabar la tarjeta microSD](#9-grabar-la-tarjeta-microsd)
9. [Primer arranque y verificación](#10-primer-arranque-y-verificación)
10. [Uso con la PC del guardia](#11-uso-con-la-pc-del-guardia)
11. [Pruebas y resultados](#12-pruebas-y-resultados)
12. [Modificar la imagen después](#13-modificar-la-imagen-después)
13. [Solución de problemas](#14-solución-de-problemas)
14. [Referencia: capa `meta-control-acceso`](#15-referencia-capa-meta-control-acceso)

---

## 1. Requisitos

**Máquina de compilación**

- Linux x86-64 (se usó Ubuntu).
- Unos **100 GB libres** en disco.
- **RAM + swap de al menos 16 GB en total** (ver 3.2).
- Conexión a internet: la primera compilación descarga varios GB de código fuente.

**Hardware**

- Raspberry Pi 4 y tarjeta microSD de 4 GB o más (se recomiendan 8 GB), clase A1/U1 o superior.
- Cámara USB UVC (640×480 a 30 fps).
- LED rojo en **GPIO17** y cerradura (o LED verde) en **GPIO27**, con su etapa de potencia.
- Lector de tarjetas microSD en la máquina de compilación.

---

## 2. Preparar la máquina de compilación

### 2.1 Paquetes del host

```bash
sudo apt update
sudo apt install gawk wget git diffstat unzip texinfo gcc build-essential chrpath \
  socat cpio python3 python3-pip python3-pexpect xz-utils debianutils iputils-ping \
  python3-git python3-jinja2 python3-subunit zstd liblz4-tool file locales libacl1 \
  bmap-tools tmux
sudo locale-gen en_US.UTF-8
```

### 2.2 Memoria de intercambio (swap)

Compilar paquetes grandes (OpenCV, gcc, el kernel) consume mucha RAM. Con 6,7 GB de RAM y 512 MB de swap, el sistema mató a BitBake por falta de memoria. Se agregaron 16 GB de swap en el disco de trabajo:

```bash
sudo fallocate -l 16G /mnt/taller/swapfile
sudo chmod 600 /mnt/taller/swapfile
sudo mkswap /mnt/taller/swapfile
sudo swapon /mnt/taller/swapfile
free -h                     # la fila Swap debe mostrar ~16G
```

Este swap dura hasta que se reinicie la computadora. Para que sea permanente, agreguen `/mnt/taller/swapfile none swap sw 0 0` a `/etc/fstab`.

---

## 3. Obtener las capas

El árbol se organizó con la estructura de `bitbake-setup` de Wrynose:

```
/mnt/taller/taller-yocto/
├── build/                      conf/, tmp/, sstate-cache/
└── layers/
    ├── bitbake/
    ├── openembedded-core/      capa base (core)
    ├── meta-yocto/             distribución poky
    ├── meta-openembedded/      meta-oe (OpenCV, libgpiod) y meta-python (python3-gpiod)
    ├── meta-raspberrypi/       BSP de la Raspberry Pi
    └── meta-control-acceso/    capa del proyecto
```

Enlacen la capa del repositorio dentro de `layers/`. Con un enlace simbólico, BitBake compila exactamente lo que está en el repo y no hay dos copias que se desincronicen. Cambien `REPO` por la ruta donde clonaron el repositorio:

```bash
REPO=~/ruta/al/Proyecto-1-Embebidos
cd /mnt/taller/taller-yocto
rm -rf layers/meta-control-acceso                 # si antes había una copia
ln -s "$REPO/yocto/meta-control-acceso" layers/meta-control-acceso
ls -l layers/meta-control-acceso                  # debe apuntar al repo
```

Comprueben que todas las capas estén en la misma rama:

```bash
for d in layers/*/; do echo "$d: $(git -C "$d" branch --show-current 2>/dev/null)"; done
```

`meta-openembedded` y `meta-raspberrypi` deben decir `wrynose`. En `bitbake`, `openembedded-core` y `meta-yocto` puede salir vacío, porque están en un commit fijo.

---

## 4. Activar el entorno y agregar las capas

**Salgan de cualquier entorno virtual de Python** (`deactivate`), porque BitBake debe usar el Python del sistema.

```bash
cd /mnt/taller/taller-yocto
source layers/openembedded-core/oe-init-build-env build
```

La terminal queda dentro de `build/`. Este `source` se repite en cada terminal nueva.

Agreguen las capas (las que ya estén activas se ignoran):

```bash
bitbake-layers add-layer ../layers/meta-openembedded/meta-oe
bitbake-layers add-layer ../layers/meta-openembedded/meta-python
bitbake-layers add-layer ../layers/meta-raspberrypi
bitbake-layers add-layer ../layers/meta-control-acceso
```

Verifiquen:

```bash
bitbake-layers show-layers
bitbake-layers show-recipes control-acceso python3-gpiod
```

- `show-layers` debe listar `core`, `yocto`, `openembedded-layer`, `meta-python`, `raspberrypi` y `control-acceso`.
- `show-recipes` debe mostrar **una sola** receta `control-acceso` (de `meta-control-acceso`) y `python3-gpiod` de `meta-python`. Si otra capa tiene una receta `control-acceso` vieja, retírenla para que no haya dos.

---

## 5. Configurar la red Wi-Fi y la IP de la PC

Esto se hace **antes de compilar**, para que la imagen ya arranque conectada.

**Red Wi-Fi** (WPA2/WPA3-Personal, solo contraseña). Editen el archivo y pongan el nombre exacto de la red y su clave:

```bash
nano ../layers/meta-control-acceso/recipes-connectivity/wifi-config/files/wpa_supplicant-wlan0.conf
```

```
ctrl_interface=/var/run/wpa_supplicant
country=CR
network={
    ssid="NOMBRE_RED"
    psk="CLAVE_RED"
}
```

Como `layers/meta-control-acceso` es un enlace, este archivo es el del repositorio. La clave queda en texto plano, así que para que Git no la suba, márquenlo una vez desde la raíz del repo:

```bash
git update-index --skip-worktree yocto/meta-control-acceso/recipes-connectivity/wifi-config/files/wpa_supplicant-wlan0.conf
```

En el repositorio queda la versión con `NOMBRE_RED` y `CLAVE_RED`.

**IP de la PC del guardia**, a la que la Raspberry envía el video en vivo. Para verla en la PC: `hostname -I`, primera dirección.

```bash
sed -i 's/^SV_PC_HOST=.*/SV_PC_HOST=10.208.1.36/' \
  ../layers/meta-control-acceso/recipes-app/control-acceso/files/control-acceso.default
grep SV_PC_HOST ../layers/meta-control-acceso/recipes-app/control-acceso/files/control-acceso.default
```

---

## 6. Configurar `local.conf`

Respalden el actual y copien el del proyecto:

```bash
cp conf/local.conf conf/local.conf.respaldo
cp "$REPO/yocto/conf/local.conf" conf/local.conf
```

(`REPO` es la variable del paso 4; si abrieron otra terminal, defínanla de nuevo.)

Líneas que se agregaron o cambiaron respecto a la plantilla:

| Línea | Para qué |
|---|---|
| `MACHINE ??= "raspberrypi4-64"` | Compilar para Raspberry Pi 4 en 64 bits |
| `EXTRA_IMAGE_FEATURES += "allow-root-login ssh-server-openssh"` | Servidor SSH con login de root. Sin contraseñas vacías ni autologin |
| `INHERIT += "extrausers"`, `SV_ROOT_PASSWD`, `EXTRA_USERS_PARAMS` | Fijan la contraseña de root al construir (hash SHA-512, con cada `$` escapado como `\$`) |
| `IMAGE_INSTALL:append = " control-acceso"` | La aplicación y sus dependencias exactas |
| `IMAGE_INSTALL:append = " linux-firmware-rpidistro-bcm43455 wpa-supplicant iw kernel-modules"` | Soporte Wi-Fi |
| `IMAGE_INSTALL:append = " wifi-config"` | Conexión automática a la red configurada en el paso 6 |
| `IMAGE_INSTALL:append = " v4l-utils libgpiod-tools sysstat"` | Herramientas de diagnóstico (cámara, GPIO, disco) |
| `IMAGE_ROOTFS_EXTRA_SPACE = "1048576"` | 1 GB libre para clips y registros |
| `RPI_EXTRA_CONFIG = "\ngpio=17=op,dh\ngpio=27=op,dl\n"` | Estado de los GPIO desde el arranque: rojo encendido y cerradura en bajo (cerrada) |
| `MACHINE_FEATURES:append = " vc4graphics wifi"` | Gráficos VC4 y Wi-Fi |
| `DISTRO_FEATURES:append = " systemd usrmerge"`, `VIRTUAL-RUNTIME_init_manager = "systemd"` | systemd como sistema de arranque |
| `LICENSE_FLAGS_ACCEPTED += "synaptics-killswitch"` | Acepta la licencia del firmware Wi-Fi |
| `BB_NUMBER_THREADS = "2"`, `PARALLEL_MAKE = "-j 4"` | Paralelismo que cabe en 6–8 GB de RAM |

**Para cambiar la contraseña de root:** generen el hash con `openssl passwd -6 'NUEVA_CONTRASEÑA'` y péguenlo en `SV_ROOT_PASSWD`, escapando cada `$` como `\$`.

**Importante:** la última línea de `local.conf` debe terminar en salto de línea. Si se agrega texto con `echo ... >>` a un archivo que no lo tiene, las dos líneas se pegan (ver 14).

---

## 7. Compilar la imagen

Láncenlo dentro de `tmux`, para que no se detenga si se cierra la ventana:

```bash
tmux new -s yocto
cd /mnt/taller/taller-yocto
source layers/openembedded-core/oe-init-build-env build
bitbake core-image-base 2>&1 | tee build.log
```

- **Salir de `tmux` sin detener la compilación:** `Ctrl + B` y luego `D`.
- **Volver a verla:** `tmux attach -t yocto`.
- **No** usen `Ctrl + C` ni `exit` dentro de `tmux`, y no dejen que la computadora se suspenda.

Qué esperar:

- Al inicio, "Checking sstate mirror object availability" tarda unos minutos.
- Luego `Running task X of ~7000`. La primera compilación tomó varias horas en una laptop de 4 núcleos.
- Si se interrumpe, vuelvan a correr `bitbake core-image-base`: retoma desde donde quedó.
- Los `WARNING` de `do_sbom_cve_check` (CVE sin parche) y el de `/home/root` son informativos.

Resultado correcto:

```
NOTE: Tasks Summary: Attempted 7028 tasks of which ... didn't need to be rerun and all succeeded.
```

La imagen queda en:

```bash
ls -lh tmp/deploy/images/raspberrypi4-64/core-image-base-raspberrypi4-64.rootfs.wic.*
```

| Archivo | Tamaño |
|---|---|
| `core-image-base-raspberrypi4-64.rootfs.wic.bz2` | ~156 MB comprimida |
| `core-image-base-raspberrypi4-64.rootfs.wic.bmap` | Imagen de ~2,3 GB, con ~536 MB de datos reales |

---

## 8. Grabar la tarjeta microSD

### 8.1 Instalar `bmaptool`

```bash
sudo apt install -y bmap-tools
```

### 8.2 Identificar la tarjeta

Inserten la microSD y listen los discos:

```bash
lsblk
```

Ejemplo real con una tarjeta de 32 GB:

```
NAME        MAJ:MIN RM   SIZE RO TYPE MOUNTPOINTS
sda           8:0    0 476.9G  0 disk
├─sda1        8:1    0   128M  0 part
└─sda2        8:2    0 476.8G  0 part /mnt/taller      ← disco de trabajo (Yocto): NO TOCAR
sdb           8:16   1  29.8G  0 disk                  ← la microSD
├─sdb1        8:17   1   512M  0 part
└─sdb2        8:18   1  29.3G  0 part
nvme0n1     259:0    0 238.5G  0 disk                  ← disco del sistema: NO TOCAR
└─nvme0n1p7 259:7    0  50.4G  0 part /
```

La microSD es el disco que:

- aparece al insertarla,
- tiene un tamaño parecido al de la tarjeta (una de "32 GB" da 29,8G),
- tiene `RM` = `1` (extraíble).

En este ejemplo es **`sdb`**. Las particiones que ya traiga se van a borrar. **Si eligen el disco equivocado, se borra.**

### 8.3 Grabar la imagen

Cambien `sdb` por su tarjeta si es otra:

```bash
cd /mnt/taller/taller-yocto/build/tmp/deploy/images/raspberrypi4-64/
sudo umount /dev/sdb1 /dev/sdb2 2>/dev/null
sudo bmaptool copy core-image-base-raspberrypi4-64.rootfs.wic.bz2 /dev/sdb
sync
```

`bmaptool` usa el archivo `.bmap` que está al lado de la imagen y solo escribe los bloques con datos. Salida esperada:

```
bmaptool: info: discovered bmap file 'core-image-base-raspberrypi4-64.rootfs.wic.bmap'
bmaptool: info: block map format version 2.0
bmaptool: info: 559309 blocks of size 4096 (2.1 GiB), mapped 130866 blocks (511.2 MiB or 23.4%)
bmaptool: info: copying image '...wic.bz2' to block device '/dev/sdb' using bmap file '...wic.bmap'
bmaptool: info: 100% copied
bmaptool: info: synchronizing '/dev/sdb'
bmaptool: info: copying time: 2m 1.7s, copying speed 4.2 MiB/sec
```


### 8.4 Verificar y expulsar

```bash
sync
lsblk /dev/sdb
sudo eject /dev/sdb
```

Deben quedar dos particiones nuevas:

```
NAME   MAJ:MIN RM  SIZE RO TYPE MOUNTPOINTS
sdb      8:16   1 29.8G  0 disk
├─sdb1   8:17   1  130M  0 part         ← arranque (kernel, firmware, config.txt)
└─sdb2   8:18   1    2G  0 part         ← sistema (aplicación, Wi-Fi, espacio para clips)
```

El resto de la tarjeta queda sin usar; es normal. Después de `eject` ya pueden sacarla.

---

## 9. Primer arranque y verificación

### 9.1 Conexiones

- Tarjeta microSD en la Raspberry.
- Cámara USB en un puerto **negro (USB 2.0)**. Los azules (USB 3.0) pueden interferir con el Wi-Fi de 2,4 GHz.
- LED rojo en GPIO17 y cerradura en GPIO27.
- Alimentación: la Raspberry arranca sola.

### 9.2 Encontrar la Raspberry en la red

La IP la asigna el router por DHCP. Desde la PC:

```bash
ping -c 2 10.208.1.181                         # IP esperada
sudo nmap -sn 10.208.1.0/24                     # si no responde, buscarla en la red
```

También pueden conectar monitor y teclado, entrar como `root` y correr `ip -4 addr show wlan0`.

### 9.3 Entrar por SSH

```bash
ssh-keygen -R 10.208.1.181        # solo si antes se usó otra imagen en esa IP
ssh root@10.208.1.181             # pide la contraseña de root definida en local.conf
```

### 9.4 Verificar la imagen

Dentro de la Raspberry:

```bash
systemctl status control-acceso                  # active (running)
grep SV_PC_HOST /etc/default/control-acceso      # SV_PC_HOST=10.208.1.36
v4l2-ctl --list-devices                          # la cámara en /dev/video0
v4l2-ctl -d /dev/video0 --list-formats-ext       # modos de la cámara (640x480 @ 30 fps)
grep ^gpio= /boot/config.txt                     # gpio=17=op,dh y gpio=27=op,dl
gst-inspect-1.0 --version                        # versión de GStreamer
verificar-pipeline.sh todo                       # resumen: versiones, plugins, GPIO, temperatura
journalctl -u control-acceso -f                  # registro del servicio (Ctrl+C para salir)
```

Si la cámara solo ofrece MJPG a 640×480 (y no YUYV), cambien el modo:

```bash
sed -i 's/^SV_FORMATO_CAMARA=.*/SV_FORMATO_CAMARA=mjpeg/' /etc/default/control-acceso
systemctl restart control-acceso
```

---

## 10. Uso con la PC del guardia

### 10.1 Crear el entorno de Python

Desde la raíz del repositorio:

```bash
cd "$REPO"

# 1. Dependencias del sistema (GStreamer y PyGObject se instalan con apt, no con pip)
sudo apt install -y python3-venv python3-gi gir1.2-gstreamer-1.0 \
  gstreamer1.0-plugins-base gstreamer1.0-plugins-good openssh-client

# 2. Crear el entorno CON acceso a los paquetes del sistema (necesario para "gi")
python3 -m venv --system-site-packages .venv

# 3. Activarlo e instalar lo de requirements.txt
source .venv/bin/activate
pip install -r pc-guardia/requirements.txt
```

`--system-site-packages` es necesario: PyGObject (`gi`, el puente entre Python y GStreamer) viene del sistema y no se instala bien con `pip`. Sin esa opción, la aplicación falla con `ModuleNotFoundError: No module named 'gi'`.


### 10.2 Configurar la IP de la Raspberry

La IP de la Raspberry la asigna el router. Con la Raspberry encendida, se ve de qué dirección llega el video:

```bash
sudo tcpdump -n -i any -c 1 udp port 5000
```

Ejemplo: `IP 10.208.1.62.49566 > 10.208.1.36.5000` → la Raspberry es `10.208.1.62` (la dirección de la izquierda, sin el último número).

Dejarla fija en la aplicación:

```bash
sed -i 's/root@[0-9.]*"/root@10.208.1.62"/' pc-guardia/security_guard_app.py
grep -m1 "^RPI_HOST" pc-guardia/security_guard_app.py
```

O, sin modificar el archivo, al ejecutarla: `SECUREVISION_RPI_HOST=root@<IP> python3 security_guard_app.py`.

### 11.3 Ejecutar la aplicación

```bash
cd "$REPO"
source .venv/bin/activate
cd pc-guardia
python3 security_guard_app.py
```

- Usuario `guardia`, contraseña `1234`.
- El video aparece en vivo (`● LIVE`) y los clips se descargan solos cada 15 s a la carpeta `eventos/`, junto al script.
- La barra superior muestra `SYNC: OK` cuando la conexión SSH con la Raspberry funciona.
- La aplicación ya trae la contraseña SSH de la Raspberry (`RPI_SSH_PASSWORD`).


---

## 11. Pruebas y resultados

Las pruebas se ejecutan en la Raspberry con `verificar-pipeline.sh`, que viene instalado en la imagen. Los comandos que necesitan la cámara detienen el servicio unos segundos y lo vuelven a arrancar solos.

### 11.1 Cómo ejecutarlas

Desde la PC, entrar a la Raspberry:

```bash
ssh root@<IP_RASPBERRY>
```

Ya dentro, correr el bloque completo (tarda de 8 a 10 minutos; no cerrar la sesión mientras corre):

```bash
mkdir -p /home/root/evidencias && cd /home/root/evidencias
verificar-pipeline.sh todo          2>&1 | tee todo.txt
verificar-pipeline.sh versiones     2>&1 | tee versiones.txt
verificar-pipeline.sh plugins       2>&1 | tee plugins.txt
verificar-pipeline.sh hardware      2>&1 | tee hardware.txt
verificar-pipeline.sh gpio          2>&1 | tee gpio.txt
verificar-pipeline.sh caps 5        2>&1 | tee caps_salida.txt
verificar-pipeline.sh fps 10        2>&1 | tee fps_salida.txt
verificar-pipeline.sh cpu 60        2>&1 | tee cpu.txt
verificar-pipeline.sh disco 60      2>&1 | tee disco.txt
verificar-pipeline.sh carga 180     2>&1 | tee carga.txt
verificar-pipeline.sh reinicio      2>&1 | tee reinicio.txt
journalctl -u control-acceso -o cat | grep estadisticas > estadisticas.txt
cp /home/root/bitacora_accesos.csv .
exit
```

Traer los resultados a la PC, desde la raíz del repositorio:

```bash
mkdir -p docs/evidencias
scp -r root@<IP_RASPBERRY>:/home/root/evidencias/* docs/evidencias/
```

Generar la imagen del grafo del pipeline:

```bash
sudo apt install -y graphviz
dot -Tpng "$(ls docs/evidencias/dot/*PAUSED_PLAYING*.dot | head -1)" -o docs/evidencias/grafo_pipeline.png
```

### 11.2 Resultados obtenidos

**Negociación de formatos y conversiones (`caps`)**

```
== Conversiones en el grafo real (A5)
  videoscale0      GstVideoScale        YUY2 640x480 -> YUY2 320x240  CONVIERTE
  videoconvert0    GstVideoConvert      YUY2 320x240 -> GRAY8 320x240  CONVIERTE
  videoconvert1    GstVideoConvert      YUY2 640x480 -> YUY2 640x480  passthrough (no convierte)
Total: 3 elementos de conversión, 2 convierten de verdad
```

La cámara entrega YUY2. Solo la rama de análisis convierte: reduce a 320×240 y pasa a gris. La rama de JPEG recibe YUY2 directamente, sin conversión. Se generó el grafo del pipeline (`dot/…PAUSED_PLAYING.dot`), en el que cada salida de los `tee` tiene su propia `queue`.

**Cuadros por segundo reales (`fps`)**

```
Cuadros: 151 en 10 s -> 15.10 fps reales (solicitados: 30)
```

La cámara usada entrega 15 fps reales a 640×480 en formato YUY2, aunque se le piden 30. Es un límite de la cámara en ese modo: el valor no cambia con la CPU cargada (ver `carga`).

**Uso de CPU (`cpu`, 60 s)**

```
  python3              39.7 %
  python3              25.8 %
  python3              25.6 %
  python3              25.5 %
  q_codificador:s      16.3 %
  q_analisis:src        4.0 %
  q_red:src             1.6 %
  q_grabacion:src       0.8 %
  v4l2src0:src          0.3 %
  vigilante             0.1 %
TOTAL: 139.1 % de un núcleo (4 núcleos = 400 %)
```

El sistema completo usa alrededor del 35 % de la CPU (139 % de 400 %). Los hilos `python3` corresponden a la detección de rostros (OpenCV reparte el trabajo en varios hilos). La compresión JPEG (`q_codificador`) usa el 16 % de un núcleo. Las ramas de red y grabación casi no consumen.

**Escritura en la tarjeta (`disco`, 60 s)**

```
Escritura: 61.2 KB/s
Libre en la partición de eventos: 1.3G
Ocupado por los clips: 31.1M
```

La escritura es muy baja y sobra espacio: los clips ocupan 31 MB y quedan 1,3 GB libres.

**Funcionamiento bajo carga (`carga`, 180 s con 4 procesos saturando la CPU)**

```
fps_camara=15.0 fps_analisis=9.1  deteccion_ms_prom=74.7 deteccion_ms_max=144.0 captura_a_decision_ms_prom=151.2 captura_a_decision_ms_max=302.3 negados_por_tiempo=0
fps_camara=15.0 fps_analisis=9.1  deteccion_ms_prom=76.8 deteccion_ms_max=152.1 captura_a_decision_ms_prom=154.4 captura_a_decision_ms_max=329.0 negados_por_tiempo=0
fps_camara=15.0 fps_analisis=10.0 deteccion_ms_prom=75.2 deteccion_ms_max=138.1 captura_a_decision_ms_prom=151.7 captura_a_decision_ms_max=306.0 negados_por_tiempo=0
```

Con la CPU saturada, la cámara se mantiene en 15 fps (no se pierden cuadros) y el análisis sigue cerca de 10 fps. La detección se vuelve más lenta (de unos 41 ms a unos 75 ms), pero el tiempo desde la captura hasta la decisión de abrir sigue bajo el límite de 500 ms (máximo 329 ms), sin ningún acceso negado por tiempo.

**Funcionamiento normal (estadísticas del servicio, sin carga)**

```
fps_camara=15.0 fps_analisis=10.0 deteccion_ms_prom=41.4 deteccion_ms_max=53.1 captura_a_decision_ms_prom=113.1 captura_a_decision_ms_max=126.9 negados_por_tiempo=0
```

En condiciones normales, la decisión de abrir se toma en unos 113 ms desde que se capturó el cuadro.

**Reinicio automático (`reinicio`)**

```
== kill -9 961
     Active: activating (start) since Thu 2026-10-01 14:41:09 UTC; 8s ago
OK: systemd lo reinició (pid nuevo 1676)
Bitácora:
2026-10-01T14:39:44+00:00,acceso,rostros=1 decision_ms=152,acceso_20261001_143944.avi
2026-10-01T14:39:49+00:00,cierre,fin del evento,acceso_20261001_143944.avi
2026-10-01T14:39:49+00:00,clip_guardado,76 cuadros,acceso_20261001_143944.avi
2026-10-01T14:39:49+00:00,clip_rotado,limite=10,acceso_20261001_142502.avi
```

Al matar el proceso a la fuerza, systemd lo vuelve a levantar solo. La bitácora muestra también un acceso completo durante la sesión de pruebas: detección en 152 ms, apertura, cierre a los 5 s, clip guardado (76 cuadros = 5 s a 15 fps) y rotación del clip más viejo al superar el límite de 10.

**GPIO (`gpio`)**

```
gpiochip0 17    "GPIO17"                output consumer="control-acceso"
gpiochip0 27    "GPIO27"                output consumer="control-acceso"
gpio=17=op,dh
gpio=27=op,dl
```

Las dos líneas están configuradas como salida y las controla el servicio. `config.txt` fija su estado desde el arranque: rojo encendido y cerradura en bajo.

**Plugins de GStreamer (`plugins`)**

```
  OK  v4l2src      OK  tee         OK  queue       OK  videorate
  OK  videoscale   OK  videoconvert OK  appsink    OK  appsrc
  OK  jpegenc      OK  jpegdec     OK  rtpjpegpay  OK  udpsink
  OK  avimux       OK  avidemux    OK  fakesink
  (disponible v4l2jpegenc: codificador JPEG por hardware)
```

La imagen trae todos los elementos que usa el pipeline, declarados uno por uno en la receta. También está disponible el codificador JPEG por hardware de la Raspberry (`v4l2jpegenc`), que se puede activar con `SV_JPEGENC=v4l2jpegenc` en `/etc/default/control-acceso`.

### 11.3 Resumen

| Prueba | Resultado |
|---|---|
| Conversiones | Solo convierte la rama de análisis; la de JPEG recibe la imagen sin conversión |
| Cuadros por segundo | 15 fps reales (límite de la cámara en YUY2) |
| CPU | ~35 % del total |
| Escritura en la SD | 61 KB/s; 1,3 GB libres |
| Bajo carga | Sin pérdida de cuadros; decisión ≤ 329 ms; 0 accesos negados |
| Decisión en uso normal | ~113 ms promedio |
| Reinicio | systemd lo recupera solo |
| GPIO | Configurados desde el arranque y controlados por el servicio |
| Plugins | Todos presentes |

---

## 12. Modificar la imagen después

**Sin recompilar** (se edita en la Raspberry y se reinicia el servicio):

```bash
nano /etc/default/control-acceso        # IP de la PC, cámara, límites, GPIO
systemctl restart control-acceso
```

**Recompilando** (cambios en recetas, paquetes o en la configuración inicial): editen los archivos de la capa o de `local.conf` y vuelvan a correr `bitbake core-image-base`. Solo se rehacen las tareas afectadas, así que tarda minutos. Luego regraben la tarjeta (paso 9).

Regrabar la tarjeta borra los clips, la bitácora y los cambios hechos en la Raspberry. Respalden antes lo necesario.

---

## 13. Solución de problemas

| Síntoma | Causa | Solución |
|---|---|---|
| `Layer 'control-acceso' depends on layer 'meta-python', but this layer is not enabled` | `meta-python` no está activa | `bitbake-layers add-layer ../layers/meta-openembedded/meta-python` |
| `Nothing RPROVIDES 'python3-gpiod'` | Las bindings de libgpiod v2 están en `meta-python` | Agregar `meta-python` |
| `Nothing RPROVIDES 'python3-csv'` (u otro módulo de Python) | En Wrynose esos módulos no tienen paquete propio | La receta usa `python3-modules` |
| `Variable VOLATILE_LOG_DIR is obsolete` | Variable eliminada en ramas recientes; `/var/log` ya es persistente | Borrar esa línea de `local.conf` |
| `Nothing RPROVIDES 'linux-firmware-rpidistro-bcm43455' ... not listed in your LICENSE_FLAGS_ACCEPTED` | Un `echo >>` pegó otra línea al final de `LICENSE_FLAGS_ACCEPTED` | Separar las líneas (revisar con `tail -n 3 conf/local.conf`) |
| La terminal se cierra sola durante la compilación | Falta de memoria (`journalctl -k \| grep -i oom` muestra `Killed process ... (Cooker)`) | Swap de 16 GB (3.2) y `BB_NUMBER_THREADS = "2"`, `PARALLEL_MAKE = "-j 4"` |
| `No reply from server in 30s` o `Resource temporarily unavailable` | Quedó un servidor de BitBake viejo tras una interrupción | `pkill -9 -f bitbake-server`, `rm -f bitbake.lock bitbake.sock` y relanzar. No borra el avance |
| `ls: cannot access 'build/init-build-env'` | Esa estructura no trae el script en `build/` | `source layers/openembedded-core/oe-init-build-env build` |
| La Raspberry no se conecta al Wi-Fi | Nombre o clave mal escritos | En la Raspberry: `journalctl -u wpa_supplicant@wlan0 -n 20` |
| `REMOTE HOST IDENTIFICATION HAS CHANGED` al hacer `ssh` | Se regrabó la tarjeta (huella SSH nueva) | `ssh-keygen -R <IP>`; en la PC también borrar `known_hosts_rpi` |
| No llega video a la PC | `SV_PC_HOST` no es la IP de la PC, o hay un firewall | Corregir `/etc/default/control-acceso`; en la PC, `sudo ufw allow 5000/udp` |

---

## 14. Referencia: capa `meta-control-acceso`

Ubicada en `yocto/meta-control-acceso/`:

```
meta-control-acceso/
├── conf/layer.conf
├── recipes-app/control-acceso/
│   ├── control-acceso_1.0.bb
│   └── files/
│       ├── control_acceso.py                 → /usr/bin/
│       ├── control-acceso.service            → servicio systemd (habilitado)
│       ├── control-acceso.default            → /etc/default/control-acceso
│       ├── journald-persistente.conf         → /etc/systemd/journald.conf.d/
│       ├── haarcascade_frontalface_default.xml → /usr/share/control-acceso/
│       └── verificar-pipeline.sh             → /usr/bin/
└── recipes-connectivity/wifi-config/
    ├── wifi-config_1.0.bb
    └── files/
        ├── wpa_supplicant-wlan0.conf         → /etc/wpa_supplicant/
        └── 25-wlan.network                   → /etc/systemd/network/
```

**`layer.conf`** declara la capa con dependencias de `core`, `openembedded-layer`, `meta-python` y `raspberrypi`, compatible con `scarthgap styhead walnascar whinlatter wrynose`.

**`control-acceso_1.0.bb`** instala los archivos anteriores, habilita el servicio (`inherit systemd`) y declara en `RDEPENDS` exactamente los paquetes que usa la aplicación:

| Uso | Paquetes |
|---|---|
| Captura de cámara | `gstreamer1.0-plugins-good-video4linux2` |
| Conversión, escala y tasa de cuadros | `gstreamer1.0-plugins-base-videoconvertscale`, `gstreamer1.0-plugins-base-videorate` |
| Intercambio con Python | `gstreamer1.0-plugins-base-app` |
| Compresión JPEG | `gstreamer1.0-plugins-good-jpeg` |
| Transmisión RTP/UDP | `gstreamer1.0-plugins-good-rtp`, `gstreamer1.0-plugins-good-udp` |
| Grabación AVI | `gstreamer1.0-plugins-good-avi` |
| Núcleo de GStreamer | `gstreamer1.0` |
| Python y bibliotecas | `python3-core`, `python3-modules`, `python3-numpy`, `python3-opencv`, `python3-pygobject`, `gobject-introspection` |
| GPIO | `python3-gpiod` |

**`wifi-config_1.0.bb`** instala la configuración de `wpa_supplicant` y de `systemd-networkd` (DHCP en `wlan0`) y habilita `wpa_supplicant@wlan0` al arrancar.

---
