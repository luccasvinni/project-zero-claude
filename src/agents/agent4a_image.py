import asyncio
import base64
import io
import json
import os
from pathlib import Path

import re

import anthropic
from google import genai
from google.genai import types
from PIL import Image
from dotenv import load_dotenv

load_dotenv()

OUTPUT_SIZE = (1080, 1620)
PAGES_RANGE = [7, 8, 9, 10]


def encode_image(img_path: Path) -> str:
    return base64.standard_b64encode(img_path.read_bytes()).decode("utf-8")


def locate_announcements(client: anthropic.Anthropic, pages_dir: Path, announcements: list[dict]) -> dict:
    """Usa Claude para identificar a região de cada anúncio nas páginas."""
    content = []
    content.append({
        "type": "text",
        "text": (
            "The following images are pages 7, 8, 9, and 10 of a Catholic parish bulletin. "
            "For each announcement listed below, identify:\n"
            "1. Which page it appears on (7, 8, 9, or 10)\n"
            "2. Its bounding box as percentages: top_pct, left_pct, bottom_pct, right_pct (0.0 to 1.0)\n\n"
            "Announcements are visually separated by white space or margins. "
            "Treat every visible white gap between blocks of content as a hard boundary: "
            "one announcement's bounding box must end before the white gap, and the next must start after it. "
            "Never let a bounding box cross a white gap — this causes content from different announcements "
            "to be mixed together, which is strictly forbidden.\n\n"
            "IMPORTANT EXCLUSION RULES — never include these areas in any bounding box:\n"
            "- Page header: the horizontal gray divider line at the top of each page, together with the date "
            "and liturgical name printed above or near it (e.g. '10 de mayo de 2026 - 6 domingo de pascua'). "
            "Every bounding box must start BELOW this header line.\n"
            "- Page footer: the line at the bottom of each page that reads "
            "'View this bulletin online at www.DiscoverMass.com'. "
            "Every bounding box must end ABOVE this footer line.\n\n"
            "Return a JSON array:\n"
            '[{"id": "1", "page": 7, "top": 0.05, "left": 0.0, "bottom": 0.25, "right": 1.0}, ...]\n\n'
            "Return ONLY the JSON array, no explanation."
        )
    })

    for page_num in PAGES_RANGE:
        img_path = pages_dir / f"page_{page_num:02d}.png"
        if not img_path.exists():
            continue
        content.append({"type": "text", "text": f"Page {page_num}:"})
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": encode_image(img_path),
            }
        })

    content.append({
        "type": "text",
        "text": "Announcements to locate:\n" + "\n".join(
            f'- id={a["id"]}: {a["title"]}' for a in announcements
        )
    })

    response = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=1024,
        messages=[{"role": "user", "content": content}],
    )

    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    locations = json.loads(raw)
    return {str(item["id"]): item for item in locations}


def crop_announcement(pages_dir: Path, location: dict) -> Image.Image:
    """Recorta a região do anúncio da página com padding."""
    page_num = location["page"]
    img_path = pages_dir / f"page_{page_num:02d}.png"
    page_img = Image.open(img_path).convert("RGB")
    w, h = page_img.size

    padding = 12
    top = max(0, int(location["top"] * h) - padding)
    left = max(0, int(location["left"] * w) - padding)
    bottom = min(h, int(location["bottom"] * h) + padding)
    right = min(w, int(location["right"] * w) + padding)

    return page_img.crop((left, top, right, bottom))


def load_parish_logo(parish_id: str) -> "Image.Image | None":
    config_dir = Path(__file__).parent.parent.parent / "config" / "parishes" / parish_id
    for ext in ("png", "jpg", "jpeg"):
        path = config_dir / f"logo.{ext}"
        if path.exists():
            return Image.open(path).convert("RGBA")
    return None


def load_placeholder(parish_id: str) -> "Image.Image | None":
    support_dir = Path(__file__).parent.parent.parent / "config" / "parishes" / parish_id / "support"
    path = support_dir / "placeholder.png"
    if path.exists():
        return Image.open(path).convert("RGB")
    return None


def composite_logo(base_img: "Image.Image", logo: "Image.Image", padding: int = 50) -> "Image.Image":
    """Overlays the parish logo at the bottom-center of the generated image."""
    bw, bh = base_img.size
    max_w = int(bw * 0.35)
    max_h = int(bh * 0.15)
    lw, lh = logo.size
    scale = min(max_w / lw, max_h / lh, 1.0)
    new_lw, new_lh = int(lw * scale), int(lh * scale)
    logo_resized = logo.resize((new_lw, new_lh), Image.LANCZOS)
    x = (bw - new_lw) // 2
    y = bh - new_lh - padding
    base_rgba = base_img.convert("RGBA")
    base_rgba.paste(logo_resized, (x, y), mask=logo_resized)
    return base_rgba.convert("RGB")


def load_agent_feedback(parish_id: str, feedback_type: str) -> str:
    path = (
        Path(__file__).parent.parent.parent
        / "config" / "parishes" / parish_id / "agent_feedback" / f"{feedback_type}_feedback.md"
    )
    if not path.exists():
        return ""
    # Strip markdown headers, return only bullet lines
    lines = [l for l in path.read_text().splitlines() if l.startswith("-")]
    return "\n".join(lines)


def build_gemini_prompt(announcement: dict) -> str:
    info_lines = [f'- "{announcement["title"]}" (large headline, keep prominent)']

    if announcement.get("event_date"):
        info_lines.append(f'- Date: {announcement["event_date"]}')
    if announcement.get("location"):
        info_lines.append(f'- Location: {announcement["location"]}')

    body = announcement.get("body", "")
    emails = re.findall(r'[\w.+-]+@[\w-]+\.[a-zA-Z]{2,}', body)
    phones = re.findall(r'\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}', body)
    urls = re.findall(r'https?://[^\s,)\n]+|www\.[^\s,)\n]+', body)

    for e in emails[:2]:
        info_lines.append(f'- Email: {e}')
    for p in phones[:2]:
        info_lines.append(f'- Phone: {p}')
    for u in urls[:2]:
        info_lines.append(f'- Website: {u}')

    info_block = "\n".join(info_lines)

    feedback = announcement.get("_parish_feedback", "")
    feedback_block = f"\n\nPREVIOUS FEEDBACK TO APPLY:\n{feedback}" if feedback else ""

    return f"""You are a professional graphic designer creating a social media post.{feedback_block}

Redesign the attached announcement image into a clean, professional VERTICAL portrait flyer (4:5 ratio, taller than wide — like an Instagram post or story).

PRESERVE from the original image:
- All logos, emblems, and brand marks
- All photos of people or places
- The color scheme and visual style
- Decorative elements and borders

SHOW ONLY this text content (remove all other text from the original):
{info_block}

DESIGN RULES:
- Output must be vertical/portrait orientation (significantly taller than wide)
- Layout must be clean, bold, and easy to read at a glance
- Text hierarchy: title largest, then date/time, then location, then contacts
- The result should look like a polished professional event flyer ready for social media
- Never insert QR codes or barcodes of any kind
- Always display dates in "Month Day, Year" format (e.g. "May 11, 2026")
- Never repeat the same link, email, or phone number more than once
- Never repeat the same logo or emblem more than once in the image

Do not add watermarks, signatures, or any text not listed above."""


def resize_to_canvas(img: Image.Image, size: tuple) -> Image.Image:
    """Redimensiona para o tamanho final mantendo proporção (contain — sem corte)."""
    cw, ch = size
    iw, ih = img.size
    scale = min(cw / iw, ch / ih)
    new_w, new_h = int(iw * scale), int(ih * scale)
    resized = img.resize((new_w, new_h), Image.LANCZOS)
    canvas = Image.new("RGB", (cw, ch), (0, 0, 0))
    x = (cw - new_w) // 2
    y = (ch - new_h) // 2
    canvas.paste(resized, (x, y))
    return canvas


def generate_image_with_gemini(gemini_client: genai.Client, crop: Image.Image, announcement: dict) -> Image.Image | None:
    """Envia apenas o recorte isolado do anúncio ao Gemini."""
    buf = io.BytesIO()
    crop.save(buf, format="PNG")
    img_bytes = buf.getvalue()

    base_prompt = build_gemini_prompt(announcement)
    prompt = base_prompt + (
        "\n\nNOTE: The provided image contains ONLY this specific announcement, "
        "already cropped and isolated. Use ALL visual elements from it (logos, photos, "
        "colors, decorative elements) — do not invent or import any external elements."
    )

    try:
        response = gemini_client.models.generate_content(
            model="gemini-3.1-flash-image-preview",
            contents=[
                types.Part.from_bytes(data=img_bytes, mime_type="image/png"),
                types.Part.from_text(text=prompt),
            ],
            config=types.GenerateContentConfig(response_modalities=["IMAGE", "TEXT"]),
        )
    except Exception as e:
        print(f"  Gemini API error: {e}")
        return None

    candidates = response.candidates or []
    if not candidates:
        return None
    content = candidates[0].content
    if not content or not content.parts:
        return None

    for part in content.parts:
        if hasattr(part, "inline_data") and part.inline_data and "image" in (part.inline_data.mime_type or ""):
            return Image.open(io.BytesIO(part.inline_data.data)).convert("RGB")

    return None


async def run(output_dir: Path, parish_id: str) -> dict[str, str]:
    anthropic_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    gemini_client = genai.Client(api_key=os.environ["GOOGLE_API_KEY"])

    announcements_path = output_dir / "announcements.json"
    announcements = json.loads(announcements_path.read_text())
    pages_dir = output_dir / "pages"

    feedback = load_agent_feedback(parish_id, "image")
    if feedback:
        print(f"  Feedback anterior carregado ({len(feedback.splitlines())} instruções).")
        for ann in announcements:
            ann["_parish_feedback"] = feedback

    print("Localizando anúncios nas páginas (Claude)...")
    locations = locate_announcements(anthropic_client, pages_dir, announcements)

    # Salva localizações para reutilização em edições individuais
    locations_path = output_dir / "locations.json"
    locations_path.write_text(json.dumps(locations, indent=2, ensure_ascii=False))

    parish_logo = load_parish_logo(parish_id)
    if parish_logo:
        print("  Logo da paróquia encontrado — será sobreposto nas imagens geradas.")

    placeholder_img = load_placeholder(parish_id)
    if placeholder_img:
        print("  Placeholder encontrado — será usado como fallback se o Gemini falhar.")

    images_dir = output_dir / "images"
    images_dir.mkdir(exist_ok=True)

    results = {}
    for ann in announcements:
        ann_id = str(ann["id"])
        title = ann.get("title", "")
        print(f"Gerando imagem: [{ann_id}] {title}...")

        location = locations.get(ann_id)
        if not location:
            print(f"  Aviso: região não encontrada para anúncio {ann_id}, pulando.")
            continue

        crop = crop_announcement(pages_dir, location)

        generated = generate_image_with_gemini(gemini_client, crop, ann)
        using_placeholder = False
        if generated is None:
            if placeholder_img:
                print(f"  Aviso: Gemini não retornou imagem para [{ann_id}], usando placeholder.")
                generated = placeholder_img
                using_placeholder = True
            else:
                print(f"  Aviso: Gemini não retornou imagem para [{ann_id}], usando recorte redimensionado.")
                generated = crop

        final_img = resize_to_canvas(generated, OUTPUT_SIZE)
        if parish_logo and not using_placeholder:
            final_img = composite_logo(final_img, parish_logo)

        filename = f"announcement_{ann_id.zfill(2)}.png"
        out_path = images_dir / filename
        final_img.save(str(out_path), "PNG")

        results[ann_id] = str(out_path)
        print(f"  Salvo: {out_path} ({final_img.size})")

    print(f"\n{len(results)} imagem(ns) gerada(s) em {images_dir}")
    return results


if __name__ == "__main__":
    output_dir = Path("output/skdrexel/2026-05-06")
    results = asyncio.run(run(output_dir=output_dir, parish_id="skdrexel"))

    print("\n--- IMAGENS GERADAS ---")
    for ann_id, path in results.items():
        print(f"  [{ann_id}] {path}")
