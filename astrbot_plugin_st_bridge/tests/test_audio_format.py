"""V1a: TTS audio is converted to 24 kHz mono 16-bit before it goes to QQ (pure function, numpy)."""

import io
import math
import unittest
import wave

import numpy as np

from package_context import install_plugin_namespace

install_plugin_namespace()

from data.plugins.astrbot_plugin_st_bridge.st_bridge_core.audio_format import TARGET_RATE, to_qq_voice_wav


class FakeLogger:
    def __init__(self):
        self.warnings = []

    def warning(self, message):
        self.warnings.append(message)


def make_wav(rate, freq=1000.0, seconds=1.0, channels=1, width=2, amp=0.5, fmt=None):
    t = np.arange(int(rate * seconds)) / rate
    mono = amp * np.sin(2 * math.pi * freq * t)
    data = np.repeat(mono[:, None], channels, axis=1) if channels > 1 else mono[:, None]
    if width == 2:
        raw = np.rint(data * 32767).astype("<i2").tobytes()
    else:
        raw = np.rint(data * 2147483647).astype("<i4").tobytes()
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(width)
        writer.setframerate(rate)
        writer.writeframes(raw)
    return buffer.getvalue()


def read_wav(data):
    with wave.open(io.BytesIO(data), "rb") as reader:
        frames = reader.readframes(reader.getnframes())
        return reader.getframerate(), reader.getnchannels(), reader.getsampwidth(), np.frombuffer(frames, "<i2").astype(float)


def level_db(samples, reference_amp):
    core = samples[len(samples) // 10 : -len(samples) // 10]
    rms = math.sqrt(float(np.mean(core ** 2))) / 32767
    return 20 * math.log10(max(rms, 1e-12) / (reference_amp / math.sqrt(2)))


class ConversionTests(unittest.TestCase):
    def assert_converted(self, data, source_rate, seconds=1.0):
        rate, channels, width, samples = read_wav(to_qq_voice_wav(data))
        self.assertEqual((rate, channels, width), (TARGET_RATE, 1, 2))
        self.assertLessEqual(abs(len(samples) - round(source_rate * seconds * TARGET_RATE / source_rate)), 1)
        return samples

    def test_common_sources_become_24k_mono_16bit(self):
        for rate in (48000, 44100, 32000):
            with self.subTest(rate=rate):
                self.assert_converted(make_wav(rate), rate)

    def test_stereo_and_32bit_sources(self):
        self.assert_converted(make_wav(48000, channels=2), 48000)
        self.assert_converted(make_wav(32000, width=4), 32000)
        samples = self.assert_converted(make_wav(48000, channels=2, width=4), 48000)
        self.assertAlmostEqual(level_db(samples, 0.5), 0.0, delta=1.0)

    def test_passband_level_is_kept(self):
        for rate in (48000, 44100, 32000):
            with self.subTest(rate=rate):
                samples = self.assert_converted(make_wav(rate, 1000.0), rate)
                self.assertAlmostEqual(level_db(samples, 0.5), 0.0, delta=1.0)

    def test_content_above_the_new_nyquist_is_removed_not_folded_back(self):
        for rate in (48000, 44100):
            with self.subTest(rate=rate):
                samples = self.assert_converted(make_wav(rate, 15000.0), rate)
                self.assertLessEqual(level_db(samples, 0.5), -40.0)

    def test_already_24k_mono_16bit_is_returned_untouched(self):
        data = make_wav(24000)
        self.assertIs(to_qq_voice_wav(data), data)

    def test_lower_rates_are_not_upsampled(self):
        mono16 = make_wav(16000)
        self.assertIs(to_qq_voice_wav(mono16), mono16)
        rate, channels, width, samples = read_wav(to_qq_voice_wav(make_wav(16000, channels=2)))
        self.assertEqual((rate, channels, width, len(samples)), (16000, 1, 2, 16000))
        rate, channels, width, _ = read_wav(to_qq_voice_wav(make_wav(8000, width=4)))
        self.assertEqual((rate, channels, width), (8000, 1, 2))

    def test_loud_signal_is_clipped_not_wrapped(self):
        samples = self.assert_converted(make_wav(48000, 100.0, amp=1.0), 48000)
        self.assertGreater(np.max(samples), 30000)
        self.assertLess(np.min(samples), -30000)

    def test_very_short_audio_is_handled(self):
        for frames in (0, 1, 5):
            with self.subTest(frames=frames):
                buffer = io.BytesIO()
                with wave.open(buffer, "wb") as writer:
                    writer.setnchannels(1)
                    writer.setsampwidth(2)
                    writer.setframerate(48000)
                    writer.writeframes(b"\x10\x00" * frames)
                rate, channels, _, samples = read_wav(to_qq_voice_wav(buffer.getvalue()))
                self.assertEqual((rate, channels, len(samples)), (24000, 1, round(frames / 2)))


class FailureTests(unittest.TestCase):
    def test_garbage_and_float_wav_return_the_original_bytes_with_a_warning(self):
        logger = FakeLogger()
        garbage = b"wav-bytes"
        self.assertIs(to_qq_voice_wav(garbage, logger), garbage)
        self.assertEqual(len(logger.warnings), 1)
        float_wav = make_wav(48000)
        header = bytearray(float_wav)
        header[20:22] = (3).to_bytes(2, "little")  # WAVE_FORMAT_IEEE_FLOAT
        logger = FakeLogger()
        self.assertEqual(to_qq_voice_wav(bytes(header), logger), bytes(header))
        self.assertEqual(len(logger.warnings), 1)

    def test_unsupported_ratio_and_sample_width_fall_back(self):
        logger = FakeLogger()
        odd = make_wav(47999)
        self.assertIs(to_qq_voice_wav(odd, logger), odd)
        eight_bit = io.BytesIO()
        with wave.open(eight_bit, "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(1)
            writer.setframerate(48000)
            writer.writeframes(b"\x80" * 100)
        raw = eight_bit.getvalue()
        self.assertIs(to_qq_voice_wav(raw, logger), raw)
        self.assertEqual(len(logger.warnings), 2)

    def test_no_logger_is_fine(self):
        self.assertEqual(to_qq_voice_wav(b""), b"")


if __name__ == "__main__":
    unittest.main()
