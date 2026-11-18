"""SAGE Desktop Awareness Subsystem -- Vision & OCR Fallback Adapter.

Provides targeted visual inspection and local Windows OCR for uninstrumented windows
(DirectX, Vulkan, custom canvases, games).
"""

from __future__ import annotations

import json
import logging
import subprocess
from typing import Any, Callable, Dict, List, Optional

from src.desktop.client import DesktopObserverClient
from src.desktop.errors import DesktopProcessError
from src.models.errors import redact_secrets_from_text

logger = logging.getLogger(__name__)

WINDOWS_OCR_POWERSHELL_SCRIPT = """
param([string]$b64)
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTaskGeneric = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' } | Select-Object -First 1
function Await($asyncOp, $type) {
    $genericMethod = $asTaskGeneric.MakeGenericMethod($type)
    $task = $genericMethod.Invoke($null, @($asyncOp))
    $task.Wait()
    return $task.Result
}
$null = [Windows.Globalization.Language, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Storage.Streams.InMemoryRandomAccessStream, Windows.Storage.Streams, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType = WindowsRuntime]

$bytes = [Convert]::FromBase64String($b64)
$stream = New-Object Windows.Storage.Streams.InMemoryRandomAccessStream
$writer = New-Object Windows.Storage.Streams.DataWriter $stream
$writer.WriteBytes($bytes)
$null = Await ($writer.StoreAsync()) ([System.UInt32])
$null = Await ($writer.FlushAsync()) ([System.Boolean])
$stream.Seek(0)

$decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$softwareBmp = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])

$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
if (-not $engine) {
    Write-Output '{"text": "", "words": []}'
    exit
}
$ocrResult = Await ($engine.RecognizeAsync($softwareBmp)) ([Windows.Media.Ocr.OcrResult])

$words = @()
foreach ($line in $ocrResult.Lines) {
    foreach ($w in $line.Words) {
        $words += @{
            text = $w.Text
            x = [int]$w.BoundingRect.X
            y = [int]$w.BoundingRect.Y
            width = [int]$w.BoundingRect.Width
            height = [int]$w.BoundingRect.Height
        }
    }
}
$outObj = @{
    text = $ocrResult.Text
    words = $words
}
Write-Output (ConvertTo-Json -Compress $outObj)
"""


def extract_text_via_windows_ocr(b64_png: str) -> Dict[str, Any]:
    """Extract text and word-level coordinates locally using Windows.Media.Ocr via WinRT."""
    if not b64_png or not b64_png.strip():
        return {"text": "", "words": []}

    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", WINDOWS_OCR_POWERSHELL_SCRIPT, "-b64", b64_png],
            capture_output=True,
            text=True,
            timeout=10.0,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            # Extract last valid JSON line
            for line in reversed(proc.stdout.strip().splitlines()):
                line_str = line.strip()
                if line_str.startswith("{") and line_str.endswith("}"):
                    return json.loads(line_str)
    except Exception as exc:
        logger.warning("Windows OCR invocation failed: %s", exc)

    return {"text": "", "words": []}


class VisionFallbackAdapter:
    """Visual inspection and OCR fallback when UI Automation structure is absent."""

    def __init__(
        self,
        client: DesktopObserverClient,
        ocr_fn: Optional[Callable[[str], Dict[str, Any]]] = None,
    ) -> None:
        self._client = client
        self._ocr_fn = ocr_fn or extract_text_via_windows_ocr
        self._fallback_invocations: int = 0

    @property
    def fallback_invocations(self) -> int:
        return self._fallback_invocations

    def inspect_visual(self, hwnd: int = 0, reason: str = "UIA structure absent") -> Dict[str, Any]:
        """Perform on-demand targeted visual capture and OCR analysis."""
        self._fallback_invocations += 1
        logger.info(
            "Executing visual fallback inspection on HWND=%d (invocation #%d, reason='%s')",
            hwnd,
            self._fallback_invocations,
            reason,
        )

        # 1. Capture targeted bounding frame
        screenshot_data = self._client.screenshot(hwnd=hwnd)
        raw_base64 = screenshot_data.get("screenshot_base64", "")
        bounds = screenshot_data.get("bounds", {})

        # 2. Extract text locally using Windows OCR
        ocr_result = self._ocr_fn(raw_base64) if raw_base64 else {"text": "", "words": []}
        raw_text = str(ocr_result.get("text", ""))
        words: List[Dict[str, Any]] = list(ocr_result.get("words", []))

        # 3. Sanitize extracted text to prevent secret leakage
        sanitized_text = redact_secrets_from_text(raw_text) if raw_text else f"Visual canvas captured ({bounds.get('width', 0)}x{bounds.get('height', 0)}px)"
        for w in words:
            if "text" in w:
                w["text"] = redact_secrets_from_text(str(w["text"]))

        # Invariant: Clearly distinguish OCR/visual fallback from UIA and mark untrusted
        return {
            "source": "visual_fallback_ocr",
            "reason": reason,
            "content": sanitized_text,
            "text": sanitized_text,
            "words": words,
            "bounds": bounds,
            "has_image": bool(raw_base64),
            "untrusted_data": True,
            "confidence_score": 0.85 if raw_text else 0.5,
        }

    def click_text(
        self,
        text_query: str,
        visual_result: Dict[str, Any],
        hwnd: int = 0,
    ) -> Dict[str, Any]:
        """Click an on-screen element located via OCR bounding-box coordinates."""
        words = visual_result.get("words", [])
        target_word: Optional[Dict[str, Any]] = None
        for w in words:
            if text_query.lower() in str(w.get("text", "")).lower():
                target_word = w
                break

        if not target_word:
            raise DesktopProcessError(f"Target text '{text_query}' not located in visual OCR results.")

        bounds = visual_result.get("bounds", {})
        offset_x = bounds.get("x", 0)
        offset_y = bounds.get("y", 0)

        click_x = offset_x + int(target_word.get("x", 0)) + int(target_word.get("width", 0)) // 2
        click_y = offset_y + int(target_word.get("y", 0)) + int(target_word.get("height", 0)) // 2

        logger.info("Clicking OCR target '%s' at screen coordinate (%d, %d)", text_query, click_x, click_y)
        res = self._client.click(x=click_x, y=click_y)
        return {
            "status": "SUCCESS",
            "clicked": True,
            "target_text": text_query,
            "x": click_x,
            "y": click_y,
            "detail": res,
        }
