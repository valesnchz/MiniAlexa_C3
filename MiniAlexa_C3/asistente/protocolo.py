"""Protocolo USB con la C3: grabar ('R' o boton BOOT) y reproducir ('P')."""
import array
import struct
import sys
import time
import zlib

from audio import read_exact

MAGIC = b'C3AUDIO1'
CHUNK = 2048      # debe coincidir con CHUNK del firmware
CREDITOS = 3      # bloques en vuelo; caben en el buffer RX de 8 KB de la C3


def leer_paquete(port):
    """Lee lo que sigue a MAGIC: cabecera + audio. Devuelve (rate, raw).

    Si antes llego un paquete viejo incompleto (p. ej. se pulso BOOT con el asistente
    cerrado y quedaron restos en el buffer USB de la C3), el paquete nuevo aparece dentro
    de los datos: se busca su MAGIC y se resincroniza en vez de fallar.
    """
    pendiente = b''
    for _ in range(3):
        if len(pendiente) < 16:
            pendiente += read_exact(port, 16 - len(pendiente), time.monotonic() + 20, 'cabecera')
        rate, count, crc, flags = struct.unpack('<IIII', pendiente[:16])
        if flags:
            raise RuntimeError(f'La C3 rechazo la captura (banderas {flags}).')
        if rate != 16000 or not 0 < count <= 16000 * 15:
            raise ValueError('Formato inesperado: carga el firmware de MiniAlexa.')
        n = count // 2 * 3
        data = pendiente[16:16 + n]
        data += read_exact(port, n - len(data), time.monotonic() + 20, 'audio')
        if zlib.crc32(data) == crc:
            return rate, desempaquetar(data)
        i = data.rfind(MAGIC)
        if i < 0:
            raise ValueError('CRC incorrecto: repite la pregunta.')
        pendiente = data[i + len(MAGIC):]   # empezar de nuevo desde el paquete mas reciente
    raise ValueError('CRC incorrecto: repite la pregunta.')


def vaciar(port, silencio=0.4, maximo=3.0):
    """Descarta lo que llegue de la C3 hasta que haya `silencio` segundos sin datos."""
    fin, quieto = time.monotonic() + maximo, time.monotonic() + silencio
    while time.monotonic() < min(fin, quieto):
        if port.read(4096):
            quieto = time.monotonic() + silencio
    port.reset_input_buffer()


def desempaquetar(data):
    """12 bits empaquetados (2 muestras en 3 bytes) -> array de uint16."""
    raw = array.array('H', bytes(len(data) // 3 * 4))
    for j in range(len(data) // 3):
        b0, b1, b2 = data[3 * j], data[3 * j + 1], data[3 * j + 2]
        raw[2 * j] = b0 | (b1 & 0x0F) << 8
        raw[2 * j + 1] = (b1 >> 4) | b2 << 4
    return raw


def reproducir(port, pcm, aviso=None):
    """Envia PCM int16 mono 16 kHz con control de flujo por creditos 'K'."""
    if len(pcm) % 2:
        pcm = pcm[:-1]
    port.write(b'P' + struct.pack('<I', len(pcm)))
    port.flush()
    creditos, enviado = CREDITOS, 0
    limite = time.monotonic() + 5
    while enviado < len(pcm):
        while creditos == 0:
            b = port.read(1)
            if b == b'K':
                creditos += 1
            elif not b:
                if time.monotonic() > limite:
                    raise TimeoutError('La C3 dejo de pedir audio durante la reproduccion.')
                continue
        bloque = pcm[enviado:enviado + CHUNK]
        port.write(bloque)
        enviado += len(bloque)
        creditos -= 1
        limite = time.monotonic() + 5
        if aviso:
            aviso(enviado / len(pcm))
    limite = time.monotonic() + 10
    while True:  # esperar fin real del sonido
        b = port.read(1)
        if b == b'D':
            return
        if not b and time.monotonic() > limite:
            raise TimeoutError('No llego la confirmacion de fin de reproduccion.')
