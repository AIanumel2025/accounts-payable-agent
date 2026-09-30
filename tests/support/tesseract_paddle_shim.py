"""Test-only engine that speaks PaddleOCR's `predict()` contract but is backed
by real Tesseract word boxes.

Used by the M11D automatic-completion acceptance test only, where the
sandbox has no PaddleOCR. It is an honest stand-in, not PaddleOCR: the test
names it as such and the report never calls this a PaddleOCR run. Real
PaddleOCR runs in the repository's OCR-capable CI workflow.

Two real Tesseract passes are merged (plain, and a dark-pixel pass that
recovers dark text on a coloured band, e.g. a total printed on a brown bar);
words are regrouped into visual lines and split into column segments.
"""

from __future__ import annotations

from typing import Any


class _Result:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.json = {"res": payload}
        self.img = {}


def _words(image, config: str) -> list[dict[str, Any]]:
    import pytesseract

    data = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT, config=config)

    return [
        {
            "text": data["text"][i].strip(),
            "left": data["left"][i],
            "top": data["top"][i],
            "right": data["left"][i] + data["width"][i],
            "bottom": data["top"][i] + data["height"][i],
            "conf": max(float(data["conf"][i]), 0.0),
        }
        for i in range(len(data["text"]))
        if data["text"][i].strip()
    ]


def _overlaps(word: dict[str, Any], others: list[dict[str, Any]]) -> bool:
    return any(
        word["left"] < other["right"] and word["right"] > other["left"]
        and word["top"] < other["bottom"] and word["bottom"] > other["top"]
        for other in others
    )


class TesseractBackedPaddleShim:
    def __init__(self, *, column_gap_pixels: int = 60, dark_threshold: int = 35) -> None:
        self._gap = column_gap_pixels
        self._dark_threshold = dark_threshold
        self.calls: list[str] = []

    def predict(self, path: str):
        from PIL import Image

        self.calls.append(path)

        with Image.open(path) as image:
            grey = image.convert("L")
            words = _words(grey, "--oem 3 --psm 6")
            dark = grey.point(lambda pixel, limit=self._dark_threshold: 0 if pixel < limit else 255)
            words += [word for word in _words(dark, "--oem 3 --psm 6") if not _overlaps(word, words)]

        words.sort(key=lambda word: ((word["top"] + word["bottom"]) / 2, word["left"]))

        lines: list[list[dict[str, Any]]] = []

        for word in words:
            centre = (word["top"] + word["bottom"]) / 2
            height = word["bottom"] - word["top"]

            for line in lines:
                line_centre = sum((w["top"] + w["bottom"]) / 2 for w in line) / len(line)

                if abs(centre - line_centre) <= 0.6 * max(height, 1):
                    line.append(word)
                    break
            else:
                lines.append([word])

        texts: list[str] = []
        scores: list[float] = []
        boxes: list[list[int]] = []

        for line in lines:
            line.sort(key=lambda word: word["left"])
            segment: list[dict[str, Any]] = []

            def flush() -> None:
                if not segment:
                    return
                texts.append(" ".join(word["text"] for word in segment))
                scores.append(max(0.0, min(1.0, sum(word["conf"] for word in segment) / len(segment) / 100)))
                boxes.append([
                    min(word["left"] for word in segment), min(word["top"] for word in segment),
                    max(word["right"] for word in segment), max(word["bottom"] for word in segment),
                ])
                segment.clear()

            for word in line:
                if segment and word["left"] - segment[-1]["right"] > self._gap:
                    flush()
                segment.append(word)

            flush()

        return [_Result({"rec_texts": texts, "rec_scores": scores, "rec_boxes": boxes})]
