"""Voz -> Gemini (transcripcion + respuesta) -> [Groq busca en la web si hace falta]
-> voz sintetizada -> PCM para la C3.

Si Gemini no responde (saturado o sin cuota), Groq hace todo: Whisper transcribe y
gpt-oss responde (buscando en la web si lo necesita)."""
import array
import asyncio
import base64
import datetime
import io
import json
import math
import os
import re
import sys
import tempfile
import time
import wave
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
URL = 'https://generativelanguage.googleapis.com/v1beta/models/{}:generateContent'
GROQ = 'https://api.groq.com/openai/v1'
GROQ_MODELOS = ['openai/gpt-oss-120b', 'openai/gpt-oss-20b']   # tienen busqueda web integrada
GROQ_WHISPER = 'whisper-large-v3-turbo'

PERSONA = (
    'Eres {nombre}, un asistente de voz tipo Alexa construido por estudiantes de '
    'Ingenieria Biomedica de Yachay Tech con un ESP32-C3. '
)

ESTILO = (
    'La respuesta va en espanol, maximo {oraciones} oraciones y {palabras} palabras, '
    'sin emojis, listas, markdown, enlaces ni citas de fuentes, porque se leera en voz alta. '
    'Escribe los numeros como se pronuncian si ayudan a leerlos. '
)

PROMPT = (
    'Recibes un audio corto en espanol grabado con un microfono electret, puede tener ruido. '
    'Responde SOLO con JSON: {{"transcripcion": "...", "respuesta": "...", "buscar": "..."}}. '
    'Si no se entiende voz, deja transcripcion vacia y responde que no escuchaste bien. '
    'El campo buscar: si para responder bien necesitas datos actuales o que no conoces con '
    'seguridad (noticias, resultados deportivos, precios, eventos, hechos recientes) y NO estan '
    'en este mensaje, escribe ahi una consulta corta para buscar en internet y en respuesta pon '
    'que por ahora no puedes consultarlo. Si no hace falta buscar (saludos, conocimiento general, '
    'la hora, la fecha o el clima de este mensaje), deja buscar vacio.'
)

CONTEXTO = (
    ' Fecha y hora actual: {ahora}. El usuario esta en {ciudad}. {clima}'
    'Nunca inventes datos actuales (clima, noticias, resultados, precios, eventos): '
    'usa los datos de este mensaje o de la busqueda; si no tienes el dato, di con '
    'honestidad que no puedes consultarlo.'
)

PROMPT_GROQ = (
    'Responde a la pregunta del usuario. Si necesitas datos actuales o que no conoces con '
    'seguridad, buscalos en internet antes de responder. Da solo la respuesta final.'
)

# Codigos WMO de Open-Meteo -> descripcion
WMO = {0: 'despejado', 1: 'mayormente despejado', 2: 'parcialmente nublado', 3: 'nublado',
       45: 'con niebla', 48: 'con niebla', 51: 'con llovizna ligera', 53: 'con llovizna',
       55: 'con llovizna intensa', 61: 'con lluvia ligera', 63: 'con lluvia', 65: 'con lluvia fuerte',
       80: 'con chubascos ligeros', 81: 'con chubascos', 82: 'con chubascos fuertes',
       95: 'con tormenta', 96: 'con tormenta y granizo', 99: 'con tormenta y granizo'}
_clima_cache = [0.0, '']

DIAS = ['lunes', 'martes', 'miercoles', 'jueves', 'viernes', 'sabado', 'domingo']
MESES = ['enero', 'febrero', 'marzo', 'abril', 'mayo', 'junio', 'julio', 'agosto',
         'septiembre', 'octubre', 'noviembre', 'diciembre']


def ahora_texto():
    t = datetime.datetime.now()
    return f'{DIAS[t.weekday()]} {t.day} de {MESES[t.month - 1]} de {t.year}, {t:%H:%M}'


def clima_texto(cfg):
    """Clima real de la ciudad configurada (Open-Meteo, gratis y sin clave). Cache de 10 min."""
    if 'lat' not in cfg or 'lon' not in cfg:
        return ''
    if time.monotonic() - _clima_cache[0] < 600:
        return _clima_cache[1]
    try:
        r = requests.get('https://api.open-meteo.com/v1/forecast', timeout=5, params={
            'latitude': cfg['lat'], 'longitude': cfg['lon'], 'timezone': 'auto', 'forecast_days': 2,
            'current': 'temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m',
            'daily': 'weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max'})
        r.raise_for_status()
        c, d = r.json()['current'], r.json()['daily']
        dia = lambda i: (f"{WMO.get(d['weather_code'][i], 'variable')}, minima {d['temperature_2m_min'][i]:.0f} "
                         f"y maxima {d['temperature_2m_max'][i]:.0f} grados, probabilidad de lluvia "
                         f"{d['precipitation_probability_max'][i] or 0} por ciento")
        texto = (f"Datos reales de Open-Meteo para {cfg.get('ciudad')}: ahora {c['temperature_2m']:.0f} grados, "
                 f"{WMO.get(c['weather_code'], 'variable')}, humedad {c['relative_humidity_2m']} por ciento, "
                 f"viento {c['wind_speed_10m']:.0f} km/h. Hoy: {dia(0)}. Manana: {dia(1)}. ")
    except Exception:
        texto = ''
    _clima_cache[:] = [time.monotonic(), texto]
    return texto


def instrucciones(cfg, tarea=PROMPT):
    texto = (PERSONA.format(nombre=cfg.get('nombre', 'Yachi')) + tarea + ' ' +
             ESTILO.format(oraciones=cfg.get('max_oraciones', 4), palabras=cfg.get('max_palabras', 80)))
    return texto + CONTEXTO.format(ahora=ahora_texto(), ciudad=cfg.get('ciudad', 'Urcuqui, Imbabura, Ecuador'),
                                   clima=clima_texto(cfg))


def cargar_config():
    cfg = json.loads((ROOT / 'config.json').read_text(encoding='utf-8'))
    cfg['gemini_api_key'] = os.environ.get('GEMINI_API_KEY') or cfg.get('gemini_api_key', '')
    cfg['groq_api_key'] = os.environ.get('GROQ_API_KEY') or cfg.get('groq_api_key', '')
    if cfg['groq_api_key'].startswith('PEGA'):
        cfg['groq_api_key'] = ''
    if (not cfg['gemini_api_key'] or cfg['gemini_api_key'].startswith('PEGA')) and not cfg['groq_api_key']:
        raise RuntimeError('Falta la clave de Gemini o de Groq: escribela en config.json.')
    return cfg


def wav_para_ia(rate, raw):
    """Quita continua, normaliza y empaqueta en WAV en memoria."""
    mean = sum(raw) / len(raw)
    alpha = math.exp(-2 * math.pi * 80 / rate)
    prev, y, out = raw[0] - mean, 0.0, []
    for s in raw:
        x = s - mean
        y = alpha * (y + x - prev)
        prev = x
        out.append(y)
    peak = max(1.0, max(abs(v) for v in out))
    g = 0.9 * 32767 / peak
    pcm = array.array('h', (int(v * g) for v in out))
    if sys.byteorder != 'little':
        pcm.byteswap()
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate); w.writeframes(pcm.tobytes())
    return buf.getvalue()


def extraer_json(texto):
    texto = re.sub(r'^```(json)?|```$', '', texto.strip(), flags=re.M).strip()
    try:
        d = json.loads(texto)
    except json.JSONDecodeError:
        m = re.search(r'\{.*\}', texto, re.S)
        if not m:
            return {'transcripcion': '', 'respuesta': texto}
        d = json.loads(m.group(0))
    return {'transcripcion': str(d.get('transcripcion', '')).strip(),
            'respuesta': str(d.get('respuesta', '')).strip(),
            'buscar': str(d.get('buscar') or '').strip()}


def limpiar_para_voz(texto):
    """Quita citas, enlaces y markdown que el modelo de busqueda a veces agrega."""
    texto = re.sub(r'【[^】]*】|\[\d+\]', '', texto)                 # citas de fuentes
    texto = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', texto)          # [texto](enlace)
    texto = re.sub(r'https?://\S+', '', texto)
    texto = re.sub(r'[*#`_|]', '', texto)
    texto = re.sub(r'[   ]', ' ', texto)
    texto = re.sub(r'(\d)\s*[–—-]\s*(\d)', r'\1 a \2', texto)  # marcador 2-0 -> 2 a 0
    return re.sub(r'\s+', ' ', texto).strip()


def groq_responder(cfg, pregunta, obligar_busqueda):
    """gpt-oss en Groq con busqueda web integrada. Devuelve (texto, modelo, busco) o None."""
    if not cfg.get('groq_api_key'):
        return None
    for modelo in cfg.get('groq_modelos', GROQ_MODELOS):
        body = {'model': modelo,
                'messages': [{'role': 'system', 'content': instrucciones(cfg, PROMPT_GROQ)},
                             {'role': 'user', 'content': pregunta}]}
        if cfg.get('busqueda', True):
            body['tools'] = [{'type': 'browser_search'}]
            body['tool_choice'] = 'required' if obligar_busqueda else 'auto'
        try:
            r = requests.post(f'{GROQ}/chat/completions', json=body, timeout=60,
                              headers={'Authorization': 'Bearer ' + cfg['groq_api_key']})
        except requests.RequestException:
            continue
        if r.status_code != 200:
            continue          # saturado, sin cuota o modelo retirado: probar el siguiente
        msg = r.json()['choices'][0]['message']
        texto = limpiar_para_voz(msg.get('content') or '')
        if texto:
            busco = any('search' in (t.get('type') or '') for t in msg.get('executed_tools') or [])
            return texto, modelo, busco
    return None


def groq_transcribir(cfg, wav_bytes):
    r = requests.post(f'{GROQ}/audio/transcriptions', timeout=60,
                      headers={'Authorization': 'Bearer ' + cfg['groq_api_key']},
                      files={'file': ('pregunta.wav', wav_bytes, 'audio/wav')},
                      data={'model': cfg.get('groq_whisper', GROQ_WHISPER), 'language': 'es'})
    if r.status_code != 200:
        raise RuntimeError(f'Groq Whisper {r.status_code}: {r.text[:200]}')
    return r.json().get('text', '').strip()


def preguntar_gemini(cfg, wav_bytes):
    """Gemini escucha el audio y responde. Devuelve el dict o (None, motivo) si ningun modelo responde."""
    body = {
        'system_instruction': {'parts': [{'text': instrucciones(cfg)}]},
        'contents': [{'role': 'user', 'parts': [
            {'inline_data': {'mime_type': 'audio/wav', 'data': base64.b64encode(wav_bytes).decode()}},
            {'text': 'Responde a lo que dice este audio.'}]}],
        'generationConfig': {'responseMimeType': 'application/json'},
    }
    ultimo = 'sin clave de Gemini'
    if not cfg.get('gemini_api_key') or cfg['gemini_api_key'].startswith('PEGA'):
        return None, ultimo
    for modelo in cfg['modelos']:
        try:
            r = requests.post(URL.format(modelo), json=body, timeout=60,
                              headers={'x-goog-api-key': cfg['gemini_api_key']})
        except requests.RequestException as e:
            ultimo = f'{modelo}: sin conexion ({type(e).__name__})'
            continue
        if r.status_code == 404 or (r.status_code == 400 and 'not found' in r.text.lower()):
            ultimo = f'{modelo} no disponible'
            continue
        if r.status_code == 429:
            ultimo = f'{modelo}: cuota agotada'
            continue
        if r.status_code in (500, 503):
            ultimo = f'{modelo}: saturado ({r.status_code})'
            continue
        if r.status_code != 200:
            ultimo = f'Gemini {r.status_code}: {r.text[:200]}'
            continue
        partes = r.json()['candidates'][0]['content']['parts']
        texto = ''.join(p.get('text', '') for p in partes if not p.get('thought'))
        d = extraer_json(texto)
        d['modelo'] = modelo
        return d, ''
    return None, ultimo


def preguntar(cfg, wav_bytes):
    """Gemini entiende el audio; si hace falta un dato actual, Groq lo busca en la web.
    Si Gemini no responde, Groq hace todo (Whisper + gpt-oss)."""
    d, motivo = preguntar_gemini(cfg, wav_bytes)
    if d is not None:
        d['busco'] = False
        if d['buscar'] and cfg.get('busqueda', True):
            pregunta = d['transcripcion'] or d['buscar']
            res = groq_responder(cfg, f'{pregunta}\n(Consulta sugerida para buscar: {d["buscar"]})', True)
            if res:
                d['respuesta'], modelo_groq, d['busco'] = res
                d['modelo'] += f' + {modelo_groq}'
        return d
    if not cfg.get('groq_api_key'):
        raise RuntimeError('Ningun modelo respondio: ' + motivo)
    # Respaldo: Gemini caido o sin cuota
    transcripcion = groq_transcribir(cfg, wav_bytes)
    if not transcripcion:
        return {'transcripcion': '', 'respuesta': 'No te escuche bien, puedes repetirlo?',
                'buscar': '', 'modelo': 'groq/' + cfg.get('groq_whisper', GROQ_WHISPER), 'busco': False}
    res = groq_responder(cfg, transcripcion, False)
    if not res:
        raise RuntimeError(f'Ningun modelo respondio ({motivo}; Groq tampoco)')
    texto, modelo_groq, busco = res
    return {'transcripcion': transcripcion, 'respuesta': texto, 'buscar': '',
            'modelo': f'Groq {modelo_groq} (Gemini: {motivo})', 'busco': busco}


def tts_edge(texto, voz, velocidad):
    import edge_tts

    async def run():
        audio = b''
        async for c in edge_tts.Communicate(texto, voz, rate=velocidad).stream():
            if c['type'] == 'audio':
                audio += c['data']
        return audio
    return asyncio.run(run())


def tts_windows(texto):
    """Respaldo sin Internet: voz de Windows (SAPI)."""
    import pyttsx3
    try:
        import pythoncom
        pythoncom.CoInitialize()   # necesario en hilos que no son el principal
    except ImportError:
        pass
    ruta = Path(tempfile.gettempdir()) / 'minialexa_tts.wav'
    eng = pyttsx3.init()
    eng.save_to_file(texto, str(ruta))
    eng.runAndWait()
    return ruta.read_bytes()


def decodificar(audio_bytes, rate=16000):
    import miniaudio
    d = miniaudio.decode(audio_bytes, output_format=miniaudio.SampleFormat.SIGNED16,
                         nchannels=1, sample_rate=rate)
    return array.array('h', d.samples)


def preparar_parlante(muestras, rate=16000, volumen=0.6):
    """Pasa-altos 150 Hz (el parlante no reproduce graves), normaliza, rampas y silencios."""
    alpha = math.exp(-2 * math.pi * 150 / rate)
    prev, y, out = 0.0, 0.0, []
    for s in muestras:
        y = alpha * (y + s - prev)
        prev = s
        out.append(y)
    peak = max(1.0, max((abs(v) for v in out), default=1.0))
    g = volumen * 32767 / peak
    n = len(out)
    rampa = int(0.02 * rate)
    for i in range(min(rampa, n)):
        out[i] *= i / rampa
        out[n - 1 - i] *= i / rampa
    pcm = array.array('h', [0] * int(0.1 * rate))
    pcm.extend(max(-32767, min(32767, int(v * g))) for v in out)
    pcm.extend([0] * int(0.15 * rate))
    if sys.byteorder != 'little':
        pcm.byteswap()
    return pcm.tobytes()


def sintetizar(cfg, texto):
    """Devuelve (audio comprimido o WAV, origen). Edge-TTS con respaldo de Windows."""
    try:
        return tts_edge(texto, cfg.get('voz', 'es-EC-AndreaNeural'), cfg.get('velocidad', '+0%')), 'edge-tts'
    except Exception:
        return tts_windows(texto), 'voz de Windows'


def para_parlante(cfg, audio):
    """Audio sintetizado -> PCM 16 kHz listo para la C3."""
    return preparar_parlante(decodificar(audio), volumen=float(cfg.get('volumen', 0.6)))


def reproducir_laptop(audio, rate=24000):
    """Reproduce por los parlantes de la laptop (bloquea hasta terminar)."""
    import winsound
    pcm = decodificar(audio, rate)
    if sys.byteorder != 'little':
        pcm.byteswap()
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate); w.writeframes(pcm.tobytes())
    winsound.PlaySound(buf.getvalue(), winsound.SND_MEMORY)


def hablar(cfg, texto):
    audio, origen = sintetizar(cfg, texto)
    return para_parlante(cfg, audio), origen
