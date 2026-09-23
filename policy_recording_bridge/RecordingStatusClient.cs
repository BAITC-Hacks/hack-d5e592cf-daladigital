using System;
using System.Net.Http;
using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;
using System.Threading.Tasks;

namespace HackAlem.TeamsBridge;

public static class RecordingStatusClient
{
    public static async Task SetAsync(HttpClient graphClient, string callId, string accessToken, bool recording)
    {
        var endpoint = "https://graph.microsoft.com/v1.0/communications/calls/"
                       + Uri.EscapeDataString(callId) + "/updateRecordingStatus";
        using var request = new HttpRequestMessage(HttpMethod.Post, endpoint);
        request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", accessToken);
        var body = JsonSerializer.Serialize(new
        {
            clientContext = Guid.NewGuid().ToString(),
            status = recording ? "recording" : "notRecording"
        });
        request.Content = new StringContent(body, Encoding.UTF8, "application/json");
        using var response = await graphClient.SendAsync(request);
        response.EnsureSuccessStatusCode();
    }
}
