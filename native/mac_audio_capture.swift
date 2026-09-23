import AVFoundation
import CoreMedia
import Foundation
import ScreenCaptureKit

private let sampleRate: Double = 16_000

final class AudioSink: NSObject, SCStreamOutput, SCStreamDelegate {
    private let file: FileHandle
    private let target: AVAudioFormat
    private let lock = NSLock()
    private var bytesWritten: UInt32 = 0
    private var converter: AVAudioConverter?
    private var failure: Error?
    private var sourcePeak: Float = 0
    private var reportedFormat = false

    var streamError: Error? {
        lock.lock()
        defer { lock.unlock() }
        return failure
    }

    init(path: String) throws {
        guard let format = AVAudioFormat(commonFormat: .pcmFormatInt16,
                                         sampleRate: sampleRate,
                                         channels: 1,
                                         interleaved: true) else {
            throw NSError(domain: "MeetCapture", code: 1,
                          userInfo: [NSLocalizedDescriptionKey: "Cannot create target audio format"])
        }
        target = format
        _ = FileManager.default.createFile(atPath: path, contents: nil)
        file = try FileHandle(forWritingTo: URL(fileURLWithPath: path))
        super.init()
        file.write(wavHeader(byteCount: 0))
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer,
                of outputType: SCStreamOutputType) {
        guard outputType == .audio, sampleBuffer.isValid,
              let description = sampleBuffer.formatDescription else { return }
        let source = AVAudioFormat(cmAudioFormatDescription: description)
        if !reportedFormat {
            fputs("Source audio: \(source)\n", stderr)
            reportedFormat = true
        }
        do {
            try sampleBuffer.withAudioBufferList { audioBufferList, _ in
                guard let input = AVAudioPCMBuffer(pcmFormat: source,
                                                   bufferListNoCopy: audioBufferList.unsafePointer)
                else { return }
                input.frameLength = AVAudioFrameCount(CMSampleBufferGetNumSamples(sampleBuffer))
                if let channels = input.floatChannelData {
                    for index in 0..<Int(input.frameLength) {
                        sourcePeak = max(sourcePeak, abs(channels[0][index]))
                    }
                }
                if converter == nil || converter?.inputFormat != source {
                    converter = AVAudioConverter(from: source, to: target)
                }
                guard let converter else { return }
                let capacity = AVAudioFrameCount(
                    ceil(Double(input.frameLength) * target.sampleRate / source.sampleRate) + 64)
                guard let output = AVAudioPCMBuffer(pcmFormat: target, frameCapacity: capacity) else { return }
                var provided = false
                var error: NSError?
                _ = converter.convert(to: output, error: &error) { _, status in
                    if provided {
                        status.pointee = .noDataNow
                        return nil
                    }
                    provided = true
                    status.pointee = .haveData
                    return input
                }
                if let error { fputs("Audio conversion error: \(error)\n", stderr); return }
                let buffer = output.audioBufferList.pointee.mBuffers
                guard let pointer = buffer.mData, buffer.mDataByteSize > 0 else { return }
                let length = Int(buffer.mDataByteSize)
                lock.lock()
                defer { lock.unlock() }
                file.write(Data(bytes: pointer, count: length))
                bytesWritten &+= UInt32(length)
            }
        } catch {
            fputs("Audio capture error: \(error)\n", stderr)
        }
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        lock.lock()
        failure = error
        lock.unlock()
        fputs("ScreenCaptureKit stopped: \(error)\n", stderr)
    }

    func finish() throws {
        lock.lock()
        defer { lock.unlock() }
        file.seek(toFileOffset: 0)
        file.write(wavHeader(byteCount: bytesWritten))
        file.synchronizeFile()
        try file.close()
        fputs("Audio saved: \(bytesWritten) bytes; source peak \(sourcePeak)\n", stderr)
        if bytesWritten == 0 {
            throw NSError(domain: "MeetCapture", code: 2,
                          userInfo: [NSLocalizedDescriptionKey: "No meeting audio was captured"])
        }
    }
}

private func wavHeader(byteCount: UInt32) -> Data {
    var bytes = Data("RIFF".utf8)
    func append16(_ value: UInt16) {
        var little = value.littleEndian
        withUnsafeBytes(of: &little) { bytes.append(contentsOf: $0) }
    }
    func append32(_ value: UInt32) {
        var little = value.littleEndian
        withUnsafeBytes(of: &little) { bytes.append(contentsOf: $0) }
    }
    append32(36 &+ byteCount)
    bytes.append(Data("WAVEfmt ".utf8))
    append32(16)
    append16(1)
    append16(1)
    append32(UInt32(sampleRate))
    append32(UInt32(sampleRate) * 2)
    append16(2)
    append16(16)
    bytes.append(Data("data".utf8))
    append32(byteCount)
    return bytes
}

@main
struct Main {
    static func main() async {
        guard (2...3).contains(CommandLine.arguments.count) else {
            fputs("Usage: mac_audio_capture OUTPUT.wav [CHROME_PID]\n", stderr)
            exit(2)
        }
        do {
            let content = try await SCShareableContent.excludingDesktopWindows(
                false, onScreenWindowsOnly: false)
            if CommandLine.arguments[1] == "--list-apps" {
                for app in content.applications where app.bundleIdentifier.contains("Chrome") {
                    print("\(app.processID) \(app.bundleIdentifier) \(app.applicationName)")
                }
                print("Displays: \(content.displays.count); applications: \(content.applications.count)")
                return
            }
            let requestedPID = CommandLine.arguments.count == 3 ? Int32(CommandLine.arguments[2]) : nil
            let applications = content.applications.filter {
                $0.bundleIdentifier == "com.google.Chrome" &&
                (requestedPID == nil || $0.processID == requestedPID)
            }
            guard let display = content.displays.first, applications.count == 1,
                  let chrome = applications.first
            else {
                throw NSError(domain: "MeetCapture", code: 3,
                              userInfo: [NSLocalizedDescriptionKey: "Cannot select Chrome for capture; provide the bot Chrome PID if multiple instances are running"])
            }
            // On macOS 15 Chrome renders audio in helper processes. Including only
            // the browser PID yields silent buffers; exclude the other applications
            // instead. Refresh the exclusions while recording as apps open/close.
            let filter = SCContentFilter(display: display,
                excludingApplications: content.applications.filter { $0.processID != chrome.processID },
                exceptingWindows: [])
            let configuration = SCStreamConfiguration()
            configuration.capturesAudio = true
            configuration.captureMicrophone = false
            // The helper produces no audio. Chrome and this helper may share the
            // same responsible macOS application when launched from Terminal.
            configuration.excludesCurrentProcessAudio = false
            // Capture at the system mixer format, then downsample in AudioSink.
            configuration.sampleRate = 48_000
            configuration.channelCount = 2
            configuration.width = 2
            configuration.height = 2
            configuration.minimumFrameInterval = CMTime(value: 1, timescale: 1)
            let sink = try AudioSink(path: CommandLine.arguments[1])
            let stream = SCStream(filter: filter, configuration: configuration, delegate: sink)
            let audioQueue = DispatchQueue(label: "meet.audio.capture")
            try stream.addStreamOutput(sink, type: .audio, sampleHandlerQueue: audioQueue)
            signal(SIGINT, SIG_IGN)
            signal(SIGTERM, SIG_IGN)
            let stop = DispatchSource.makeSignalSource(signal: SIGINT, queue: .main)
            let terminate = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
            var shouldStop = false
            stop.setEventHandler { shouldStop = true }
            terminate.setEventHandler { shouldStop = true }
            stop.resume()
            terminate.resume()
            try await stream.startCapture()
            // Parent waits for this marker before announcing a working recorder.
            try Data("ready".utf8).write(to: URL(fileURLWithPath: CommandLine.arguments[1] + ".ready"))
            fputs("Capturing Chrome meeting audio. Press Ctrl-C after the meeting.\n", stderr)
            var ticks = 0
            var filterError: Error?
            while !shouldStop && sink.streamError == nil {
                try? await Task.sleep(for: .milliseconds(250))
                ticks += 1
                if ticks % 20 == 0 {
                    do {
                        let current = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
                        try await stream.updateContentFilter(SCContentFilter(display: display,
                            excludingApplications: current.applications.filter { $0.processID != chrome.processID },
                            exceptingWindows: []))
                    } catch {
                        filterError = error
                        shouldStop = true
                    }
                }
            }
            if sink.streamError == nil {
                do { try await stream.stopCapture() }
                catch { filterError = error }
            }
            audioQueue.sync {}
            try sink.finish()
            if let error = sink.streamError { throw error }
            if let error = filterError { throw error }
        } catch {
            fputs("Capture failed: \(error). Check macOS Screen Recording permission for Terminal.\n", stderr)
            exit(1)
        }
    }
}
