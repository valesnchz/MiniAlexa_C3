import array
import io
import json
import math
import struct
import unittest
import wave
import zlib
from unittest import mock

import cerebro
import protocolo


class FakeC3:
    """Simula la C3: entrega 'K' por cada bloque completo y 'D' al final."""
    def __init__(self, payload=b''):
        self.rx = bytearray(payload)
        self.recibido = bytearray()
        self.total = None
        self.k_dados = 0
        self.fin = False
    def write(self, b):
        self.recibido += b
        if self.total is None and len(self.recibido) >= 5:
            self.total = struct.unpack('<I', self.recibido[1:5])[0]
    def flush(self): pass
    def reset_input_buffer(self): pass
    def read(self, n):
        if self.rx:
            out = bytes(self.rx[:n]); del self.rx[:n]; return out
        datos = len(self.recibido) - 5
        bloques = (datos + protocolo.CHUNK - 1) // protocolo.CHUNK
        if self.k_dados < bloques:
            self.k_dados += 1
            return b'K'
        if self.total is not None and datos >= self.total and not self.fin:
            self.fin = True
            return b'D'
        return b''


class Pruebas(unittest.TestCase):
    def test_reproducir_respeta_creditos(self):
        pcm = bytes(range(256)) * 100  # 25600 bytes, 13 bloques
        c3 = FakeC3()
        protocolo.reproducir(c3, pcm)
        self.assertEqual(c3.recibido[:1], b'P')
        self.assertEqual(bytes(c3.recibido[5:]), pcm)
        self.assertTrue(c3.fin)

    def test_leer_paquete(self):
        raw = [2048 + int(300 * math.sin(i / 10)) for i in range(64000)]
        data = bytearray()
        for a, b in zip(raw[0::2], raw[1::2]):
            data += bytes([a & 0xFF, (a >> 8) | (b & 0x0F) << 4, b >> 4])
        data = bytes(data)
        c3 = FakeC3(struct.pack('<IIII', 16000, 64000, zlib.crc32(data), 0) + data)
        rate, out = protocolo.leer_paquete(c3)
        self.assertEqual((rate, list(out)), (16000, raw))

    def test_resincroniza_tras_paquete_viejo(self):
        raw = [2048 + int(300 * math.sin(i / 7)) for i in range(96000)]
        data = bytearray()
        for a, b in zip(raw[0::2], raw[1::2]):
            data += bytes([a & 0xFF, (a >> 8) | (b & 0x0F) << 4, b >> 4])
        cab = struct.pack('<IIII', 16000, 96000, zlib.crc32(data), 0)
        viejo = cab + bytes(data[:3000])            # paquete incompleto atascado en la C3
        nuevo = b'S' + protocolo.MAGIC + cab + bytes(data)
        rate, out = protocolo.leer_paquete(FakeC3(viejo + nuevo))
        self.assertEqual(list(out), raw)

    def test_json_con_basura(self):
        d = cerebro.extraer_json('```json\n{"transcripcion":"hola","respuesta":"Hola, ¿qué tal?"}\n```')
        self.assertEqual(d['respuesta'], 'Hola, ¿qué tal?')

    def test_wav_y_parlante(self):
        raw = array.array('H', (2048 + int(500 * math.sin(2 * math.pi * 300 * i / 16000)) for i in range(64000)))
        wav = cerebro.wav_para_ia(16000, raw)
        muestras = cerebro.decodificar(wav)
        self.assertEqual(len(muestras), 64000)
        pcm = cerebro.preparar_parlante(muestras, volumen=0.5)
        x = array.array('h'); x.frombytes(pcm)
        self.assertLessEqual(max(abs(v) for v in x), 0.5 * 32767 + 1)
        self.assertEqual(len(x), 64000 + 1600 + 2400)

    def test_gemini_simulado(self):
        cfg = {'gemini_api_key': 'x', 'modelos': ['no-existe', 'gemini-ok'], 'nombre': 'Yachi'}
        r404 = mock.Mock(status_code=404, text='model not found')
        r200 = mock.Mock(status_code=200)
        r200.json.return_value = {'candidates': [{'content': {'parts': [
            {'text': '{"transcripcion": "qué hora es", "respuesta": "No tengo reloj."}'}]}}]}
        with mock.patch('cerebro.requests.post', side_effect=[r404, r200]) as p:
            d = cerebro.preguntar(cfg, b'RIFF')
        self.assertEqual((d['modelo'], d['respuesta']), ('gemini-ok', 'No tengo reloj.'))
        body = p.call_args.kwargs['json']
        self.assertEqual(body['contents'][0]['parts'][0]['inline_data']['mime_type'], 'audio/wav')

    def test_gemini_saturado_pasa_al_siguiente(self):
        cfg = {'gemini_api_key': 'x', 'modelos': ['saturado', 'gemini-ok'], 'nombre': 'Yachi'}
        r503 = mock.Mock(status_code=503, text='high demand')
        r200 = mock.Mock(status_code=200)
        r200.json.return_value = {'candidates': [{'content': {'parts': [
            {'text': '{"transcripcion": "", "respuesta": "No te escuche bien."}'}]}}]}
        with mock.patch('cerebro.requests.post', side_effect=[r503, r200]):
            d = cerebro.preguntar(cfg, b'RIFF')
        self.assertEqual(d['modelo'], 'gemini-ok')

    def test_gemini_pide_busqueda_y_groq_busca(self):
        cfg = {'gemini_api_key': 'x', 'groq_api_key': 'g', 'modelos': ['gemini-ok'], 'nombre': 'Yachi'}
        gem = mock.Mock(status_code=200)
        gem.json.return_value = {'candidates': [{'content': {'parts': [{'text':
            '{"transcripcion": "quien gano ayer", "respuesta": "No puedo consultarlo.", "buscar": "resultado Ecuador"}'}]}}]}
        groq = mock.Mock(status_code=200)
        groq.json.return_value = {'choices': [{'message': {
            'content': 'Ecuador gano **2 – 0** a Bolivia【3†L4】.',
            'executed_tools': [{'type': 'browser_search'}]}}]}
        with mock.patch('cerebro.requests.post', side_effect=[gem, groq]) as p:
            d = cerebro.preguntar(cfg, b'RIFF')
        self.assertEqual(d['respuesta'], 'Ecuador gano 2 a 0 a Bolivia.')
        self.assertTrue(d['busco'])
        self.assertEqual(d['modelo'], 'gemini-ok + openai/gpt-oss-120b')
        self.assertEqual(p.call_args.kwargs['json']['tool_choice'], 'required')

    def test_sin_busqueda_no_llama_a_groq(self):
        cfg = {'gemini_api_key': 'x', 'groq_api_key': 'g', 'modelos': ['gemini-ok'], 'nombre': 'Yachi'}
        gem = mock.Mock(status_code=200)
        gem.json.return_value = {'candidates': [{'content': {'parts': [{'text':
            '{"transcripcion": "hola", "respuesta": "Hola!", "buscar": ""}'}]}}]}
        with mock.patch('cerebro.requests.post', side_effect=[gem]) as p:
            d = cerebro.preguntar(cfg, b'RIFF')
        self.assertEqual((d['respuesta'], p.call_count, d['busco']), ('Hola!', 1, False))

    def test_gemini_caido_responde_groq(self):
        cfg = {'gemini_api_key': 'x', 'groq_api_key': 'g', 'modelos': ['a', 'b'], 'nombre': 'Yachi'}
        r503 = mock.Mock(status_code=503, text='high demand')
        r429 = mock.Mock(status_code=429, text='quota')
        whisper = mock.Mock(status_code=200)
        whisper.json.return_value = {'text': ' Que hora es? '}
        groq = mock.Mock(status_code=200)
        groq.json.return_value = {'choices': [{'message': {'content': 'Son las tres.'}}]}
        with mock.patch('cerebro.requests.post', side_effect=[r503, r429, whisper, groq]):
            d = cerebro.preguntar(cfg, b'RIFF')
        self.assertEqual((d['transcripcion'], d['respuesta'], d['busco']), ('Que hora es?', 'Son las tres.', False))
        self.assertTrue(d['modelo'].startswith('Groq openai/gpt-oss-120b'))


if __name__ == '__main__':
    unittest.main()
