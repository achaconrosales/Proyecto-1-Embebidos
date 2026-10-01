#!/bin/sh
# shellcheck disable=SC2012
# verificar-pipeline.sh - Evidencias de la checklist de validación del pipeline
# (Secure Vision, Raspberry Pi 4). POSIX sh: funciona con busybox.
#
# Uso: verificar-pipeline.sh <comando> [argumentos]
#   Ejecute sin argumentos para ver la lista de comandos.
#
# Los comandos que abren la cámara detienen el servicio control-acceso y lo
# vuelven a arrancar al terminar (la cámara solo admite un proceso).

SERVICIO=control-acceso
DEFAULTS=${SV_DEFAULTS:-/etc/default/control-acceso}
SCRIPT=${SV_SCRIPT:-/usr/bin/control_acceso.py}
PYTHON=${SV_PYTHON:-python3}
SALIDA=${SV_SALIDA:-/home/root/evidencias}

# shellcheck source=/dev/null
[ -r "$DEFAULTS" ] && { set -a; . "$DEFAULTS"; set +a; }
DIR_EVENTOS=${SV_DIR_EVENTOS:-/home/root/eventos}
BITACORA=${SV_BITACORA:-/home/root/bitacora_accesos.csv}

mkdir -p "$SALIDA"

# ------------------------------------------------------------------ utilidades

titulo() { printf '\n== %s\n' "$*"; }
falla() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

servicio_activo() {
    command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet "$SERVICIO" 2>/dev/null
}

SERVICIO_DETENIDO=0
detener_servicio() {
    if servicio_activo; then
        echo "Deteniendo $SERVICIO (la cámara es exclusiva)..."
        systemctl stop "$SERVICIO"
        SERVICIO_DETENIDO=1
    fi
}
restaurar_servicio() {
    if [ "$SERVICIO_DETENIDO" = 1 ]; then
        echo "Arrancando de nuevo $SERVICIO..."
        systemctl start "$SERVICIO"
        SERVICIO_DETENIDO=0
    fi
}
trap restaurar_servicio EXIT
trap 'exit 130' INT TERM

pipeline_gst_launch() {
    "$PYTHON" "$SCRIPT" --gst-launch || falla "no se pudo obtener el pipeline de $SCRIPT"
}

# Ejecuta gst-launch con el pipeline del servicio durante N segundos y
# termina con EOS (-e). Variables de entorno extra se pasan antes.
# Salida estándar en $1, errores en $2.
correr_pipeline() {
    _seg=$1; _out=$2; _err=$3
    _pipe=$(pipeline_gst_launch)
    # Sin comillas a propósito: gst-launch necesita el pipeline en palabras.
    # shellcheck disable=SC2086
    gst-launch-1.0 -e -v $_pipe >"$_out" 2>"$_err" &
    _pid=$!
    sleep "$_seg"
    kill -INT "$_pid" 2>/dev/null
    wait "$_pid"
}

pid_servicio() {
    _p=$(systemctl show -p MainPID --value "$SERVICIO" 2>/dev/null)
    [ -n "$_p" ] && [ "$_p" != 0 ] && { echo "$_p"; return; }
    # Sin systemd: el proceso python cuya línea de comandos incluye el script
    for _p in $(pgrep -f control_acceso.py); do
        case "$(cat "/proc/$_p/comm" 2>/dev/null)" in
            python*) echo "$_p"; return ;;
        esac
    done
}

ticks_proceso() { # utime+stime de /proc/<pid>/stat (campos 14 y 15)
    awk '{ sub(/^.*\) /, ""); print $12 + $13 }' "/proc/$1/stat" 2>/dev/null
}

hz() { getconf CLK_TCK 2>/dev/null || echo 100; }

temperatura_mc() { cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null || echo NA; }

throttled() {
    if command -v vcgencmd >/dev/null 2>&1; then
        vcgencmd get_throttled | sed 's/.*=//'
    elif [ -r /sys/devices/platform/soc/soc:firmware/get_throttled ]; then
        printf '0x%x\n' "0x$(cat /sys/devices/platform/soc/soc:firmware/get_throttled)"
    else
        echo NA
    fi
}

ultimo_clip() { ls -t "$DIR_EVENTOS"/acceso_*.avi 2>/dev/null | head -n 1; }

# ---------------------------------------------------------------- comandos

cmd_pipeline() { # Muestra el pipeline exacto del servicio
    titulo "Pipeline del servicio (versión gst-launch, appsink -> fakesink)"
    pipeline_gst_launch | sed 's/ \([tj]\.\) /\n  \1 /g'
}

cmd_caps() { # A1, A2, A5, A6, B1: caps negociados, conversiones reales y grafo .dot
    seg=${1:-5}
    detener_servicio
    dot_dir="$SALIDA/dot"; rm -rf "$dot_dir"; mkdir -p "$dot_dir"
    titulo "Ejecutando el pipeline $seg s con -v y GST_DEBUG_DUMP_DOT_DIR"
    GST_DEBUG_DUMP_DOT_DIR="$dot_dir" correr_pipeline "$seg" "$SALIDA/caps.txt" "$SALIDA/caps.err"
    grep "caps = " "$SALIDA/caps.txt" > "$SALIDA/caps_negociados.txt"
    echo "Caps negociados: $SALIDA/caps_negociados.txt ($(wc -l < "$SALIDA/caps_negociados.txt") pads)"

    titulo "Conversiones en el grafo real (A5)"
    # Para cada videoconvert/videoscale/v4l2convert compara caps de sink y src.
    awk '
    / caps = / {
        split($0, a, " caps = ")
        ruta = a[1]; caps = a[2]
        n = split(ruta, p, "/"); ultimo = p[n]; sub(/:$/, "", ultimo)
        split(ultimo, q, ":"); tipo = q[1]
        sub(/\.Gst.*$/, "", q[2]); elemento = q[2]
        pad = ultimo; sub(/^.*:/, "", pad)
        if (tipo !~ /GstVideoConvert|GstVideoScale|GstV4l2Convert|GstVideoConvertScale/) next
        fmt = caps; sub(/^.*format=\(string\)/, "", fmt); sub(/,.*$/, "", fmt)
        w = caps; sub(/^.*width=\(int\)/, "", w); sub(/,.*$/, "", w)
        h = caps; sub(/^.*height=\(int\)/, "", h); sub(/,.*$/, "", h)
        valor[elemento, pad] = fmt " " w "x" h
        elementos[elemento] = tipo
    }
    END {
        total = 0; activos = 0
        for (e in elementos) {
            total++
            ent = valor[e, "sink"]; sal = valor[e, "src"]
            estado = (ent == sal) ? "passthrough (no convierte)" : "CONVIERTE"
            if (ent != sal) activos++
            printf "  %-16s %-20s %s -> %s  %s\n", e, elementos[e], ent, sal, estado
        }
        printf "Total: %d elementos de conversión, %d convierten de verdad\n", total, activos
    }' "$SALIDA/caps_negociados.txt"

    titulo "Grafo del pipeline (A6, B1)"
    ls "$dot_dir"/*.dot 2>/dev/null | sed 's/^/  /'
    echo "En la PC (con graphviz):"
    echo "  scp root@<IP_RPI>:$dot_dir/*PLAYING*.dot . && dot -Tpng *PLAYING*.dot -o grafo.png"
}

cmd_fps() { # A3: cuadros reales de la cámara, contados (no leídos de los caps)
    seg=${1:-10}
    detener_servicio
    ancho=${SV_ANCHO:-640}; alto=${SV_ALTO:-480}; fps=${SV_FPS:-30}
    if [ "${SV_FORMATO_CAMARA:-raw}" = mjpeg ]; then
        caps="image/jpeg,width=$ancho,height=$alto,framerate=$fps/1"
    else
        caps="video/x-raw,width=$ancho,height=$alto,framerate=$fps/1"
    fi
    titulo "Contando cuadros de ${SV_DISPOSITIVO:-/dev/video0} durante $seg s ($caps)"
    gst-launch-1.0 -e -v v4l2src device="${SV_DISPOSITIVO:-/dev/video0}" do-timestamp=true \
        ! "$caps" ! fakesink name=contador silent=false sync=false \
        > "$SALIDA/fps.txt" 2>&1 &
    pid=$!; sleep 1; inicio=$(grep -c "chain" "$SALIDA/fps.txt")
    sleep "$seg"; fin=$(grep -c "chain" "$SALIDA/fps.txt")
    kill -INT "$pid" 2>/dev/null; wait "$pid"
    awk -v a="$inicio" -v b="$fin" -v s="$seg" -v f="$fps" 'BEGIN {
        r = (b - a) / s
        printf "Cuadros: %d en %d s -> %.2f fps reales (solicitados: %d)\n", b - a, s, r, f }'
    echo "Con el servicio en marcha, fps de cada rama en el journal:"
    echo "  journalctl -u $SERVICIO | grep estadisticas | tail"
}

cmd_latencia() { # D1: tracer latency por ruta y por elemento
    seg=${1:-10}
    detener_servicio
    titulo "Tracer de latencia durante $seg s"
    GST_TRACERS="latency(flags=pipeline+element)" GST_DEBUG="GST_TRACER:7" GST_DEBUG_NO_COLOR=1 \
        correr_pipeline "$seg" "$SALIDA/latencia.out" "$SALIDA/latencia.log"
    grep "TRACE .*GST_TRACER" "$SALIDA/latencia.log" > "$SALIDA/latencia_trazas.txt"
    echo "Por ruta fuente -> sink (ms):"
    awk '/ latency, / {
        s = $0; sub(/^.*sink-element=\(string\)/, "", s); sub(/,.*$/, "", s)
        t = $0; sub(/^.*time=\(guint64\)/, "", t); sub(/,.*$/, "", t); t = t / 1e6
        n[s]++; sum[s] += t; if (t > max[s]) max[s] = t
    } END { for (k in n) printf "  %-12s n=%-5d prom=%7.2f  max=%7.2f\n", k, n[k], sum[k] / n[k], max[k] }' \
        "$SALIDA/latencia_trazas.txt"
    echo "Por elemento (ms, para el presupuesto D4):"
    awk '/ element-latency, / {
        e = $0; sub(/^.*element=\(string\)/, "", e); sub(/,.*$/, "", e)
        t = $0; sub(/^.*time=\(guint64\)/, "", t); sub(/,.*$/, "", t); t = t / 1e6
        n[e]++; sum[e] += t; if (t > max[e]) max[e] = t
    } END { for (k in n) printf "  %-16s n=%-5d prom=%7.3f  max=%7.3f\n", k, n[k], sum[k] / n[k], max[k] }' \
        "$SALIDA/latencia_trazas.txt" | sort -t= -k3 -rn
    echo "Trazas completas: $SALIDA/latencia_trazas.txt"
    echo "La latencia de extremo a extremo (D2) se mide en la PC con el botón MEDIR LATENCIA."
}

cmd_cpu() { # C2: CPU del servicio por hilo durante N segundos
    seg=${1:-60}
    pid=$(pid_servicio); [ -n "$pid" ] || falla "el servicio no está corriendo"
    titulo "CPU de control_acceso.py (pid $pid) durante $seg s"
    t=$(hz)
    for tarea in /proc/"$pid"/task/*; do
        echo "$(basename "$tarea") $(ticks_proceso "$pid/task/$(basename "$tarea")") $(cat "$tarea/comm")"
    done > "$SALIDA/cpu_antes.txt"
    total0=$(ticks_proceso "$pid")
    sleep "$seg"
    total1=$(ticks_proceso "$pid")
    echo "Por hilo (los hilos de GStreamer se llaman como su queue):"
    for tarea in /proc/"$pid"/task/*; do
        echo "$(basename "$tarea") $(ticks_proceso "$pid/task/$(basename "$tarea")") $(cat "$tarea/comm")"
    done | awk -v t="$t" -v s="$seg" 'NR == FNR { a[$1] = $2; next }
        { d = $2 - a[$1]; if (d > 0) printf "  %-18s %6.1f %%\n", $3, 100 * d / (t * s) }' \
        "$SALIDA/cpu_antes.txt" - | sort -k2 -rn
    awk -v a="$total0" -v b="$total1" -v t="$t" -v s="$seg" \
        'BEGIN { printf "TOTAL: %.1f %% de un núcleo (4 núcleos = 400 %%)\n", 100 * (b - a) / (t * s) }'
    echo "Para C2: repetir con SV_JPEGENC=v4l2jpegenc o SV_FORMATO_CAMARA=mjpeg y comparar."
}

cmd_salud() { # F1-F4: RSS, descriptores, CPU, temperatura y throttling cada minuto
    minutos=${1:-240}
    archivo=${2:-$SALIDA/salud.csv}
    pid=$(pid_servicio); [ -n "$pid" ] || falla "el servicio no está corriendo"
    titulo "Registrando salud de pid $pid durante $minutos min en $archivo"
    echo "epoch,rss_kb,fds,cpu_pct,temp_mc,throttled" > "$archivo"
    t=$(hz); prev=$(ticks_proceso "$pid")
    i=0
    while [ "$i" -lt "$minutos" ]; do
        sleep 60
        [ -d "/proc/$pid" ] || { echo "el proceso $pid terminó (¿reinicio?)"; break; }
        ahora=$(ticks_proceso "$pid")
        cpu=$(awk -v a="$prev" -v b="$ahora" -v t="$t" 'BEGIN { printf "%.1f", 100 * (b - a) / (t * 60) }')
        prev=$ahora
        rss=$(awk '/VmRSS/ { print $2 }' "/proc/$pid/status")
        fds=$(ls "/proc/$pid/fd" | wc -l)
        echo "$(date +%s),$rss,$fds,$cpu,$(temperatura_mc),$(throttled)" >> "$archivo"
        i=$((i + 1))
    done
    cmd_resumen_salud "$archivo"
}

cmd_resumen_salud() { # Resumen de un salud.csv (pendiente de RSS por regresión lineal)
    archivo=${1:-$SALIDA/salud.csv}
    titulo "Resumen de $archivo"
    awk -F, 'NR > 1 {
        n++; x = ($1 - t0); if (n == 1) { t0 = $1; x = 0 }
        sx += x; sy += $2; sxx += x * x; sxy += x * $2
        if (n == 1 || $2 < rmin) rmin = $2; if ($2 > rmax) rmax = $2
        if (n == 1 || $3 < fmin) fmin = $3; if ($3 > fmax) fmax = $3
        if ($5 != "NA" && $5 > tmax) tmax = $5
        if ($6 != "0x0" && $6 != "NA") thr = thr " " $6
        cs += $4; dur = x
    } END {
        if (n < 2) { print "Faltan muestras"; exit }
        m = (n * sxy - sx * sy) / (n * sxx - sx * sx)
        printf "Muestras: %d (%.1f h)\n", n, dur / 3600
        printf "RSS: %d..%d KB, pendiente %.1f KB/h (F2: cercana a 0 = sin fuga)\n", rmin, rmax, m * 3600
        printf "Descriptores: %d..%d (F3: deben ser constantes)\n", fmin, fmax
        printf "CPU promedio: %.1f %%\n", cs / n
        printf "Temperatura máx: %.1f C\n", tmax / 1000
        printf "Throttling: %s (F4: debe ser 0x0)\n", (thr == "" ? "0x0 en todas las muestras" : thr)
    }' "$archivo"
}

cmd_disco() { # F5: tasa de escritura en la tarjeta SD
    seg=${1:-60}
    dev=${SV_DISCO:-mmcblk0}
    titulo "Escritura en /dev/$dev durante $seg s"
    s0=$(awk -v d="$dev" '$3 == d { print $10 }' /proc/diskstats)
    sleep "$seg"
    s1=$(awk -v d="$dev" '$3 == d { print $10 }' /proc/diskstats)
    awk -v a="$s0" -v b="$s1" -v s="$seg" 'BEGIN { printf "Escritura: %.1f KB/s\n", (b - a) * 512 / 1024 / s }'
    df -h "$DIR_EVENTOS" | tail -n 1 | awk '{ print "Libre en la partición de eventos: " $4 }'
    du -sh "$DIR_EVENTOS" 2>/dev/null | awk '{ print "Ocupado por los clips: " $1 }'
    command -v iostat >/dev/null 2>&1 && echo "Detalle: iostat -x 5"
}

cmd_carga() { # F6: pérdida de cuadros con carga en paralelo
    seg=${1:-180}
    servicio_activo || falla "el servicio debe estar corriendo"
    titulo "Carga de CPU (4 procesos) durante $seg s"
    echo "Tip: baje SV_INTERVALO_ESTADISTICAS a 10 en $DEFAULTS para más muestras."
    inicio=$(date +%s)
    pids=""
    for _ in 1 2 3 4; do
        ( while :; do :; done ) & pids="$pids $!"
    done
    sleep "$seg"
    # shellcheck disable=SC2086
    kill $pids 2>/dev/null
    echo "Estadísticas del servicio durante la carga:"
    journalctl -u "$SERVICIO" --since "@$inicio" --no-pager -o cat | grep estadisticas
    echo "Compare fps_camara con el valor sin carga (F6)."
}

cmd_clip() { # E4: el clip (el último por omisión) es reproducible
    archivo=${1:-$(ultimo_clip)}
    [ -f "$archivo" ] || falla "no hay clips en $DIR_EVENTOS"
    titulo "Validando $archivo"
    cuadros=$(gst-launch-1.0 -v filesrc location="$archivo" ! avidemux ! fakesink silent=false 2>&1 \
        | grep -c "chain")
    if [ "$cuadros" -gt 0 ]; then
        awk -v c="$cuadros" -v f="${SV_FPS:-30}" \
            'BEGIN { printf "OK: %d cuadros (%.2f s a %d fps)\n", c, c / f, f }'
    else
        echo "FALLA: el archivo no se pudo demultiplexar"; return 1
    fi
}

cmd_detener_y_validar() { # E4 + B6: systemctl stop durante una grabación
    servicio_activo || falla "el servicio debe estar corriendo"
    titulo "Póngase frente a la cámara; se espera a que empiece una grabación (máx. 60 s)"
    i=0
    while [ -z "$(ls "$DIR_EVENTOS/.grabando" 2>/dev/null)" ] && [ "$i" -lt 600 ]; do
        sleep 0.1; i=$((i + 1))
    done
    [ -n "$(ls "$DIR_EVENTOS/.grabando" 2>/dev/null)" ] || falla "no hubo grabación en 60 s"
    sleep 1
    echo "Grabación en curso: systemctl stop $SERVICIO"
    systemctl stop "$SERVICIO"
    systemctl status "$SERVICIO" --no-pager | head -n 3
    cmd_clip
    systemctl start "$SERVICIO"
}

cmd_reinicio() { # E3, E6: kill -9 y reinicio por systemd
    pid=$(pid_servicio); [ -n "$pid" ] || falla "el servicio no está corriendo"
    titulo "kill -9 $pid"
    kill -9 "$pid"
    sleep 8
    nuevo=$(pid_servicio)
    systemctl status "$SERVICIO" --no-pager | head -n 5
    if [ -n "$nuevo" ] && [ "$nuevo" != "$pid" ]; then
        echo "OK: systemd lo reinició (pid nuevo $nuevo)"
    else
        echo "FALLA: no se reinició"
    fi
    echo "Bitácora:"; tail -n 4 "$BITACORA"
}

cmd_camara() { # E2: desconexión de la cámara en caliente
    titulo "Desconecte la cámara USB ahora; se observa el servicio 20 s"
    journalctl -u "$SERVICIO" -f -n 0 --no-pager -o cat &
    jp=$!
    sleep 20
    kill "$jp" 2>/dev/null
    systemctl status "$SERVICIO" --no-pager | head -n 5
    echo "Bitácora:"; tail -n 5 "$BITACORA"
    echo "Vuelva a conectarla: el servicio se recupera solo (RestartSec=5)."
}

cmd_llenar() { # E5: deja solo N MB libres en la partición de eventos
    mb=${1:-50}
    libre=$(df -k "$DIR_EVENTOS" | awk 'NR == 2 { print int($4 / 1024) }')
    relleno=$((libre - mb))
    [ "$relleno" -gt 0 ] || falla "ya hay menos de $mb MB libres"
    titulo "Creando $DIR_EVENTOS/../relleno.bin de $relleno MB"
    if command -v fallocate >/dev/null 2>&1; then
        fallocate -l "${relleno}M" "$DIR_EVENTOS/../relleno.bin"
    else
        dd if=/dev/zero of="$DIR_EVENTOS/../relleno.bin" bs=1M count="$relleno" 2>/dev/null
    fi
    df -h "$DIR_EVENTOS" | tail -n 1
    echo "Provoque eventos y revise la bitácora (clip_borrado_por_espacio / sin_grabacion)."
    echo "Al terminar: verificar-pipeline.sh vaciar"
}

cmd_vaciar() { rm -f "$DIR_EVENTOS/../relleno.bin"; df -h "$DIR_EVENTOS" | tail -n 1; }

cmd_gpio() { # H3, H5: estado de las líneas 17 y 27
    titulo "GPIO"
    gpioinfo -c gpiochip0 17 27 2>/dev/null || gpioinfo gpiochip0 | grep -E "line +(17|27):"
    if ! servicio_activo; then
        echo "Valores (servicio detenido): $(gpioget -c gpiochip0 17 27 2>/dev/null || gpioget gpiochip0 17 27)"
    fi
    grep -E "^gpio=" /boot/config.txt 2>/dev/null
}

cmd_throttling() { # F4
    titulo "Throttling"
    echo "get_throttled = $(throttled)   temperatura = $(temperatura_mc) m°C"
}

cmd_plugins() { # G3: registro regenerado y elementos presentes
    titulo "Regenerando el registro de plugins"
    rm -rf "$HOME/.cache/gstreamer-1.0"
    gst-inspect-1.0 > /dev/null
    for e in v4l2src tee queue videorate videoscale videoconvert appsink appsrc \
             jpegenc jpegdec rtpjpegpay udpsink avimux avidemux fakesink; do
        if gst-inspect-1.0 "$e" > /dev/null 2>&1; then echo "  OK  $e"; else echo "  FALTA $e"; fi
    done
    gst-inspect-1.0 v4l2jpegenc > /dev/null 2>&1 \
        && echo "  (disponible v4l2jpegenc: codificador JPEG por hardware)"
}

cmd_hardware() { # C1: dispositivos del bloque multimedia
    titulo "Dispositivos V4L2"
    v4l2-ctl --list-devices 2>/dev/null
    echo "Elementos v4l2 de GStreamer:"
    gst-inspect-1.0 video4linux2 2>/dev/null | grep -E "^ +v4l2" | sed 's/^/  /'
}

cmd_versiones() { # G5
    titulo "Versiones"
    grep -E "^(PRETTY_NAME|VERSION_ID|DISTRO_CODENAME)" /etc/os-release 2>/dev/null
    uname -a
    gst-inspect-1.0 --version | head -n 2
    "$PYTHON" -c "import cv2, numpy; print('OpenCV', cv2.__version__, '| numpy', numpy.__version__)"
    [ -r /etc/build ] && cat /etc/build
}

cmd_todo() { # Revisiones rápidas que no detienen el servicio
    cmd_versiones; cmd_plugins; cmd_gpio; cmd_throttling
    [ -n "$(ultimo_clip)" ] && cmd_clip
    titulo "Clips en la tarjeta (máx. ${SV_MAX_EVENTOS:-10})"
    ls -l "$DIR_EVENTOS"/acceso_*.avi 2>/dev/null | wc -l
    titulo "Últimas estadísticas del servicio"
    journalctl -u "$SERVICIO" --no-pager -o cat 2>/dev/null | grep estadisticas | tail -n 3
}

ayuda() {
    cat <<EOF
Uso: $(basename "$0") <comando> [args]      (evidencias en $SALIDA)

  pipeline                  muestra el pipeline del servicio
  caps [seg]                A1 A2 A5 A6 B1  caps negociados, conversiones reales, grafo .dot
  fps [seg]                 A3              fps reales de la cámara (conteo de cuadros)
  latencia [seg]            D1 D4           tracer latency por ruta y por elemento
  cpu [seg]                 C2              CPU del servicio por hilo
  salud [min] [csv]         F1-F4           RSS, fds, CPU, temperatura, throttling cada minuto
  resumen-salud [csv]       F2-F4           resumen de un salud.csv
  disco [seg]               F5              tasa de escritura en la SD
  carga [seg]               F6              fps del servicio con 4 procesos de carga
  clip [archivo]            E4              valida que el último clip sea reproducible
  detener-y-validar         E4 B6           systemctl stop durante una grabación + validación
  reinicio                  E3 E6           kill -9 y verificación del reinicio
  camara                    E2              observa el servicio al desconectar la cámara
  llenar [MB] / vaciar      E5              llena el disco dejando MB libres / lo vacía
  gpio                      H3 H5           estado de GPIO17 y GPIO27
  throttling                F4
  plugins                   G3              regenera el registro y verifica elementos
  hardware                  C1              dispositivos V4L2 y elementos v4l2
  versiones                 G5
  todo                      revisiones rápidas sin detener el servicio
EOF
}

comando=${1:-}
[ $# -gt 0 ] && shift
case "$comando" in
    pipeline) cmd_pipeline ;;
    caps) cmd_caps "$@" ;;
    fps) cmd_fps "$@" ;;
    latencia) cmd_latencia "$@" ;;
    cpu) cmd_cpu "$@" ;;
    salud) cmd_salud "$@" ;;
    resumen-salud) cmd_resumen_salud "$@" ;;
    disco) cmd_disco "$@" ;;
    carga) cmd_carga "$@" ;;
    clip) cmd_clip "$@" ;;
    detener-y-validar) cmd_detener_y_validar ;;
    reinicio) cmd_reinicio ;;
    camara) cmd_camara ;;
    llenar) cmd_llenar "$@" ;;
    vaciar) cmd_vaciar ;;
    gpio) cmd_gpio ;;
    throttling) cmd_throttling ;;
    plugins) cmd_plugins ;;
    hardware) cmd_hardware ;;
    versiones) cmd_versiones ;;
    todo) cmd_todo ;;
    *) ayuda; [ -z "$comando" ] || exit 1 ;;
esac
