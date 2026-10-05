import re
import json
import queue
import threading
import time
from dataclasses import dataclass
from typing import Optional

import tkinter as tk
from tkinter import ttk, messagebox, filedialog

import serial
from serial.tools import list_ports

APP_NAME = "Dragino TrackerD Configuration Tool"
APP_VERSION = "1.2"
MAX_LOG_LINES = 5000
PORT_WATCH_MS = 1500


@dataclass
class Parameter:
    command: str
    label: str
    group: str
    value: str = ""          # Valor deseado (lo que se enviará al equipo)
    description: str = ""
    secret: bool = False
    read_value: str = ""     # Valor leído desde el equipo


GROUPS = [
    "Identidad / dispositivo",
    "LoRaWAN - OTAA",
    "LoRaWAN - ABP",
    "Radio LoRaWAN",
    "Temporización",
    "GPS",
    "Movimiento / sensores",
    "BLE / Wi-Fi",
    "Interfaz / energía",
]

PARAMETERS = [
    Parameter("DEUI", "Device EUI", GROUPS[0], description="Identificador único del dispositivo."),
    Parameter("APPEUI", "Application EUI", GROUPS[1], description="EUI de aplicación OTAA."),
    Parameter("APPKEY", "Application Key", GROUPS[1], description="Clave OTAA.", secret=True),
    Parameter("NJM", "Network Join Mode", GROUPS[1], description="0=ABP, 1=OTAA."),
    Parameter("DADDR", "Device Address", GROUPS[2], description="Dirección LoRaWAN para ABP."),
    Parameter("NWKSKEY", "Network Session Key", GROUPS[2], description="Clave de sesión ABP.", secret=True),
    Parameter("APPSKEY", "Application Session Key", GROUPS[2], description="Clave de sesión ABP.", secret=True),

    Parameter("ADR", "ADR", GROUPS[3], description="0=OFF, 1=ON."),
    Parameter("DR", "Data Rate", GROUPS[3], description="Data rate LoRaWAN."),
    Parameter("TXP", "Transmit Power", GROUPS[3], description="Potencia TX según la escala del firmware."),
    Parameter("CHE", "Channel / Channel Mode", GROUPS[3], description="Configuración de canales."),
    Parameter("CFM", "Confirmed Uplink", GROUPS[3], description="0=unconfirmed, 1=confirmed."),
    Parameter("PNACKMD", "Auto None-ACK", GROUPS[3], description="Modo de mensajes automáticos sin ACK."),
    Parameter("DWELLT", "Uplink Dwell Time", GROUPS[3], description="Uplink dwell time."),

    Parameter("TDC", "Normal TX interval", GROUPS[4], description="Intervalo de transmisión normal en ms."),
    Parameter("MTDC", "Motion TX interval", GROUPS[4], description="Intervalo durante movimiento en ms."),
    Parameter("ATDC", "Alarm TX interval", GROUPS[4], description="Intervalo durante alarma en ms."),
    Parameter("FTIME", "GPS fix timeout", GROUPS[4], description="Tiempo máximo de posicionamiento según firmware."),
    Parameter("ATST", "AT serial window", GROUPS[4], description="Ventana de configuración AT."),
    Parameter("INTWK", "Sport mode", GROUPS[4], description="Modo de actividad/movimiento."),

    Parameter("GF", "GPS enabled", GROUPS[5], description="0=OFF, 1=ON."),
    Parameter("PDOP", "PDOP threshold", GROUPS[5], description="Umbral PDOP."),
    Parameter("BG", "GPS payload time", GROUPS[5], description="Configuración del tiempo GPS."),
    Parameter("SMOD", "Work mode", GROUPS[5], description="Modo de trabajo/payload."),

    Parameter("PT", "Motion threshold", GROUPS[6], description="Umbral del sensor de movimiento."),
    Parameter("PM", "Pedometer", GROUPS[6], description="Configuración del podómetro."),
    Parameter("FD", "Fall detection", GROUPS[6], description="Detección de caída."),

    Parameter("BLEMASK", "BLE mask", GROUPS[7], description="Máscara/configuración BLE."),
    Parameter("WiFiMASK", "Wi-Fi mask", GROUPS[7], description="Máscara/configuración Wi-Fi."),

    Parameter("LON", "Uplink LED", GROUPS[8], description="Actividad LED de uplink."),
    Parameter("BEEP", "Buzzer", GROUPS[8], description="0=OFF, 1=ON."),
    Parameter("EAT", "Long press time", GROUPS[8], description="Tiempo de pulsación larga."),
    Parameter("SHOWID", "SHOWID", GROUPS[8], description="Mostrar identificación."),
    Parameter("ATST", "AT window", GROUPS[8], description="Ventana AT."),
    Parameter("DEVICE", "Device type", GROUPS[8], description="13=TrackerD según firmware observado."),
    Parameter("BTDC", "Bluetooth TX interval", GROUPS[8], description="Intervalo BT según firmware."),
]

# Remove duplicate command keeping first occurrence.
_unique = {}
for p in PARAMETERS:
    _unique.setdefault(p.command, p)
PARAMETERS = list(_unique.values())

# Lookup (case-insensitive) de parámetros por comando.
PARAM_BY_CMD = {p.command.upper(): p for p in PARAMETERS}

# Líneas que el equipo envía y que no son valores.
IGNORED_RX = {"OK", "ERROR", "AT_ERROR", "AT_PARAM_ERROR", "AT_BUSY_ERROR",
              "AT_NO_NET_JOINED", "AT_RX_ERROR", ""}

# Formatos aceptados para líneas tipo "AT+TDC=300000", "TDC=300000", "TDC: 300000"
CFG_LINE_RE = re.compile(r"^\s*(?:AT\+)?([A-Za-z0-9_]+)\s*[=:]\s*(.*?)\s*$")

STATUS_COLORS = {"ok": "#1a7f37", "error": "#c62828", "idle": "#555555"}

# --- Información de caracterización que imprime el equipo tras ATZ / arranque ---
RE_RESET = re.compile(r"rst:0x[0-9a-fA-F]+\s*\((\w+)\)\s*,\s*boot:0x[0-9a-fA-F]+\s*\((\w+)\)")
RE_ROM = re.compile(r"^ets\s+(.+?)\s*$")
RE_FLASH = re.compile(r"^mode:(\w+),\s*clock div:(\d+)")
RE_WAKEUP = re.compile(r"Wakeup was not caused by deep sleep:\s*(\d+)")
RE_FW = re.compile(r"^\s*([A-Za-z][\w\- ]*?)\s*,\s*(v\d+(?:\.\d+)+)\s*$", re.IGNORECASE)
RE_REGION = re.compile(r"^\s*([A-Z]{2}\d{3}(?:[-_][A-Z0-9]+)*)\s*$")
RE_BAT = re.compile(r"^\s*BAT:\s*(\d+)\s*mV", re.IGNORECASE)
RE_TXMODE = re.compile(
    r"TXMODE,\s*freq=(\d+),\s*len=(\d+),\s*SF=(\d+),\s*BW=(\d+),\s*CR=([\d/]+)(?:,\s*IH=(\d+))?"
)

# (clave, etiqueta). Los dos últimos se muestran a ancho completo en el panel.
DEVICE_INFO_FIELDS = [
    ("model", "Modelo"),
    ("firmware", "Firmware"),
    ("region", "Región"),
    ("battery", "Batería"),
    ("reset_reason", "Reinicio"),
    ("boot_mode", "Arranque"),
    ("last_tx", "Último TX"),
    ("captured_at", "Capturado"),
]


class SerialManager:
    def __init__(self, on_line):
        self.ser: Optional[serial.Serial] = None
        self.on_line = on_line
        self.rx_thread = None
        self.stop_event = threading.Event()
        self.lock = threading.Lock()

    def connect(self, port, baud, bytesize, parity, stopbits, timeout):
        self.disconnect()
        ser = serial.Serial(
            port=port,
            baudrate=int(baud),
            bytesize=bytesize,
            parity=parity,
            stopbits=stopbits,
            timeout=float(timeout),
        )
        self.ser = ser
        # Un evento de parada por conexión: un hilo antiguo nunca interfiere con el nuevo.
        self.stop_event = threading.Event()
        self.rx_thread = threading.Thread(
            target=self._reader, args=(ser, self.stop_event), daemon=True
        )
        self.rx_thread.start()

    def disconnect(self):
        self.stop_event.set()
        ser = self.ser
        self.ser = None
        if ser:
            try:
                ser.close()
            except Exception:
                pass

    @property
    def connected(self):
        ser = self.ser
        return ser is not None and ser.is_open

    def send(self, command):
        ser = self.ser
        if ser is None or not ser.is_open:
            raise RuntimeError("El puerto serial no está conectado.")
        payload = command.rstrip("\r\n") + "\r\n"
        with self.lock:
            ser.write(payload.encode("ascii", errors="replace"))
            ser.flush()
        self.on_line("TX", command.rstrip("\r\n"))

    def _reader(self, ser, stop_event):
        while not stop_event.is_set():
            try:
                raw = ser.readline()
                if raw:
                    text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                    self.on_line("RX", text)
            except Exception as exc:
                # Si la parada fue pedida por el usuario no es una desconexión inesperada.
                if not stop_event.is_set():
                    self.on_line("DISCONNECT", str(exc))
                break


class TrackerDGUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME} v{APP_VERSION}")
        self.geometry("1280x820")
        self.minsize(1050, 700)

        self.serial_mgr = SerialManager(self.serial_event)
        self.events = queue.Queue()
        self.config_values = {}
        self.param_items = {}
        self.dirty = set()
        self.pending_read: Optional[str] = None  # comando AT+X=? esperando respuesta
        self.connected_ui = False
        self.active_port = ""
        self.cmd_history = []
        self.cmd_history_idx = 0
        self.action_widgets = []
        self.conn_widgets = []
        self.device_info = {}          # datos capturados con ATZ
        self.info_from_profile = False
        self.info_vars = {}

        self.show_secrets = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="Desconectado")
        self.stats_var = tk.StringVar(value="")
        self.auto_scroll = tk.BooleanVar(value=True)

        self._build_style()
        self._build_ui()
        self.refresh_ports()
        self.update_connection_state(False)
        self.set_status("Desconectado", "idle")

        self.bind("<F5>", lambda _: self.refresh_ports())
        self.bind("<Control-l>", lambda _: self.clear_monitor())

        self.after(100, self.process_events)
        self.after(PORT_WATCH_MS, self.watch_port)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def _build_style(self):
        style = ttk.Style(self)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("Title.TLabel", font=("Segoe UI", 15, "bold"))
        style.configure("Status.TLabel", font=("Segoe UI", 10, "bold"))
        style.configure("Group.TLabelframe.Label", font=("Segoe UI", 10, "bold"))
        style.configure("Treeview", rowheight=26)

    def _build_ui(self):
        top = ttk.Frame(self, padding=10)
        top.pack(fill="x")

        ttk.Label(top, text="Dragino TrackerD", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        self.status_label = ttk.Label(top, textvariable=self.status_var, style="Status.TLabel")
        self.status_label.grid(row=0, column=1, padx=20, sticky="w")

        conn = ttk.LabelFrame(self, text="Comunicación serial", padding=8)
        conn.pack(fill="x", padx=10, pady=(0, 8))

        self.port_var = tk.StringVar()
        self.baud_var = tk.StringVar(value="115200")
        self.data_var = tk.StringVar(value="8")
        self.parity_var = tk.StringVar(value="None")
        self.stop_var = tk.StringVar(value="1")
        self.timeout_var = tk.StringVar(value="0.2")

        ttk.Label(conn, text="COM").grid(row=0, column=0)
        self.port_combo = ttk.Combobox(conn, textvariable=self.port_var, width=12, state="readonly")
        self.port_combo.grid(row=0, column=1, padx=5)
        self.refresh_btn = ttk.Button(conn, text="Actualizar", command=self.refresh_ports)
        self.refresh_btn.grid(row=0, column=2, padx=5)

        ttk.Label(conn, text="Baudrate").grid(row=0, column=3)
        baud_combo = ttk.Combobox(conn, textvariable=self.baud_var,
                                  values=["9600", "19200", "38400", "57600", "115200", "230400", "460800", "921600"],
                                  width=10)
        baud_combo.grid(row=0, column=4, padx=5)

        ttk.Label(conn, text="Data").grid(row=0, column=5)
        data_combo = ttk.Combobox(conn, textvariable=self.data_var, values=["5", "6", "7", "8"],
                                  width=5, state="readonly")
        data_combo.grid(row=0, column=6, padx=5)

        ttk.Label(conn, text="Parity").grid(row=0, column=7)
        parity_combo = ttk.Combobox(conn, textvariable=self.parity_var,
                                    values=["None", "Even", "Odd", "Mark", "Space"],
                                    width=8, state="readonly")
        parity_combo.grid(row=0, column=8, padx=5)

        ttk.Label(conn, text="Stop").grid(row=0, column=9)
        stop_combo = ttk.Combobox(conn, textvariable=self.stop_var, values=["1", "1.5", "2"],
                                  width=5, state="readonly")
        stop_combo.grid(row=0, column=10, padx=5)

        ttk.Label(conn, text="Timeout").grid(row=0, column=11)
        timeout_entry = ttk.Entry(conn, textvariable=self.timeout_var, width=7)
        timeout_entry.grid(row=0, column=12, padx=5)

        self.connect_btn = ttk.Button(conn, text="Conectar", command=self.toggle_connection)
        self.connect_btn.grid(row=0, column=13, padx=8)

        # Widgets de conexión que se bloquean mientras hay una sesión abierta.
        self.conn_widgets = [
            (self.port_combo, "readonly"),
            (baud_combo, "normal"),
            (data_combo, "readonly"),
            (parity_combo, "readonly"),
            (stop_combo, "readonly"),
            (timeout_entry, "normal"),
        ]

        # Barra de estado inferior (se empaqueta antes del cuerpo para que siempre sea visible).
        statusbar = ttk.Frame(self, padding=(10, 3))
        statusbar.pack(side="bottom", fill="x")
        ttk.Separator(self, orient="horizontal").pack(side="bottom", fill="x")
        ttk.Label(statusbar, textvariable=self.stats_var).pack(side="left")
        ttk.Label(statusbar, text=f"v{APP_VERSION}  |  F5: actualizar puertos  |  Ctrl+L: limpiar monitor",
                  foreground="#777777").pack(side="right")

        body = ttk.PanedWindow(self, orient="horizontal")
        body.pack(fill="both", expand=True, padx=10, pady=5)

        left = ttk.Frame(body, padding=(0, 0, 5, 0))
        right = ttk.Frame(body, padding=(5, 0, 0, 0))
        body.add(left, weight=3)
        body.add(right, weight=2)

        # Configuration tree/table
        cfg_toolbar = ttk.Frame(left)
        cfg_toolbar.pack(fill="x", pady=(0, 5))
        btn_cfg = ttk.Button(cfg_toolbar, text="Leer AT+CFG", command=self.read_cfg)
        btn_cfg.pack(side="left")
        btn_read = ttk.Button(cfg_toolbar, text="Leer parámetro", command=self.read_selected)
        btn_read.pack(side="left", padx=5)
        btn_apply_sel = ttk.Button(cfg_toolbar, text="Aplicar seleccionado", command=self.apply_selected)
        btn_apply_sel.pack(side="left", padx=5)
        btn_apply_all = ttk.Button(cfg_toolbar, text="Aplicar cambios", command=self.apply_all)
        btn_apply_all.pack(side="left", padx=5)
        ttk.Checkbutton(cfg_toolbar, text="Mostrar secretos", variable=self.show_secrets,
                        command=self.refresh_table).pack(side="right")

        cfg_toolbar2 = ttk.Frame(left)
        cfg_toolbar2.pack(fill="x", pady=(0, 5))
        ttk.Button(cfg_toolbar2, text="Limpiar valores", command=self.clear_values).pack(side="left")
        ttk.Button(cfg_toolbar2, text="Exportar perfil...", command=self.export_profile).pack(side="left", padx=5)
        ttk.Button(cfg_toolbar2, text="Importar perfil...", command=self.import_profile).pack(side="left", padx=5)

        columns = ("group", "command", "label", "read", "desired", "description")
        tree_frame = ttk.Frame(left)
        tree_frame.pack(fill="both", expand=True)

        self.tree = ttk.Treeview(tree_frame, columns=columns, show="tree headings", selectmode="browse")
        self.tree.heading("#0", text="Grupo / parámetro")
        self.tree.column("#0", width=220, minwidth=160, anchor="w", stretch=False)
        self.tree.heading("group", text="Grupo")
        self.tree.column("group", width=150, minwidth=100, stretch=False)
        self.tree.heading("command", text="AT")
        self.tree.column("command", width=90, minwidth=70, stretch=False)
        self.tree.heading("label", text="Nombre")
        self.tree.column("label", width=170, minwidth=100, stretch=False)
        self.tree.heading("read", text="ValorLeido")
        self.tree.column("read", width=200, minwidth=120, stretch=False)
        self.tree.heading("desired", text="Valor deseado")
        self.tree.column("desired", width=200, minwidth=120, stretch=False)
        self.tree.heading("description", text="Descripción")
        self.tree.column("description", width=360, minwidth=150, stretch=False)

        # Resaltar filas donde el valor deseado difiere del leído.
        self.tree.tag_configure("diff", background="#fff4cc")

        vs = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        hs = ttk.Scrollbar(tree_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        hs.pack(side="bottom", fill="x")
        vs.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)

        self.tree.bind("<Double-1>", self.edit_cell)

        # Manual command / monitor
        cmd = ttk.LabelFrame(right, text="Consola AT", padding=8)
        cmd.pack(fill="x")
        self.cmd_var = tk.StringVar()
        self.cmd_entry = ttk.Entry(cmd, textvariable=self.cmd_var)
        self.cmd_entry.pack(side="left", fill="x", expand=True)
        self.cmd_entry.bind("<Return>", lambda _: self.send_manual())
        self.cmd_entry.bind("<Up>", self.history_prev)
        self.cmd_entry.bind("<Down>", self.history_next)
        btn_send = ttk.Button(cmd, text="Enviar", command=self.send_manual)
        btn_send.pack(side="left", padx=5)
        btn_at = ttk.Button(cmd, text="AT?", command=lambda: self.send_command("AT?"))
        btn_at.pack(side="left")

        # Información de caracterización del equipo (se llena con la salida de ATZ).
        info = ttk.LabelFrame(right, text="Información del dispositivo (ATZ)", padding=8)
        info.pack(fill="x", pady=(8, 0))
        for idx, (key, label) in enumerate(DEVICE_INFO_FIELDS):
            var = tk.StringVar(value="—")
            self.info_vars[key] = var
            if idx < 6:
                row, col = divmod(idx, 2)
                span = 1
            else:
                row, col = 3 + (idx - 6), 0
                span = 3
            ttk.Label(info, text=label + ":", foreground="#555555").grid(
                row=row, column=col * 2, sticky="w", padx=(0, 4), pady=1)
            ttk.Label(info, textvariable=var, font=("Segoe UI", 9, "bold")).grid(
                row=row, column=col * 2 + 1, columnspan=span, sticky="w",
                padx=(0, 14), pady=1)
        btn_atz = ttk.Button(info, text="Ejecutar ATZ y capturar", command=self.run_atz)
        btn_atz.grid(row=5, column=0, columnspan=4, sticky="w", pady=(6, 0))

        # Controles que solo tienen sentido con el puerto abierto.
        self.action_widgets = [btn_cfg, btn_read, btn_apply_sel, btn_apply_all,
                               self.cmd_entry, btn_send, btn_at, btn_atz]

        monitor = ttk.LabelFrame(right, text="Monitor serial", padding=8)
        monitor.pack(fill="both", expand=True, pady=(8, 0))

        options = ttk.Frame(monitor)
        options.pack(fill="x")
        ttk.Checkbutton(options, text="Auto-scroll", variable=self.auto_scroll).pack(side="left")
        ttk.Button(options, text="Limpiar", command=self.clear_monitor).pack(side="right")
        ttk.Button(options, text="Guardar log...", command=self.save_log).pack(side="right", padx=5)

        self.monitor = tk.Text(monitor, wrap="none", font=("Consolas", 9))
        ms = ttk.Scrollbar(monitor, orient="vertical", command=self.monitor.yview)
        self.monitor.configure(yscrollcommand=ms.set)
        self.monitor.tag_configure("TX", foreground="#0b57d0")
        self.monitor.tag_configure("ERR", foreground="#c62828")
        self.monitor.tag_configure("DISCONNECT", foreground="#c62828", font=("Consolas", 9, "bold"))
        self.monitor.pack(side="left", fill="both", expand=True)
        ms.pack(side="right", fill="y")

        self.build_table()

    # ------------------------------------------------------------------
    # Estado de la interfaz
    # ------------------------------------------------------------------
    def set_status(self, text, level="idle"):
        self.status_var.set(text)
        self.status_label.configure(foreground=STATUS_COLORS.get(level, STATUS_COLORS["idle"]))

    def update_connection_state(self, connected):
        self.connected_ui = connected
        self.connect_btn.config(text="Desconectar" if connected else "Conectar")
        for w in self.action_widgets:
            w.config(state="normal" if connected else "disabled")
        for w, enabled_state in self.conn_widgets:
            w.config(state="disabled" if connected else enabled_state)
        self.refresh_btn.config(state="disabled" if connected else "normal")

    def update_stats(self):
        read = sum(1 for p in PARAMETERS if p.read_value)
        self.stats_var.set(
            f"Leídos: {read}/{len(PARAMETERS)}   |   Cambios pendientes: {len(self.dirty)}"
        )

    def refresh_ports(self):
        ports = [p.device for p in list_ports.comports()]
        self.port_combo["values"] = ports
        if ports and self.port_var.get() not in ports:
            self.port_var.set(ports[0])
        elif not ports:
            self.port_var.set("")

    # ------------------------------------------------------------------
    # Tabla
    # ------------------------------------------------------------------
    def _row_values(self, p):
        read_display = self.mask_value(p, p.read_value)
        desired_display = self.mask_value(p, p.value)
        return (p.group, p.command, p.label, read_display, desired_display, p.description)

    def _row_tags(self, p):
        if p.value and p.read_value and p.value != p.read_value:
            return ("diff",)
        return ()

    def build_table(self):
        self.tree.delete(*self.tree.get_children())
        self.param_items.clear()
        groups = {}
        for p in PARAMETERS:
            if p.group not in groups:
                groups[p.group] = self.tree.insert("", "end", text=p.group, open=True,
                                                   values=("", "", "", "", "", ""))
            parent = groups[p.group]
            item = self.tree.insert(parent, "end", text=p.label,
                                    values=self._row_values(p), tags=self._row_tags(p))
            self.param_items[p.command] = item

    def refresh_table(self):
        for p in PARAMETERS:
            self.update_row(p)

    def update_row(self, p):
        item = self.param_items.get(p.command)
        if not item:
            return
        self.tree.item(item, values=self._row_values(p), tags=self._row_tags(p))

    def mask_value(self, p, value=None):
        if value is None:
            value = p.value
        if p.secret and value and not self.show_secrets.get():
            return "••••••••••••"
        return value

    def edit_cell(self, event):
        item = self.tree.identify_row(event.y)
        if not item:
            return
        command = self.tree.set(item, "command")
        if not command:
            return
        p = next((x for x in PARAMETERS if x.command == command), None)
        if not p:
            return

        # Solo la columna "Valor deseado" es editable.
        column = self.tree.identify_column(event.x)
        desired_col = f"#{list(self.tree['columns']).index('desired') + 1}"
        if column != desired_col:
            return

        bbox = self.tree.bbox(item, "desired")
        if not bbox:
            return
        x, y, w, h = bbox
        if not w:
            return

        entry = ttk.Entry(self.tree)
        entry.insert(0, p.value)
        entry.place(x=x, y=y, width=w, height=h)
        entry.focus_set()
        closed = {"done": False}

        def commit(_=None):
            if closed["done"]:
                return
            closed["done"] = True
            new_value = entry.get()
            if new_value != p.value:
                p.value = new_value
                self.dirty.add(p.command)
            self.update_row(p)
            entry.destroy()

        def cancel(_=None):
            if closed["done"]:
                return
            closed["done"] = True
            entry.destroy()

        entry.bind("<Return>", commit)
        entry.bind("<Escape>", cancel)
        entry.bind("<FocusOut>", commit)

    def clear_values(self):
        if not messagebox.askyesno(
            "Limpiar valores",
            "Se borrarán los valores leídos y deseados de todos los parámetros.\n"
            "No se envía nada al equipo.\n\n¿Continuar?"
        ):
            return
        for p in PARAMETERS:
            p.read_value = ""
            p.value = ""
        self.dirty.clear()
        self.pending_read = None
        self.refresh_table()
        self.update_stats()

    # ------------------------------------------------------------------
    # Perfiles (exportar / importar valores deseados)
    # ------------------------------------------------------------------
    def export_profile(self):
        data = {p.command: p.value for p in PARAMETERS if p.value}
        if not data and not self.device_info:
            messagebox.showinfo("Exportar perfil",
                                "No hay valores deseados ni información del dispositivo para exportar.")
            return
        if any(p.secret and p.value for p in PARAMETERS):
            if not messagebox.askyesno(
                "Claves en texto plano",
                "El perfil incluye claves (AppKey / session keys) que se guardarán "
                "en texto plano.\n\n¿Continuar?"
            ):
                return
        path = filedialog.asksaveasfilename(
            title="Exportar perfil",
            defaultextension=".json",
            initialfile=f"trackerd_perfil_{time.strftime('%Y%m%d_%H%M%S')}.json",
            filetypes=[("Perfil JSON", "*.json"), ("Todos los archivos", "*.*")],
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "device": "Dragino TrackerD",
                        "version": 2,
                        "exported": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "app_version": APP_VERSION,
                        "device_info": self.device_info,
                        "parameters": data,
                    },
                    f, indent=2, ensure_ascii=False)
            self.set_status(f"Perfil exportado: {path}", "ok" if self.connected_ui else "idle")
        except OSError as exc:
            messagebox.showerror("Exportar perfil", str(exc))

    def import_profile(self):
        path = filedialog.askopenfilename(
            title="Importar perfil",
            filetypes=[("Perfil JSON", "*.json"), ("Todos los archivos", "*.*")],
        )
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("Formato de perfil inválido.")
            params = data["parameters"]
            if not isinstance(params, dict):
                raise ValueError("Formato de perfil inválido.")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            messagebox.showerror("Importar perfil", f"No se pudo leer el perfil:\n{exc}")
            return

        loaded, ignored = 0, 0
        for cmd, value in params.items():
            p = PARAM_BY_CMD.get(str(cmd).upper())
            if not p:
                ignored += 1
                continue
            p.value = str(value)
            if p.value != p.read_value:
                self.dirty.add(p.command)
            else:
                self.dirty.discard(p.command)
            loaded += 1
        info = data.get("device_info")
        info_loaded = isinstance(info, dict) and bool(info)
        if info_loaded:
            self.device_info = dict(info)
            self.info_from_profile = True
            self.update_device_info_panel()
        self.refresh_table()
        self.update_stats()
        msg = f"Parámetros cargados: {loaded}."
        if ignored:
            msg += f"\nIgnorados (desconocidos): {ignored}."
        if info_loaded:
            msg += "\nInformación del dispositivo cargada desde el perfil."
        msg += "\n\nRevisa la columna 'Valor deseado' y pulsa 'Aplicar cambios' para enviarlos."
        messagebox.showinfo("Importar perfil", msg)

    # ------------------------------------------------------------------
    # Eventos seriales / parseo de respuestas
    # ------------------------------------------------------------------
    def serial_event(self, direction, line):
        self.events.put((direction, line))

    def process_events(self):
        try:
            while True:
                direction, line = self.events.get_nowait()
                ts = time.strftime("%H:%M:%S")
                tag = direction if direction in ("TX", "ERR", "DISCONNECT") else "RX"
                self.monitor.insert("end", f"[{ts}] {direction}: {line}\n", tag)
                self._trim_monitor()
                if self.auto_scroll.get():
                    self.monitor.see("end")
                if direction == "ERR":
                    self.set_status("Error serial", "error")
                elif direction == "DISCONNECT":
                    self.handle_disconnect(line)
                elif direction == "RX":
                    self.handle_rx(line)
        except queue.Empty:
            pass
        self.update_stats()
        self.after(100, self.process_events)

    def _trim_monitor(self):
        lines = int(self.monitor.index("end-1c").split(".")[0])
        if lines > MAX_LOG_LINES:
            self.monitor.delete("1.0", f"{lines - MAX_LOG_LINES}.0")

    def set_read_value(self, p, value):
        p.read_value = value
        # Si aún no hay valor deseado, se precarga con el leído (sin marcar como cambio).
        if not p.value:
            p.value = value
        self.update_row(p)

    def handle_rx(self, line):
        text = line.strip()
        upper = text.upper()

        # Datos de caracterización (salida de ATZ / arranque): no son valores de parámetros.
        if self.parse_device_info(text):
            return

        # Fin de respuesta: se libera la lectura pendiente.
        if upper in IGNORED_RX or (upper.startswith("AT_") and upper.endswith("ERROR")):
            if upper != "":
                self.pending_read = None
            return

        # 1) Formato "AT+CMD=valor", "CMD=valor" o "CMD: valor" (p. ej. salida de AT+CFG)
        m = CFG_LINE_RE.match(text)
        if m:
            key = m.group(1).upper()
            value = m.group(2)
            p = PARAM_BY_CMD.get(key)
            if p and value != "?":
                self.set_read_value(p, value)
                if self.pending_read and self.pending_read.upper() == key:
                    self.pending_read = None
                return

        # 2) Respuesta cruda a AT+CMD=? (solo el valor)
        if self.pending_read:
            p = PARAM_BY_CMD.get(self.pending_read.upper())
            if p and not text.upper().startswith("AT+"):
                self.set_read_value(p, text)
                self.pending_read = None

    # ------------------------------------------------------------------
    # Conexión / desconexión
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # Información del dispositivo (ATZ)
    # ------------------------------------------------------------------
    def parse_device_info(self, text):
        """Extrae datos de caracterización de las líneas que imprime el equipo."""
        updates = {}
        m = RE_RESET.search(text)
        if m:
            updates["reset_reason"], updates["boot_mode"] = m.group(1), m.group(2)
        m = RE_ROM.match(text)
        if m:
            updates["esp32_rom"] = " ".join(m.group(1).split())
        m = RE_FLASH.match(text)
        if m:
            updates["flash_mode"] = m.group(1)
            updates["flash_clock_div"] = int(m.group(2))
        m = RE_WAKEUP.search(text)
        if m:
            updates["wakeup_cause"] = int(m.group(1))
        m = RE_FW.match(text)
        if m:
            updates["model"], updates["firmware"] = m.group(1).strip(), m.group(2)
        m = RE_REGION.match(text)
        if m:
            updates["region"] = m.group(1)
        m = RE_BAT.match(text)
        if m:
            updates["battery_mv"] = int(m.group(1))
        m = RE_TXMODE.search(text)
        if m:
            freq = int(m.group(1))
            updates["last_tx"] = {
                "freq_hz": freq,
                "freq_mhz": round(freq / 1e6, 3),
                "payload_len": int(m.group(2)),
                "sf": int(m.group(3)),
                "bw_khz": int(m.group(4)),
                "cr": m.group(5),
                "iheader": int(m.group(6)) if m.group(6) is not None else None,
            }
        if not updates:
            return False

        # Una captura en vivo reemplaza la información cargada desde un perfil.
        if self.info_from_profile:
            self.device_info.clear()
            self.info_from_profile = False
        self.device_info.update(updates)
        self.device_info["captured_at"] = time.strftime("%Y-%m-%d %H:%M")
        self.device_info["source"] = "ATZ"
        self.update_device_info_panel()
        return True

    def info_display(self, key):
        info = self.device_info
        try:
            if key == "battery":
                return f"{info['battery_mv']} mV" if "battery_mv" in info else "—"
            if key == "last_tx":
                t = info.get("last_tx")
                if not t:
                    return "—"
                return f"{t['freq_mhz']:.2f} MHz · SF{t['sf']} BW{t['bw_khz']} CR{t['cr']}"
            if key == "captured_at":
                value = info.get("captured_at")
                if not value:
                    return "—"
                return value + (" (perfil)" if self.info_from_profile else "")
            return str(info.get(key) or "—")
        except (KeyError, TypeError, ValueError):
            return "—"

    def update_device_info_panel(self):
        for key, var in self.info_vars.items():
            var.set(self.info_display(key))

    def run_atz(self):
        if not messagebox.askyesno(
            "Ejecutar ATZ",
            "ATZ reinicia el TrackerD (reset por software) y captura la información "
            "que imprime al arrancar.\n\n¿Continuar?"
        ):
            return
        self.send_command("ATZ")

    def toggle_connection(self):
        if self.connected_ui:
            self.serial_mgr.disconnect()
            self.pending_read = None
            self.update_connection_state(False)
            self.set_status("Desconectado", "idle")
            self.refresh_ports()
            return

        if not self.port_var.get():
            messagebox.showwarning("Puerto", "Selecciona un puerto COM.")
            return

        parity_map = {
            "None": serial.PARITY_NONE,
            "Even": serial.PARITY_EVEN,
            "Odd": serial.PARITY_ODD,
            "Mark": serial.PARITY_MARK,
            "Space": serial.PARITY_SPACE,
        }
        stop_map = {
            "1": serial.STOPBITS_ONE,
            "1.5": serial.STOPBITS_ONE_POINT_FIVE,
            "2": serial.STOPBITS_TWO,
        }

        try:
            self.serial_mgr.connect(
                self.port_var.get(),
                int(self.baud_var.get()),
                int(self.data_var.get()),
                parity_map[self.parity_var.get()],
                stop_map[self.stop_var.get()],
                float(self.timeout_var.get()),
            )
        except Exception as exc:
            self.refresh_ports()
            messagebox.showerror(
                "Error de conexión",
                f"{exc}\n\nVerifica que el dispositivo esté conectado y que ningún otro "
                "programa esté usando el puerto."
            )
            return

        self.active_port = self.port_var.get()
        self.update_connection_state(True)
        self.set_status(
            f"Conectado: {self.active_port} @ {self.baud_var.get()} "
            f"8{self.parity_var.get()[0]}{self.stop_var.get()}",
            "ok",
        )

    def handle_disconnect(self, reason=""):
        """Desconexión inesperada (cable USB retirado, puerto cerrado, etc.)."""
        if not self.connected_ui:
            return  # ya gestionada (evita avisos duplicados)
        lost_port = self.active_port
        self.serial_mgr.disconnect()
        self.pending_read = None
        self.update_connection_state(False)
        self.set_status(f"Desconectado: se perdió {lost_port}", "error")
        self.refresh_ports()
        self.after(50, lambda: messagebox.showwarning(
            "Dispositivo desconectado",
            f"Se perdió la comunicación con {lost_port}.\n\n"
            "Vuelve a conectar el TrackerD, pulsa 'Actualizar' y luego 'Conectar'."
        ))

    def watch_port(self):
        """Comprueba periódicamente que el puerto siga existiendo."""
        try:
            if self.connected_ui:
                available = [p.device for p in list_ports.comports()]
                if self.active_port not in available or not self.serial_mgr.connected:
                    self.handle_disconnect("Puerto no disponible")
        except Exception:
            pass
        self.after(PORT_WATCH_MS, self.watch_port)

    # ------------------------------------------------------------------
    # Envío de comandos
    # ------------------------------------------------------------------
    def send_command(self, command):
        if command.strip().upper() == "ATZ":
            self.pending_read = None
        try:
            self.serial_mgr.send(command)
        except (serial.SerialException, OSError) as exc:
            self.handle_disconnect(str(exc))
        except Exception as exc:
            messagebox.showerror("Serial", str(exc))

    def send_manual(self):
        cmd = self.cmd_var.get().strip()
        if cmd:
            if not self.cmd_history or self.cmd_history[-1] != cmd:
                self.cmd_history.append(cmd)
            self.cmd_history_idx = len(self.cmd_history)
            self.send_command(cmd)
            self.cmd_var.set("")

    def history_prev(self, _=None):
        if self.cmd_history and self.cmd_history_idx > 0:
            self.cmd_history_idx -= 1
            self.cmd_var.set(self.cmd_history[self.cmd_history_idx])
            self.cmd_entry.icursor("end")
        return "break"

    def history_next(self, _=None):
        if self.cmd_history_idx < len(self.cmd_history) - 1:
            self.cmd_history_idx += 1
            self.cmd_var.set(self.cmd_history[self.cmd_history_idx])
        else:
            self.cmd_history_idx = len(self.cmd_history)
            self.cmd_var.set("")
        self.cmd_entry.icursor("end")
        return "break"

    def read_cfg(self):
        self.pending_read = None
        self.send_command("AT+CFG")

    def selected_parameter(self):
        selection = self.tree.selection()
        if not selection:
            return None
        command = self.tree.set(selection[0], "command")
        return next((p for p in PARAMETERS if p.command == command), None)

    def read_selected(self):
        p = self.selected_parameter()
        if not p:
            messagebox.showinfo("Parámetro", "Selecciona un parámetro de la tabla.")
            return
        # According to the TrackerD help, AT+CMD=? gets the parameter value.
        self.pending_read = p.command
        self.send_command(f"AT+{p.command}=?")

    def apply_selected(self):
        p = self.selected_parameter()
        if not p:
            messagebox.showinfo("Parámetro", "Selecciona un parámetro de la tabla.")
            return
        self.apply_parameter(p)

    def apply_parameter(self, p):
        if p.secret and not p.value:
            messagebox.showwarning("Valor vacío", f"{p.command} está vacío.")
            return
        if not messagebox.askyesno(
            "Confirmar cambio",
            f"Enviar:\n\nAT+{p.command}={p.value}\n\n¿Continuar?"
        ):
            return
        self.send_command(f"AT+{p.command}={p.value}")
        self.dirty.discard(p.command)

    def apply_all(self):
        changed = [p for p in PARAMETERS if p.command in self.dirty]
        if not changed:
            messagebox.showinfo("Cambios", "No hay parámetros modificados.")
            return

        if not messagebox.askyesno(
            "Confirmar cambios",
            "Se enviarán los siguientes parámetros:\n\n" +
            "\n".join(f"AT+{p.command}={p.value}" for p in changed) +
            "\n\n¿Continuar?"
        ):
            return

        def worker():
            for p in changed:
                try:
                    self.serial_mgr.send(f"AT+{p.command}={p.value}")
                    time.sleep(0.15)
                    self.dirty.discard(p.command)
                except (serial.SerialException, OSError) as exc:
                    self.events.put(("DISCONNECT", str(exc)))
                    break
                except Exception as exc:
                    self.events.put(("ERR", str(exc)))
                    break

        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------------------
    # Monitor
    # ------------------------------------------------------------------
    def clear_monitor(self):
        self.monitor.delete("1.0", "end")

    def save_log(self):
        content = self.monitor.get("1.0", "end-1c")
        if not content.strip():
            messagebox.showinfo("Guardar log", "El monitor está vacío.")
            return
        path = filedialog.asksaveasfilename(
            title="Guardar log",
            defaultextension=".txt",
            initialfile=f"trackerd_log_{time.strftime('%Y%m%d_%H%M%S')}.txt",
            filetypes=[("Texto", "*.txt"), ("Todos los archivos", "*.*")],
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(content + "\n")
        except OSError as exc:
            messagebox.showerror("Guardar log", str(exc))

    def on_close(self):
        if self.dirty and not messagebox.askyesno(
            "Salir", "Hay cambios sin aplicar al equipo.\n\n¿Salir de todas formas?"
        ):
            return
        self.serial_mgr.disconnect()
        self.destroy()


if __name__ == "__main__":
    app = TrackerDGUI()
    app.mainloop()