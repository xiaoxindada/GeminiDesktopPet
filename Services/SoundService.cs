using System.IO;
using System.Media;

namespace GeminiDesktopPet.Services;

public enum PetSoundEffect
{
    Chime,
    Purr,
    Sparkle,
    Boing,
    Angry,
    Victory
}

/// <summary>
/// Generates lightweight pastel PCM WAV sound effects in memory without external audio files.
/// </summary>
public sealed class SoundService
{
    private readonly Dictionary<PetSoundEffect, byte[]> _wavCache = new();
    private DateTime _lastPlayUtc = DateTime.MinValue;

    public bool Enabled { get; set; } = true;

    public SoundService()
    {
        _wavCache[PetSoundEffect.Chime] = SynthesizeNotes([(784, 0.06, 0.16), (1046.5, 0.09, 0.14)]);
        _wavCache[PetSoundEffect.Purr] = SynthesizePurr();
        _wavCache[PetSoundEffect.Sparkle] = SynthesizeNotes([(880, 0.045, 0.12), (1174.6, 0.05, 0.12), (1567.9, 0.09, 0.13)]);
        _wavCache[PetSoundEffect.Boing] = SynthesizeSweep(340, 620, 0.11, 0.16);
        _wavCache[PetSoundEffect.Angry] = SynthesizeSweep(480, 220, 0.12, 0.18);
        _wavCache[PetSoundEffect.Victory] = SynthesizeNotes([(659.25, 0.06, 0.14), (783.99, 0.06, 0.14), (987.77, 0.06, 0.14), (1318.5, 0.14, 0.16)]);
    }

    public void Play(PetSoundEffect effect)
    {
        if (!Enabled) return;

        var now = DateTime.UtcNow;
        if ((now - _lastPlayUtc).TotalMilliseconds < 140)
            return;
        _lastPlayUtc = now;

        if (!_wavCache.TryGetValue(effect, out var bytes))
            return;

        Task.Run(() =>
        {
            try
            {
                using var ms = new MemoryStream(bytes, writable: false);
                using var player = new SoundPlayer(ms);
                player.Play();
            }
            catch
            {
                // Audio device may be unavailable.
            }
        });
    }

    private static byte[] SynthesizeNotes((double freq, double dur, double amp)[] notes)
    {
        const int sampleRate = 22050;
        var samples = new List<short>();

        foreach (var (freq, dur, amp) in notes)
        {
            int count = (int)(sampleRate * dur);
            for (int i = 0; i < count; i++)
            {
                double t = i / (double)sampleRate;
                double env = Math.Sin(Math.PI * i / count);
                double wave = Math.Sin(2 * Math.PI * freq * t) + 0.25 * Math.Sin(4 * Math.PI * freq * t);
                samples.Add((short)(Math.Clamp(wave * env * amp, -1.0, 1.0) * short.MaxValue));
            }
        }

        return EncodeWav(samples, sampleRate);
    }

    private static byte[] SynthesizePurr()
    {
        const int sampleRate = 22050;
        const double dur = 0.22;
        int count = (int)(sampleRate * dur);
        var samples = new List<short>(count);

        for (int i = 0; i < count; i++)
        {
            double t = i / (double)sampleRate;
            double env = Math.Sin(Math.PI * i / count);
            double mod = 0.55 + 0.45 * Math.Sin(2 * Math.PI * 26 * t);
            double tone = Math.Sin(2 * Math.PI * 185 * t) * 0.6 + Math.Sin(2 * Math.PI * 370 * t) * 0.3;
            samples.Add((short)(Math.Clamp(tone * mod * env * 0.16, -1.0, 1.0) * short.MaxValue));
        }

        return EncodeWav(samples, sampleRate);
    }

    private static byte[] SynthesizeSweep(double startFreq, double endFreq, double dur, double amp)
    {
        const int sampleRate = 22050;
        int count = (int)(sampleRate * dur);
        var samples = new List<short>(count);
        double phase = 0;

        for (int i = 0; i < count; i++)
        {
            double progress = i / (double)count;
            double freq = startFreq + (endFreq - startFreq) * progress;
            phase += 2 * Math.PI * freq / sampleRate;
            double env = Math.Sin(Math.PI * progress);
            double wave = Math.Sin(phase);
            samples.Add((short)(Math.Clamp(wave * env * amp, -1.0, 1.0) * short.MaxValue));
        }

        return EncodeWav(samples, sampleRate);
    }

    private static byte[] EncodeWav(List<short> samples, int sampleRate)
    {
        using var ms = new MemoryStream();
        using var bw = new BinaryWriter(ms);

        int dataBytes = samples.Count * 2;
        bw.Write("RIFF"u8);
        bw.Write(36 + dataBytes);
        bw.Write("WAVE"u8);
        bw.Write("fmt "u8);
        bw.Write(16);
        bw.Write((short)1); // PCM
        bw.Write((short)1); // Mono
        bw.Write(sampleRate);
        bw.Write(sampleRate * 2);
        bw.Write((short)2);
        bw.Write((short)16);
        bw.Write("data"u8);
        bw.Write(dataBytes);
        foreach (short s in samples)
            bw.Write(s);

        return ms.ToArray();
    }
}
