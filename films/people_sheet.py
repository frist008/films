"""Contact sheet of important-people candidates for the owner (owner-survey «Важные люди»).

Owner, 2026-10-26: «хоть проверяй что присылаешь», «где его фото?» — a chat widget does not load TMDB photos and a page on
127.0.0.1 does not open on his phone, so the photos go as ONE image file sent to the chat (SendUserFile): numbered cards
«N · name · role · seen K». Photos: data/cache/people_photos/<tmdb_id>.jpg (TMDB w185).

    python -X utf8 -m films.people_sheet OUT.jpg "N|tmdb_id|Имя|роль · видели K" ...
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from films import db

PHOTOS = db.ROOT / "data" / "cache" / "people_photos"
FONT = Path("C:/Windows/Fonts/arial.ttf")
FONT_B = Path("C:/Windows/Fonts/arialbd.ttf")
W, H, PAD, TEXT_H, COLS = 185, 278, 16, 74, 4


def build(out: Path, cards: list[tuple[str, int, str, str]]) -> Path:
    rows = (len(cards) + COLS - 1) // COLS
    sheet = Image.new("RGB", (COLS * (W + PAD) + PAD, rows * (H + TEXT_H + PAD) + PAD), "white")
    d = ImageDraw.Draw(sheet)
    f_b = ImageFont.truetype(str(FONT_B), 20)
    f = ImageFont.truetype(str(FONT), 15)
    for i, (num, tmdb_id, name, sub) in enumerate(cards):
        x = PAD + (i % COLS) * (W + PAD)
        y = PAD + (i // COLS) * (H + TEXT_H + PAD)
        photo = Image.open(PHOTOS / f"{tmdb_id}.jpg").convert("RGB").resize((W, H))
        sheet.paste(photo, (x, y))
        d.rectangle((x, y, x + 44, y + 34), fill="black")
        d.text((x + 8, y + 5), num, font=f_b, fill="white")
        d.text((x, y + H + 6), name, font=f_b, fill="black")
        d.text((x, y + H + 34), sub, font=f, fill=(90, 90, 90))
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out, quality=90)
    return out


if __name__ == "__main__":
    out = Path(sys.argv[1])
    cards = []
    for a in sys.argv[2:]:
        num, tid, name, sub = a.split("|")
        cards.append((num, int(tid), name, sub))
    print(build(out, cards))
