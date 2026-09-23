// Add this file to Microsoft's LocalMediaSamples bot project. The bot must call
// updateRecordingStatus(recording) successfully before attaching this forwarder.
using System;
using System.Net.Http;
using System.Net.Http.Headers;
using System.Runtime.InteropServices;
using System.Threading;
using System.Threading.Channels;
using System.Threading.Tasks;
using Microsoft.Skype.Bots.Media;

namespace HackAlem.TeamsBridge;

public sealed class TeamsAudioForwarder : IAsyncDisposable
{
    private sealed record Frame(string Speaker, long Timestamp, byte[] Pcm);

    private readonly IAudioSocket _socket;
    private readonly HttpClient _http;
    private readonly Channel<Frame> _frames;
    private readonly Task _sender;
    private readonly string _sessionId;
    private readonly string _token;
    private Exception? _sendError;
    private int _droppedFrames;

    public TeamsAudioForwarder(IAudioSocket socket, HttpClient http, string sessionId, string token, bool recordingStatusConfirmed)
    {
        if (!recordingStatusConfirmed)
            throw new InvalidOperationException("Confirm Teams recording status before processing media.");
        _socket = socket;
        _http = http;
        _sessionId = sessionId;
        _token = token;
        _frames = Channel.CreateBounded<Frame>(new BoundedChannelOptions(10000)
        {
            FullMode = BoundedChannelFullMode.Wait,
            SingleReader = true,
            SingleWriter = true
        });
        _socket.AudioMediaReceived += OnAudio;
        _sender = SendFramesAsync();
    }

    private void OnAudio(object? sender, AudioMediaReceivedEventArgs args)
    {
        var buffer = args.Buffer;
        try
        {
            if (buffer.IsSilence) return;
            if (buffer.UnmixedAudioBuffers is { Length: > 0 } unmixed)
            {
                foreach (var item in unmixed)
                    Enqueue(item.ActiveSpeakerId.ToString(), buffer.Timestamp, item.Data, item.Length);
            }
            else
            {
                // Mixed fallback preserves speech, but speaker attribution is unavailable.
                Enqueue("mixed", buffer.Timestamp, buffer.Data, buffer.Length);
            }
        }
        finally
        {
            buffer.Dispose();
        }
    }

    private void Enqueue(string speaker, long timestamp, IntPtr data, long length)
    {
        if (data == IntPtr.Zero || length != 640) return; // PCM 16 kHz, 20 ms.
        var bytes = new byte[640];
        Marshal.Copy(data, bytes, 0, bytes.Length);
        if (!_frames.Writer.TryWrite(new Frame(speaker, timestamp, bytes)))
            Interlocked.Increment(ref _droppedFrames);
    }

    private async Task SendFramesAsync()
    {
        try
        {
            await foreach (var frame in _frames.Reader.ReadAllAsync())
            {
                using var request = new HttpRequestMessage(HttpMethod.Post,
                    $"sessions/{Uri.EscapeDataString(_sessionId)}/audio/{Uri.EscapeDataString(frame.Speaker)}");
                request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", _token);
                request.Headers.Add("X-Timestamp", frame.Timestamp.ToString());
                request.Content = new ByteArrayContent(frame.Pcm);
                using var response = await _http.SendAsync(request);
                response.EnsureSuccessStatusCode();
            }
        }
        catch (Exception error)
        {
            _sendError = error;
        }
    }

    public async ValueTask DisposeAsync()
    {
        _socket.AudioMediaReceived -= OnAudio;
        _frames.Writer.TryComplete();
        await _sender;
        if (_sendError is not null) throw new InvalidOperationException("Audio forwarding failed", _sendError);
        if (_droppedFrames > 0) throw new InvalidOperationException($"Lost {_droppedFrames} audio frames");
        using var request = new HttpRequestMessage(HttpMethod.Post,
            $"sessions/{Uri.EscapeDataString(_sessionId)}/finish");
        request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", _token);
        using var response = await _http.SendAsync(request);
        response.EnsureSuccessStatusCode();
    }
}
