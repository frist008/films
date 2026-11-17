"""R333 (2026-11-17): Python 3.15 has no onnxruntime; faster-whisper's VAD filter runs on pysilero-vad (the same Silero v6
model) through trailer_lang._PySileroVAD — owner: «А альтернативы нет ему? Если нет то ок 3.13»."""
import importlib.util
import unittest

import numpy as np

HAVE = importlib.util.find_spec("pysilero_vad") and importlib.util.find_spec("faster_whisper")


@unittest.skipUnless(HAVE, "pysilero-vad / faster-whisper not installed")
class PySileroVad(unittest.TestCase):
    def test_same_shape_as_the_onnx_model(self):
        from films.sources import trailer_lang as tl
        probs = tl._PySileroVAD()(np.zeros(512 * 10, dtype="float32"))
        self.assertEqual(probs.shape, (10, 1))
        self.assertTrue((probs < 0.5).all())                  # silence is not speech

    def test_vad_filter_works_without_onnxruntime(self):
        from films.sources import trailer_lang as tl
        backend = tl.vad_backend()
        if backend == "onnxruntime":
            self.skipTest("onnxruntime is installed: faster-whisper's own VAD")
        from faster_whisper.vad import VadOptions, get_speech_timestamps
        sr = 16000
        self.assertEqual(get_speech_timestamps(np.zeros(sr * 3, "float32"), VadOptions()), [])   # was RuntimeError


if __name__ == "__main__":
    unittest.main()
