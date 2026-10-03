# Diseño del sistema Secure Vision

## La idea

El objetivo del sistema es **saber quién entra y cuándo**, no restringir la entrada a un grupo de personas autorizadas. Es un control de acceso por **registro**: la puerta se abre para cualquiera que se presente, pero nadie entra sin dejar evidencia.

Una cámara vigila la entrada y la Raspberry Pi analiza la imagen continuamente:

- **Sin nadie frente a la cámara**, la puerta permanece cerrada y el **LED rojo** está encendido.
- **Cuando aparece un rostro**, el sistema lo toma como un intento de ingreso: se apaga el LED rojo, se enciende el **LED verde** (que representa la cerradura liberada) y la puerta queda abierta durante **5 segundos**.
- **Mientras la puerta está abierta** se graba un video corto del ingreso y se anota el evento en una bitácora con la fecha y la hora.
- **Pasados los 5 segundos**, el LED verde se apaga, vuelve el rojo y la puerta se cierra otra vez.

El rostro no se compara con ninguna lista: basta con que haya una cara de frente para abrir. Lo importante es que **cada apertura queda asociada a un video**, así que después siempre se puede ver quién fue.

Un guardia, desde su computadora, cumple dos funciones:

1. **Vigilancia en vivo:** ve en todo momento lo que capta la cámara de la entrada.
2. **Revisión de ingresos:** su aplicación descarga automáticamente los videos y la bitácora de la Raspberry. Si necesita saber quién entró a cierta hora, busca el evento y reproduce el video.

Este enfoque evita registrar datos biométricos o mantener una base de personas, y aun así deja un historial verificable de todos los ingresos.

## Cómo funciona

```
      ENTRADA                          RASPBERRY PI                         PC DEL GUARDIA
 ┌──────────────┐           ┌─────────────────────────────┐           ┌──────────────────────┐
 │   Persona    │  cámara   │  ¿Hay un rostro?            │  video en │  Vista en vivo       │
 │  frente a la │ ────────► │    sí → abrir puerta 5 s    │  vivo ──► │                      │
 │   cámara     │           │         grabar video 5 s    │           │  Lista de eventos    │
 └──────────────┘           │         anotar en bitácora  │  videos ─►│  (videos + bitácora) │
                            │    no → puerta cerrada      │           │                      │
                            └──────────────┬──────────────┘           └──────────────────────┘
                                           │
                                     LED rojo / verde
                                     (cerradura)
```

1. **Vigilar.** La Raspberry mira la cámara todo el tiempo y envía el video en vivo a la PC del guardia.
2. **Detectar.** Si aparece un rostro, se considera un evento.
3. **Abrir.** La salida de la cerradura se activa durante 5 segundos (LED verde) y luego vuelve a cerrar (LED rojo).
4. **Registrar.** El evento se graba en un video corto y se anota en una bitácora.
5. **Revisar.** La PC del guardia descarga los videos y la bitácora automáticamente, y el guardia puede verlos cuando quiera.

## Las partes

| Parte | Qué hace |
|---|---|
| **Cámara** | Capta la entrada |
| **Raspberry Pi 4** | Detecta rostros, controla la cerradura, graba los eventos y transmite el video |
| **Cerradura / LEDs** | Rojo = cerrado, verde = abierto |
| **PC del guardia** | Muestra el video en vivo y guarda los eventos para revisarlos |

## Pipelines de GStreamer

GStreamer arma el procesamiento de video como una cadena de **elementos** conectados con `!`: cada uno hace una tarea (capturar, convertir, comprimir, enviar) y le pasa el resultado al siguiente. Entre elementos pueden ir **filtros de formato** (`video/x-raw,...`), que exigen un tamaño, formato de color o tasa de cuadros en ese punto de la cadena.

El sistema usa dos pipelines principales: uno en la Raspberry (**emisor**) y otro en la PC del guardia (**receptor**), unidos por la red.

```
 RASPBERRY (emisor)                                        PC DEL GUARDIA (receptor)
 cámara → JPEG → paquetes RTP → udpsink ───── UDP :5000 ────► udpsrc → JPEG → imagen → pantalla
```

### Emisor (Raspberry Pi)

#### Pipeline completo

```
v4l2src device=/dev/video0 do-timestamp=true
  ! video/x-raw,width=640,height=480,framerate=30/1
  ! tee name=t

t. ! queue name=q_analisis leaky=downstream max-size-buffers=2 max-size-bytes=0 max-size-time=0
   ! videorate drop-only=true max-rate=10 ! videoscale ! videoconvert
   ! video/x-raw,format=GRAY8,width=320,height=240
   ! appsink name=analisis sync=false drop=true max-buffers=1                     ──► detección

t. ! queue name=q_codificador leaky=downstream max-size-buffers=4 max-size-bytes=0 max-size-time=0
   ! videoconvert ! video/x-raw,format=(string){YUY2,UYVY,I420,NV12}
   ! jpegenc quality=85 ! tee name=j

j. ! queue name=q_red leaky=downstream max-size-buffers=4 max-size-bytes=0 max-size-time=0
   ! rtpjpegpay ! udpsink host=<IP_PC> port=5000 sync=false async=false          ──► PC

j. ! queue name=q_grabacion max-size-buffers=30 max-size-bytes=0 max-size-time=0
   ! appsink name=grabacion sync=false drop=false max-buffers=0 emit-signals=true  ──► clip
```

#### Estructura

Un solo pipeline captura la cámara una vez y reparte la imagen en tres usos. Los `tee` son "repartidores": copian cada cuadro a varias ramas, y cada rama empieza con una `queue` (una fila con su propio hilo), para que una rama lenta no frene a las demás.

```
                       ┌─► ANÁLISIS: 10 fps, gris, 320x240 ──────────► Python: ¿hay rostro? → LEDs
cámara ─► tee (t) ─────┤
                       └─► JPEG ─► tee (j) ─┬─► RED: paquetes RTP ────► PC del guardia
                                            └─► GRABACIÓN ────────────► Python → clip .avi
```

#### Paso por paso

**1. Captura**

| Elemento | Qué hace |
|---|---|
| `v4l2src device=/dev/video0` | Lee la cámara USB por medio de Video4Linux2, el controlador de video de Linux |
| `do-timestamp=true` | Marca cada cuadro con la hora en que se capturó; con eso se mide cuánto tarda la decisión de abrir |
| `video/x-raw,width=640,height=480,framerate=30/1` | Pide a la cámara 640×480 a 30 fps. No fija el formato de color, así que la cámara entrega el suyo (YUY2) |
| `tee name=t` | Reparte cada cuadro a la rama de análisis y a la de codificación |

**2. Rama de análisis (detección de rostros)**

| Elemento | Qué hace | Por qué |
|---|---|---|
| `queue name=q_analisis leaky=downstream max-size-buffers=2` | Fila de máximo 2 cuadros; si se llena, descarta los más viejos | Para decidir si hay alguien en la puerta importa el cuadro más reciente, no uno atrasado |
| `videorate drop-only=true max-rate=10` | Deja pasar como máximo 10 cuadros por segundo | Detectar 30 veces por segundo no mejora nada y triplica el trabajo |
| `videoscale` | Reduce la imagen a la mitad por lado | Con 4 veces menos píxeles, la detección es unas 4 veces más rápida |
| `videoconvert` + `format=GRAY8,width=320,height=240` | Pasa la imagen a escala de grises | El detector de rostros trabaja en gris |
| `appsink name=analisis drop=true max-buffers=1 sync=false` | Entrega los cuadros al programa de Python | Guarda solo el último cuadro y lo entrega sin esperar; Python nunca procesa imágenes viejas |

Python toma esos cuadros, busca rostros y, si encuentra uno, enciende el LED verde, apaga el rojo y empieza a grabar.

**3. Rama de codificación (compresión JPEG)**

| Elemento | Qué hace | Por qué |
|---|---|---|
| `queue name=q_codificador leaky=downstream max-size-buffers=4` | Fila de máximo 4 cuadros | Si el procesador no alcanza a comprimir, se pierden cuadros aquí en lugar de frenar la cámara |
| `videoconvert` + `format=(string){YUY2,UYVY,I420,NV12}` | Asegura un formato de color compatible con el envío por red | El estándar RTP/JPEG solo transporta ciertos formatos. Si la cámara ya entrega YUY2, pasa sin convertir |
| `jpegenc quality=85` | Comprime cada cuadro como una imagen JPEG al 85 % de calidad | La imagen se comprime **una sola vez** y se usa para la red y para la grabación |
| `tee name=j` | Reparte el JPEG a la rama de red y a la de grabación | |

**4. Rama de red (video en vivo hacia la PC)**

| Elemento | Qué hace | Por qué |
|---|---|---|
| `queue name=q_red leaky=downstream max-size-buffers=4` | Fila de máximo 4 cuadros; descarta los viejos | En video en vivo es mejor perder un cuadro que atrasarse |
| `rtpjpegpay` | Corta cada JPEG en paquetes pequeños con formato RTP | RTP es el protocolo estándar para enviar video por red |
| `udpsink host=<IP_PC> port=5000 sync=false` | Envía los paquetes por UDP a la PC del guardia | UDP no espera confirmación: es rápido y, si se pierde un paquete, solo se pierde ese cuadro |

**5. Rama de grabación (evidencia)**

| Elemento | Qué hace | Por qué |
|---|---|---|
| `queue name=q_grabacion max-size-buffers=30` (sin `leaky`) | Fila de hasta 30 cuadros (1 s) que **no descarta** nada | Es la evidencia del ingreso: no se deben perder cuadros |
| `appsink name=grabacion emit-signals=true drop=false` | Avisa a Python por cada JPEG que llega | Si no hay evento, Python lo descarta; si hay, lo agrega al clip |

#### Pipeline del clip (uno por evento)

Cuando se detecta un rostro, Python crea un segundo pipeline pequeño que dura solo los 5 segundos del evento:

```
appsrc ! avimux ! filesink location=acceso_AAAAMMDD_HHMMSS.avi
```

| Elemento | Qué hace |
|---|---|
| `appsrc` | Recibe desde Python los JPEG de la rama de grabación |
| `avimux` | Los empaqueta en un archivo de video AVI |
| `filesink` | Escribe el archivo en la tarjeta SD |

Al terminar los 5 segundos se envía una señal de fin (EOS), `avimux` completa el archivo y queda listo para reproducirse. El clip usa los mismos JPEG que se envían a la PC, así que grabar no exige comprimir otra vez.

#### Por qué filas que pierden cuadros y filas que no

| Fila | ¿Descarta cuadros? | Razón |
|---|---|---|
| `q_analisis` | Sí | Solo importa lo más reciente para decidir |
| `q_codificador` | Sí | Protege la captura si el procesador se atrasa |
| `q_red` | Sí | En vivo, la rapidez importa más que la completitud |
| `q_grabacion` | **No** | Es la evidencia: tiene que estar completa |

### Receptor (PC del guardia)

#### Pipeline completo

```
udpsrc port=5000 buffer-size=4194304
  caps="application/x-rtp,media=video,clock-rate=90000,encoding-name=JPEG,payload=26"
  ! rtpjpegdepay
  ! jpegdec
  ! videoconvert
  ! video/x-raw,format=RGB
  ! appsink name=videosink sync=false max-buffers=1 drop=true
```

#### Paso por paso

```
red ─► udpsrc ─► rtpjpegdepay ─► jpegdec ─► videoconvert (RGB) ─► appsink ─► Python/Qt ─► pantalla
       paquetes   arma el JPEG    imagen      formato de pantalla   último cuadro
```

| Elemento | Qué hace | Por qué |
|---|---|---|
| `udpsrc port=5000` | Escucha los paquetes que llegan al puerto 5000 | Es la contraparte de `udpsink` en la Raspberry |
| `buffer-size=4194304` | Reserva 4 MB para los paquetes entrantes | Cada cuadro llega como decenas de paquetes seguidos; con poco espacio se perderían |
| `caps="application/x-rtp,…,encoding-name=JPEG,payload=26"` | Describe qué contienen los paquetes | Los paquetes RTP no dicen qué llevan dentro: el receptor tiene que saberlo de antemano, y debe coincidir con lo que genera `rtpjpegpay` |
| `rtpjpegdepay` | Junta los paquetes y reconstruye cada imagen JPEG | Es la operación inversa de `rtpjpegpay` |
| `jpegdec` | Descomprime el JPEG a una imagen normal | |
| `videoconvert` + `format=RGB` | Convierte la imagen a RGB | Es el formato que usa la interfaz gráfica (Qt) para mostrarla |
| `appsink sync=false max-buffers=1 drop=true` | Entrega las imágenes al programa de Python | Muestra cada cuadro apenas llega y se queda solo con el más reciente, para que el video no se atrase |

La aplicación revisa además los mensajes de error de GStreamer: si el pipeline falla, muestra `● ERROR` y se reconecta sola a los 3 segundos.

