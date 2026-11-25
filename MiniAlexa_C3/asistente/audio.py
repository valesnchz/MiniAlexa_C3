"""Protocolo binario y WAV; sin dependencias de interfaz."""
import array
import math
import struct
import sys
import time
import wave
import zlib

MAGIC=b'C3AUDIO1'
def read_exact(port,n,deadline,phase='respuesta'):
    data=bytearray()
    while len(data)<n:
        if time.monotonic()>deadline:
            raise TimeoutError(f'Tiempo agotado durante {phase}: llegaron {len(data)} de {n} bytes. '+
                ('La cabecera llegó pero faltan muestras. Carga el firmware corregido que envía bloques USB.' if phase=='audio' else 'Comprueba COM, firmware y que el puerto no esté ocupado.'))
        data.extend(port.read(n-len(data)))
    return bytes(data)

def capture(port):
    port.reset_input_buffer()
    port.write(b'R');port.flush()
    deadline=time.monotonic()+18
    window=bytearray()
    while bytes(window)!=MAGIC:
        window.extend(read_exact(port,1,deadline,'búsqueda de cabecera'))
        if len(window)>8:del window[0]
    rate,count,crc,flags=struct.unpack('<IIII',read_exact(port,16,deadline,'cabecera'))
    if flags:raise RuntimeError(f'La C3 rechazó la captura (banderas {flags}). No se guarda audio incompleto.')
    if rate!=16000 or count!=64000:raise ValueError('Formato inesperado: utiliza el firmware de micrófono de esta carpeta.')
    data=read_exact(port,count*2,time.monotonic()+20,'audio')
    if zlib.crc32(data)!=crc:raise ValueError('CRC incorrecto: transferencia incompleta o corrupta. Repite la grabación.')
    raw=array.array('H');raw.frombytes(data)
    if sys.byteorder!='little':raw.byteswap()
    if any(v>4095 for v in raw):raise ValueError('La captura contiene valores fuera del rango ADC de 12 bits.')
    return rate,raw

def save_wav(path,rate,raw):
    if not raw:raise ValueError('Sin muestras')
    mean=sum(raw)/len(raw)
    # Quitar continua y rumble con filtro pasa-altos de 80 Hz, un polo.
    alpha=math.exp(-2*math.pi*80/rate)
    prev=raw[0]-mean;y=0.0;filtered=[]
    for sample in raw:
        x=sample-mean;y=alpha*(y+x-prev);prev=x;filtered.append(y)
    peak=max(abs(x) for x in filtered)
    # Ganancia fija: no amplificar automáticamente el ruido de un micrófono ausente.
    pcm=array.array('h',(max(-32768,min(32767,round(x*16))) for x in filtered))
    if sys.byteorder!='little':pcm.byteswap()
    with wave.open(str(path),'wb') as w:
        w.setnchannels(1);w.setsampwidth(2);w.setframerate(rate);w.writeframes(pcm.tobytes())
    rms=math.sqrt(sum((x-mean)**2 for x in raw)/len(raw))
    return {'mean':mean,'min':min(raw),'max':max(raw),'rms':rms,'near_rails':sum(x<=5 or x>=4090 for x in raw),'peak':peak,'seconds':len(raw)/rate}
