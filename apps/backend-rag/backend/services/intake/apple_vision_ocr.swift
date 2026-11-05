// apple_vision_ocr.swift -- on-device OCR for the document-intake worker (macOS only).
//
// Protocol, consumed by apple_vision_ocr.py (which compiles and runs this file):
//   stdin   the raw bytes of ONE image (PNG, JPEG, HEIC, TIFF, ...), nothing else
//   stdout  exactly ONE JSON object followed by a newline, nothing else:
//             {"ok":true,"width":W,"height":H,"observations":[
//                {"text":..,"confidence":0..1,"x":..,"y":..,"w":..,"h":..}, ...]}
//               x/y/w/h = the text box in [0,1] of the UPRIGHT image, origin TOP-LEFT
//             {"ok":false,"error":"<short class>"}   and exit status 2
//   stderr  free-form diagnostics; never the recognised text
//
// The recognised text is client PII (passports, KITAS, bank statements). It leaves
// this process through stdout only: no file, no log, no network.

import Foundation
import ImageIO
import Vision

struct Observation: Encodable {
    let text: String
    let confidence: Double
    let x: Double
    let y: Double
    let w: Double
    let h: Double
}

struct Success: Encodable {
    let ok: Bool
    let width: Int
    let height: Int
    let observations: [Observation]
}

struct Failure: Encodable {
    let ok: Bool
    let error: String
}

/// Request languages, most specific first. A language this OS cannot recognise is
/// dropped instead of failing the whole request.
let requestedLanguages = ["id-ID", "en-US"]

func clean(_ value: Double) -> Double {
    value.isFinite ? (value * 1_000_000).rounded() / 1_000_000 : 0
}

/// Write every byte to a descriptor, tolerating short writes and EINTR.
func writeAll(_ descriptor: Int32, _ bytes: Data) {
    bytes.withUnsafeBytes { (raw: UnsafeRawBufferPointer) in
        guard let base = raw.baseAddress else { return }
        var offset = 0
        while offset < raw.count {
            let written = write(descriptor, base + offset, raw.count - offset)
            if written < 0 {
                if errno == EINTR { continue }
                return
            }
            offset += written
        }
    }
}

func emit<T: Encodable>(_ value: T, to descriptor: Int32) -> Bool {
    let encoder = JSONEncoder()
    encoder.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
    guard var data = try? encoder.encode(value) else { return false }
    data.append(0x0A)
    writeAll(descriptor, data)
    return true
}

func exifOrientation(of source: CGImageSource) -> CGImagePropertyOrientation {
    guard let properties = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any],
        let raw = properties[kCGImagePropertyOrientation] as? UInt32,
        let orientation = CGImagePropertyOrientation(rawValue: raw)
    else { return .up }
    return orientation
}

func run() -> Int32 {
    // Keep the real stdout for the single JSON document; whatever a framework prints
    // by accident while recognising goes to stderr instead.
    let savedStdout = dup(STDOUT_FILENO)
    if savedStdout >= 0 { _ = dup2(STDERR_FILENO, STDOUT_FILENO) }
    let result = savedStdout >= 0 ? savedStdout : STDOUT_FILENO

    func fail(_ errorClass: String) -> Int32 {
        _ = emit(Failure(ok: false, error: errorClass), to: result)
        return 2
    }

    let input = FileHandle.standardInput.readDataToEndOfFile()
    if input.isEmpty { return fail("empty_input") }

    guard let source = CGImageSourceCreateWithData(input as CFData, nil),
        CGImageSourceGetCount(source) > 0,
        let image = CGImageSourceCreateImageAtIndex(source, 0, nil)
    else { return fail("decode_failed") }

    let orientation = exifOrientation(of: source)
    let sideways: Set<CGImagePropertyOrientation> = [.left, .leftMirrored, .right, .rightMirrored]
    let width = sideways.contains(orientation) ? image.height : image.width
    let height = sideways.contains(orientation) ? image.width : image.height

    let request = VNRecognizeTextRequest()
    request.revision = VNRecognizeTextRequestRevision3
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = false  // identifiers must never be "corrected"
    request.automaticallyDetectsLanguage = false
    let supported = (try? request.supportedRecognitionLanguages()) ?? []
    let languages = requestedLanguages.filter { supported.contains($0) }
    if !languages.isEmpty { request.recognitionLanguages = languages }

    let handler = VNImageRequestHandler(cgImage: image, orientation: orientation, options: [:])
    do {
        try handler.perform([request])
    } catch {
        return fail("vision_failed")
    }

    var observations: [Observation] = []
    for observation in request.results ?? [] {
        guard let candidate = observation.topCandidates(1).first else { continue }
        let box = observation.boundingBox  // normalised, origin bottom-left
        observations.append(
            Observation(
                text: candidate.string,
                confidence: clean(Double(candidate.confidence)),
                x: clean(Double(box.minX)),
                y: clean(1.0 - Double(box.maxY)),
                w: clean(Double(box.width)),
                h: clean(Double(box.height))))
    }

    let success = Success(ok: true, width: width, height: height, observations: observations)
    return emit(success, to: result) ? 0 : fail("encode_failed")
}

exit(run())
