# TrackerD GUI

GUI de escritorio para Windows 11 para configurar y monitorizar un Dragino TrackerD mediante su interfaz USB/serial.

## Características

- Selección de COM, baudrate, bits de datos, paridad, stop bits y timeout.
- Conexión/desconexión serial.
- Monitor serial en tiempo real.
- `AT?` para descubrir comandos soportados por el firmware.
- `AT+CFG` para leer la configuración completa.
- Visualización jerárquica por grupos:
  - Identidad / dispositivo
  - LoRaWAN - OTAA
  - LoRaWAN - ABP
  - Radio
  - Temporización
  - GPS
  - Movimiento / sensores
  - BLE / Wi-Fi
  - Interfaz / energía
- Edición mediante tabla.
- Botón "Leer" por parámetro usando `AT+CMD=?`.
- Botón "Aplicar" usando `AT+CMD=value`.
- Botón "Aplicar todo" para enviar solamente parámetros modificados.
- Registro de actividad.
- Valores sensibles como APPKEY/NWKSKEY/APPSKEY se pueden ocultar.
- No guarda automáticamente claves LoRaWAN en disco.

## Requisitos

Windows 11 + Python 3.11 o superior recomendado.

## Instalación

Abre PowerShell en esta carpeta:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\setup_venv.ps1
```

o usa:

```bat
setup_venv.bat
```

Después:

```powershell
.\.venv\Scripts\python.exe tracker_gui.py
```

También puedes ejecutar:

```bat
run.bat
```

## Dependencia

La única dependencia externa de Python es:

```text
pyserial
```

Tkinter viene incluido normalmente con Python para Windows.

## Comunicación

El TrackerD observado en las pruebas utiliza:

- 115200 baud
- 8 bits
- sin paridad
- 1 stop bit
- terminación CR+LF

La GUI permite modificar estos valores.

## Nota

La lista de parámetros está basada en los comandos observados en un TrackerD v1.5.6. `AT+CFG` se trata como fuente de verdad: cualquier comando nuevo que el firmware entregue puede aparecer en el monitor serial aunque todavía no tenga una fila especializada en la tabla.
