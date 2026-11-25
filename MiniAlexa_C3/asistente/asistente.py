import datetime
import queue
import struct
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk, messagebox

# Si se abre con otro Python (p. ej. el boton ▶ de VS Code), relanzar con el del .venv,
# que es el que tiene edge-tts, miniaudio y pyttsx3 instalados.
_VENV = Path(__file__).resolve().parent / '.venv'
if _VENV.exists() and Path(sys.prefix).resolve() != _VENV.resolve():
    sys.exit(subprocess.call([str(_VENV / 'Scripts' / 'python.exe'), str(Path(__file__).resolve())] + sys.argv[1:]))

import serial
from serial.tools import list_ports

from audio import save_wav
import cerebro
import protocolo

ROOT = Path(__file__).resolve().parent


class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title('Mini-Alexa C3')
        self.root.geometry('720x560')
        self.cola = queue.Queue()
        self.pedir = threading.Event()
        self.activo = False
        self.ocupado = False
        f = ttk.Frame(self.root, padding=20)
        f.pack(fill='both', expand=True)
        ttk.Label(f, text='Mini-Alexa C3', font=('Segoe UI', 22, 'bold')).pack(anchor='w')
        ttk.Label(f, text='Electret → C3 → Gemini → voz → C3 → parlante').pack(anchor='w', pady=4)
        fila = ttk.Frame(f)
        fila.pack(fill='x', pady=8)
        self.combo = ttk.Combobox(fila, state='readonly', width=32)
        self.combo.pack(side='left')
        ttk.Button(fila, text='Actualizar', command=self.puertos).pack(side='left', padx=6)
        self.bconectar = ttk.Button(fila, text='Conectar', command=self.conectar)
        self.bconectar.pack(side='left')
        self.laptop = True      # leer la respuesta por los parlantes de la laptop
        self.vlaptop = tk.BooleanVar(value=True)
        ttk.Checkbutton(f, text='💻 Leer la respuesta en voz alta en la laptop', variable=self.vlaptop,
                        command=lambda: setattr(self, 'laptop', self.vlaptop.get())).pack(anchor='w')
        self.parlante = False   # parlante de la C3 (circuito LM386)
        self.vparlante = tk.BooleanVar(value=False)
        ttk.Checkbutton(f, text='🔊 Parlante conectado (responder también con voz)', variable=self.vparlante,
                        command=lambda: setattr(self, 'parlante', self.vparlante.get())).pack(anchor='w')
        self.bhablar = ttk.Button(f, text='🎤  Hablar (6 s)   — o pulsa BOOT en la placa',
                                  command=self.hablar, state='disabled')
        self.bhablar.pack(fill='x', pady=8)
        self.estado = tk.StringVar(value='Conecta la C3 y pulsa Conectar.')
        ttk.Label(f, textvariable=self.estado, font=('Segoe UI', 12, 'bold'), wraplength=660).pack(anchor='w', pady=6)
        ttk.Label(f, text='Escuché:').pack(anchor='w')
        self.txt_pregunta = tk.StringVar()
        ttk.Label(f, textvariable=self.txt_pregunta, wraplength=660, font=('Segoe UI', 12)).pack(anchor='w', pady=4)
        ttk.Label(f, text='Respuesta:').pack(anchor='w', pady=(10, 0))
        self.txt_respuesta = tk.StringVar()
        ttk.Label(f, textvariable=self.txt_respuesta, wraplength=660, font=('Segoe UI', 12)).pack(anchor='w', pady=4)
        self.info = tk.StringVar()
        ttk.Label(f, textvariable=self.info, wraplength=660, foreground='gray').pack(anchor='w', pady=12)
        self.root.protocol('WM_DELETE_WINDOW', self.cerrar)
        self.puertos()
        self.root.after(100, self.atender_cola)

    # --- interfaz ---
    def puertos(self):
        self.devs = list(list_ports.comports())
        self.combo['values'] = [f'{p.device} · {p.description}' for p in self.devs]
        if self.devs:
            self.combo.current(0)

    def ui(self, **k):
        self.cola.put(k)

    def atender_cola(self):
        while not self.cola.empty():
            k = self.cola.get()
            for nombre, valor in k.items():
                getattr(self, nombre).set(valor)
        self.bhablar['state'] = 'normal' if self.activo and not self.ocupado else 'disabled'
        self.root.after(100, self.atender_cola)

    def conectar(self):
        if self.activo:
            return
        n = self.combo.current()
        if n < 0:
            messagebox.showinfo('Puerto', 'Conecta la C3 y actualiza los puertos.')
            return
        try:
            cfg = cerebro.cargar_config()
        except Exception as e:
            messagebox.showerror('Configuración', str(e))
            return
        self.activo = True
        self.bconectar['state'] = 'disabled'
        threading.Thread(target=self.bucle, args=(self.devs[n].device, cfg), daemon=True).start()

    def hablar(self):
        if self.activo and not self.ocupado:
            self.pedir.set()

    def cerrar(self):
        self.activo = False
        self.root.after(300, self.root.destroy)

    # --- hilo de comunicacion ---
    def bucle(self, puerto, cfg):
        try:
            link = serial.Serial()
            link.port, link.baudrate, link.timeout, link.write_timeout = puerto, 921600, 0.1, 3
            link.dtr = False
            link.rts = False   # no reiniciar la C3
            link.open()
            protocolo.vaciar(link)   # restos de grabaciones hechas con el asistente cerrado
        except Exception as e:
            self.activo = False
            self.ui(estado=f'No se pudo abrir {puerto}: {e}')
            return
        self.ui(estado='Lista. Pulsa Hablar o BOOT y haz tu pregunta.')
        ventana = bytearray()
        with link:
            while self.activo:
                if self.pedir.is_set():
                    self.pedir.clear()
                    link.reset_input_buffer()
                    link.write(b'R')
                b = link.read(1)
                if not b:
                    continue
                if b == b'S':
                    self.ocupado = True
                    self.ui(estado='🔴 GRABANDO: habla ahora (6 s)…', txt_pregunta='', txt_respuesta='')
                    continue
                ventana += b
                del ventana[:-8]
                if bytes(ventana) == protocolo.MAGIC:
                    ventana.clear()
                    self.turno(link, cfg)
                    self.ocupado = False
        self.ui(estado='Desconectado.')

    def turno(self, link, cfg):
        t0 = time.monotonic()
        try:
            self.ui(estado='Recibiendo audio…')
            rate, raw = protocolo.leer_paquete(link)
            carpeta = ROOT / 'grabaciones'
            carpeta.mkdir(exist_ok=True)
            sello = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
            stats = save_wav(carpeta / f'pregunta-{sello}.wav', rate, raw)
            self.ui(estado='🧠 Pensando…')
            d = cerebro.preguntar(cfg, cerebro.wav_para_ia(rate, raw))
            t1 = time.monotonic()
            self.ui(txt_pregunta=d['transcripcion'] or '(no se entendió)', txt_respuesta=d['respuesta'])
            origen = 'solo texto'
            if (self.laptop or self.parlante) and d['respuesta']:
                self.ui(estado='🗣️ Generando voz…')
                audio, origen = cerebro.sintetizar(cfg, d['respuesta'])
            t2 = time.monotonic()
            if self.laptop and d['respuesta']:
                self.ui(estado='💻 Leyendo la respuesta en la laptop…')
                cerebro.reproducir_laptop(audio)
            if self.parlante and d['respuesta']:
                pcm = cerebro.para_parlante(cfg, audio)
                (carpeta / f'respuesta-{sello}.pcm').write_bytes(pcm)
                self.ui(estado='🔊 Respondiendo por el parlante…')
                protocolo.reproducir(link, pcm)
            with open(ROOT / 'historial.txt', 'a', encoding='utf-8') as h:
                h.write(f"{sello}\tP: {d['transcripcion']}\tR: {d['respuesta']}\n")
            self.ui(estado='Lista. Pulsa Hablar o BOOT para otra pregunta.',
                    info=f"{d['modelo']}{' · 🔎 buscó en la web' if d.get('busco') else ''} · voz: {origen} · IA {t1 - t0:.1f} s · voz {t2 - t1:.1f} s · "
                         f"ADC {stats['min']}–{stats['max']}" +
                         (' · ¡posible saturación!' if stats['near_rails'] else ''))
        except Exception as e:
            self.ui(estado=f'⚠️ {e}')
            try:
                protocolo.vaciar(link)   # que la siguiente pregunta empiece limpia
            except Exception:
                pass


App().root.mainloop()
