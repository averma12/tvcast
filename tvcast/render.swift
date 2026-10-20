// Draws subtitle cues into a Blu-ray PGS (.sup) file, for burning into video with ffmpeg.
// Usage: render CUES.tsv OUT.sup VIDEO_WIDTH VIDEO_HEIGHT BRIGHTNESS
// CUES.tsv has one cue per line: start seconds, end seconds, text with "\n" for line breaks.
// BRIGHTNESS scales the text gray: 1 is white, lower keeps text comfortable in HDR (PQ) video.
// burn.py compiles this with the Command Line Tools' swiftc; it uses only CoreText and CoreGraphics.
import CoreGraphics
import CoreText
import Foundation

let args = CommandLine.arguments
guard args.count == 6, let videoW = Int(args[3]), let videoH = Int(args[4]), let brightness = Double(args[5]) else {
    FileHandle.standardError.write("usage: render CUES.tsv OUT.sup WIDTH HEIGHT BRIGHTNESS\n".data(using: .utf8)!)
    exit(2)
}
let cues = try String(contentsOfFile: args[1], encoding: .utf8)

// Text scales with the picture width: 84 px suits a 3840-wide frame.
let fontSize = CGFloat(videoW) * 84 / 3840
let bandH = Int(fontSize * 5)  // room for three lines plus outline
let bottomMargin = Int(fontSize * 0.7)
let font = CTFontCreateWithName("HelveticaNeue-Medium" as CFString, fontSize, nil)

// Palette: 0 is transparent; 1 + lum * 8 + (alpha - 1) for lum 0...3 (black to text gray) and
// alpha 1...8 eighths, which keeps the anti-aliased edges of white text with a black outline.
var palette = Data([0, 16, 128, 128, 0])
for lum in 0..<4 {
    let gray = Double(lum) / 3 * 255 * brightness
    let y = UInt8((16 + gray * 219 / 255).rounded())
    for a in 1...8 {
        palette.append(contentsOf: [UInt8(palette.count / 5), y, 128, 128, UInt8(min(255, (Double(a) * 255 / 8).rounded()))])
    }
}

var sup = Data()
func be16(_ v: Int) -> [UInt8] { [UInt8(v >> 8 & 0xFF), UInt8(v & 0xFF)] }
func be32(_ v: Int) -> [UInt8] { [UInt8(v >> 24 & 0xFF), UInt8(v >> 16 & 0xFF), UInt8(v >> 8 & 0xFF), UInt8(v & 0xFF)] }
func segment(_ kind: UInt8, _ pts: Int, _ payload: [UInt8]) {
    var head: [UInt8] = [0x50, 0x47]
    head += be32(pts)
    head += be32(0)
    head.append(kind)
    head += be16(payload.count)
    sup.append(contentsOf: head)
    sup.append(contentsOf: payload)
}
func window(_ x: Int, _ y: Int, _ w: Int, _ h: Int) -> [UInt8] {
    var p: [UInt8] = [1, 0]
    p += be16(x); p += be16(y); p += be16(w); p += be16(h)
    return p
}
// Presentation composition: the frame size, then state 0x80 (epoch start, one object at x, y)
// or 0x00 with no objects, which clears the screen.
var composition = 0
func pcs(_ object: (Int, Int)?) -> [UInt8] {
    var p = be16(videoW)
    p += be16(videoH)
    p.append(0x10)
    p += be16(composition)
    p += [object == nil ? 0x00 : 0x80, 0, 0, object == nil ? 0 : 1]
    if let (x, y) = object {
        p += be16(0); p += [0, 0]; p += be16(x); p += be16(y)
    }
    return p
}
func clear(_ pts: Int, _ win: [UInt8]) {
    segment(0x16, pts, pcs(nil))
    segment(0x17, pts, win)
    segment(0x80, pts, [])
    composition += 1
}

// Run-length encoding as PGS defines it, row by row.
func rle(_ px: [UInt8], _ w: Int, _ h: Int) -> [UInt8] {
    var out = [UInt8]()
    out.reserveCapacity(px.count / 4)
    for y in 0..<h {
        var x = 0
        while x < w {
            let c = px[y * w + x]
            var n = 1
            while x + n < w && px[y * w + x + n] == c && n < 16383 { n += 1 }
            if c != 0 && n <= 2 {
                out.append(contentsOf: [UInt8](repeating: c, count: n))
            } else if c == 0 {
                out.append(contentsOf: n < 64 ? [0, UInt8(n)] : [0, UInt8(0x40 | n >> 8), UInt8(n & 0xFF)])
            } else {
                out.append(contentsOf: n < 64 ? [0, UInt8(0x80 | n), c] : [0, UInt8(0xC0 | n >> 8), UInt8(n & 0xFF), c])
            }
            x += n
        }
        out.append(contentsOf: [0, 0])
    }
    return out
}

// An empty display set at 0:00 pins the stream's start, so ffmpeg doesn't shift every cue back
// to begin at the first one when it opens this file next to the video.
clear(0, window(0, 0, videoW, videoH))
var drawn = 0
for row in cues.split(separator: "\n") {
    let f = row.split(separator: "\t", maxSplits: 2, omittingEmptySubsequences: false)
    guard f.count == 3, let start = Double(f[0]), let end = Double(f[1]), end > start else { continue }
    let lines = f[2].replacingOccurrences(of: "\\n", with: "\n").split(separator: "\n").map(String.init)
    let ctx = CGContext(data: nil, width: videoW, height: bandH, bitsPerComponent: 8, bytesPerRow: videoW * 4,
                        space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
    ctx.setLineJoin(.round)
    for (i, text) in lines.reversed().enumerated() {  // bottom line first
        let fill = CTLineCreateWithAttributedString(CFAttributedStringCreate(nil, text as CFString, [
            kCTFontAttributeName: font, kCTForegroundColorAttributeName: CGColor(gray: 1, alpha: 1)] as CFDictionary)!)
        let stroke = CTLineCreateWithAttributedString(CFAttributedStringCreate(nil, text as CFString, [
            kCTFontAttributeName: font, kCTStrokeWidthAttributeName: 14.0,
            kCTStrokeColorAttributeName: CGColor(gray: 0, alpha: 1)] as CFDictionary)!)
        let width = CGFloat(CTLineGetTypographicBounds(fill, nil, nil, nil))
        let pos = CGPoint(x: (CGFloat(videoW) - width) / 2, y: fontSize * 0.36 + CGFloat(i) * fontSize * 1.22)
        ctx.textPosition = pos
        CTLineDraw(stroke, ctx)
        ctx.textPosition = pos
        CTLineDraw(fill, ctx)
    }
    // Quantize to the palette and crop to the drawn pixels (rows are stored top first).
    let rgba = ctx.data!.bindMemory(to: UInt8.self, capacity: videoW * bandH * 4)
    var idx = [UInt8](repeating: 0, count: videoW * bandH)
    var minX = videoW, minY = bandH, maxX = -1, maxY = -1
    for y in 0..<bandH {
        for x in 0..<videoW {
            let p = (y * videoW + x) * 4
            let a = Int(rgba[p + 3])
            if a < 24 { continue }
            let lum = min(3, min(255, Int(rgba[p]) * 255 / a) * 4 / 256)
            idx[y * videoW + x] = UInt8(1 + lum * 8 + max(1, min(8, (a * 8 + 128) / 256)) - 1)
            minX = min(minX, x); maxX = max(maxX, x); minY = min(minY, y); maxY = max(maxY, y)
        }
    }
    if maxX < 0 { continue }
    let w = maxX - minX + 1, h = maxY - minY + 1
    var crop = [UInt8](repeating: 0, count: w * h)
    for y in 0..<h { for x in 0..<w { crop[y * w + x] = idx[(minY + y) * videoW + minX + x] } }
    let x0 = minX, y0 = videoH - bottomMargin - bandH + minY
    let pts = Int((start * 90000).rounded()), endPts = Int((end * 90000).rounded())
    let win = window(x0, y0, w, h)

    segment(0x16, pts, pcs((x0, y0)))
    segment(0x17, pts, win)
    var pds: [UInt8] = [0, 0]
    pds += [UInt8](palette)
    segment(0x14, pts, pds)
    // Object data longer than one segment continues in further ODS segments.
    var obj = be16(w)
    obj += be16(h)
    obj += rle(crop, w, h)
    var at = 0, first = true
    while at < obj.count {
        let room = first ? 65535 - 7 : 65535 - 4
        let chunk = Array(obj[at..<min(obj.count, at + room)])
        let last = at + chunk.count >= obj.count
        var ods = be16(0)
        ods += [0, UInt8((first ? 0x80 : 0) | (last ? 0x40 : 0))]
        if first { ods += Array(be32(obj.count)[1...]) }
        ods += chunk
        segment(0x15, pts, ods)
        at += chunk.count
        first = false
    }
    segment(0x80, pts, [])
    composition += 1
    clear(endPts, win)
    drawn += 1
}
try sup.write(to: URL(fileURLWithPath: args[2]))
print("drew \(drawn) cues")
