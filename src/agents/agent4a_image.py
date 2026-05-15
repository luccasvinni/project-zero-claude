import asyncio
import base64
import io
import json
import os
from pathlib import Path

import anthropic
from google import genai
from google.genai import types
from PIL import Image
from dotenv import load_dotenv

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from log_ai import log_ai_tokens

load_dotenv()

OUTPUT_SIZE = (1080, 1350)

EXTRACTION_SYSTEM_PROMPT = """You are a precise data extraction agent. Read the provided announcement image and extract specific information into a structured JSON object.

Extract the following fields:
- "titulo": event title exactly as it appears in the image (required)
- "tipo_evento": classify into exactly one of: "feminine_retreat", "sacramental", "volunteer", "prayer", "saints", "matrimonial", "ministry", "other"
- "data": event date exactly as it appears in the image — preserve the original formatting, capitalization, ordinal suffixes (e.g. "May 18th", "domingo, 18 de mayo", "May 18, 2026") — omit if not present
- "hora": event time exactly as it appears in the image — preserve the original formatting (e.g. "7:00pm", "7:00 PM", "19h00") — omit if not present
- "local": event location exactly as it appears in the image — omit if not present
- "descricao": short description of the event, max 60 characters, only if explicitly present in the image — omit if not present
- "emails": array of email addresses exactly as they appear — omit if not present
- "telefones": array of phone numbers exactly as they appear — omit if not present
- "websites": array of URLs/websites belonging to the announcement — omit if not present

STRICT EXCLUSIONS — never extract the following, even if visible in the image:
- Page headers: the bulletin date line and liturgical day name printed at the very top of the page
- Page footers: any text referencing DiscoverMass, bulletin printing services, or lines such as "View this bulletin online at www.DiscoverMass.com" — these are printing infrastructure and must be completely ignored
- Any URL containing "discovermass" (case-insensitive) must never appear in the output
- QR codes and barcodes: never extract the URL or content encoded in a QR code or barcode as a website — QR codes are handled separately and must not appear in the extracted data

Rules:
- Return ONLY a valid JSON object, no explanation or markdown fences
- Omit any field that is not present in the image — never invent or assume information
- For arrays (emails, telefones, websites), include all instances found"""

EVENT_STYLE_MAP = {
    "feminine_retreat": "Soft, elegant palettes — delicate florals, warm light, refined typography. Convey grace, intimacy, and spiritual femininity.",
    "sacramental": "Sober, classic tones — use recognizable and spiritually meaningful iconography. Avoid generic imagery; every visual element must carry liturgical weight.",
    "volunteer": "Vibrant, warm colors with a strong sense of community. Show people actively engaged and welcomed. Energetic yet approachable.",
    "prayer": "Sacred atmosphere — use elements such as a lit monstrance, candles, and sacred penumbra. Avoid flat or spiritually shallow imagery; convey depth and reverence.",
    "saints": "Include recognizable iconography of the saint — traditional or historical depictions. Reverent and timeless aesthetic.",
    "matrimonial": "Represent couples in a liturgical context — altar, candles, rings. Elegant, solemn, and tender.",
    "ministry": "Show people actively engaged in the liturgy — community, service, and devotion. Warm and inviting.",
    "other": "Clean, professional, and welcoming. Use contextual visual elements that match the specific theme of the event.",
}


def encode_image(img_path: Path) -> str:
    return base64.standard_b64encode(img_path.read_bytes()).decode("utf-8")


def locate_announcements(client: anthropic.Anthropic, pages_dir: Path, announcements: list[dict], atimo_team: bool = False) -> dict:
    """Usa Claude para identificar a região de cada anúncio nas páginas."""
    page_files = sorted(pages_dir.glob("page_*.png"))
    page_nums = [int(f.stem.split("_")[1]) for f in page_files]
    pages_list = ", ".join(str(n) for n in page_nums)

    content = []
    content.append({
        "type": "text",
        "text": (
            f"The following images are pages {pages_list} of a Catholic parish bulletin. "
            f"For each announcement listed below, identify:\n"
            f"1. Which page it appears on ({pages_list})\n"
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
            f'[{{"id": "1", "page": {page_nums[0] if page_nums else 1}, "top": 0.05, "left": 0.0, "bottom": 0.25, "right": 1.0}}, ...]\n\n'
            "Return ONLY the JSON array, no explanation."
        )
    })

    for page_num in page_nums:
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
    log_ai_tokens(response, task="image_locate", atimo_team=atimo_team)

    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    locations = json.loads(raw)
    return {str(item["id"]): item for item in locations}


def crop_announcement(pages_dir: Path, location: dict, padding: int = 12) -> Image.Image:
    """Recorta a região do anúncio da página com padding."""
    if location.get("manual"):
        padding = 0
    page_num = location["page"]
    img_path = pages_dir / f"page_{page_num:02d}.png"
    page_img = Image.open(img_path).convert("RGB")
    w, h = page_img.size

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


def extract_announcement_json(client: anthropic.Anthropic, crop: Image.Image, atimo_team: bool = False) -> dict:
    """Agent 1: Claude Vision reads the crop and extracts structured JSON."""
    buf = io.BytesIO()
    crop.save(buf, format="PNG")
    img_b64 = base64.standard_b64encode(buf.getvalue()).decode()

    response = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=1024,
        system=EXTRACTION_SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": img_b64},
                },
                {"type": "text", "text": "Extract the announcement data from this image."},
            ],
        }],
    )
    log_ai_tokens(response, task="image_extract_json", atimo_team=atimo_team)

    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    return json.loads(raw)


def build_prompt_from_json(data: dict) -> str:
    """Agent 2: builds the final Gemini prompt by injecting JSON data into the template."""
    template = (Path(__file__).parent.parent / "prompts" / "prompt_final.txt").read_text(encoding="utf-8")

    lines = [
        "[TEXT DATA TO RENDER - EXACT STRINGS ONLY]",
        "CRITICAL: The capitalized labels below (TITLE, DESCRIPTION, DATE, TIME, DATE & TIME, LOCATION, CONTACT, WEBSITE) are internal layout hints ONLY. They must NEVER appear as visible text in the final image — not as headings, subtitles, captions, or in any form. Render ONLY the quoted string values that follow each label. Each value must appear EXACTLY ONCE.",
    ]

    if data.get("titulo"):
        lines.append(f'- TITLE: "{data["titulo"]}"')
    if data.get("descricao"):
        lines.append(f'- DESCRIPTION: "{data["descricao"]}"')

    data_val = data.get("data")
    hora_val = data.get("hora")
    if data_val:
        lines.append(f'- DATE: "{data_val}"')
    if hora_val:
        lines.append(f'- TIME: "{hora_val}"')

    if data.get("local"):
        lines.append(f'- LOCATION: "{data["local"]}"')

    contatos = data.get("telefones", []) + data.get("emails", [])
    if contatos:
        lines.append(f'- CONTACT: "{" | ".join(contatos)}"')

    for url in data.get("websites", []):
        if "discovermass" not in url.lower():
            lines.append(f'- WEBSITE: "{url}"')

    text_data_block = "\n".join(lines) + "\n\n"
    style_block = EVENT_STYLE_MAP.get(data.get("tipo_evento", "other"), EVENT_STYLE_MAP["other"])

    prompt = template.replace("{text_data_block}", text_data_block).replace("{style_block}", style_block)

    if data.get("_strict_crop"):
        prompt += (
            "\n\n[MANUAL SELECTION — STRICT BOUNDS]\n"
            "The reference image is an exact region manually selected by the user. "
            "You must base the entire design EXCLUSIVELY on the content visible in that image. "
            "Do not infer, add, or reference ANY element that is not explicitly present in this crop."
        )

    edit_request = data.get("_edit_request", "")
    if edit_request:
        prompt += f"\n\n[TARGETED EDIT]\nApply only this change, keep everything else intact:\n- {edit_request}"

    return prompt


def build_prompt_with_claude(client: anthropic.Anthropic, ann_json: dict, crop: Image.Image, atimo_team: bool = False) -> str:
    """Usa Claude para gerar o prompt do Gemini com base no JSON extraído e no recorte visual."""
    buf = io.BytesIO()
    crop.save(buf, format="PNG")
    img_b64 = base64.standard_b64encode(buf.getvalue()).decode()

    style_block = EVENT_STYLE_MAP.get(ann_json.get("tipo_evento", "other"), EVENT_STYLE_MAP["other"])

    data_lines = []
    if ann_json.get("titulo"):
        data_lines.append(f'Title: "{ann_json["titulo"]}"')
    if ann_json.get("data"):
        data_lines.append(f'Date: "{ann_json["data"]}"')
    if ann_json.get("hora"):
        data_lines.append(f'Time: "{ann_json["hora"]}"')
    if ann_json.get("local"):
        data_lines.append(f'Location: "{ann_json["local"]}"')
    if ann_json.get("descricao"):
        data_lines.append(f'Description: "{ann_json["descricao"]}"')
    contatos = ann_json.get("telefones", []) + ann_json.get("emails", [])
    if contatos:
        data_lines.append(f'Contact: "{" | ".join(contatos)}"')
    for url in ann_json.get("websites", []):
        if "discovermass" not in url.lower():
            data_lines.append(f'Website: "{url}"')

    edit_request = ann_json.get("_edit_request", "")

    user_msg = (
        "Write a detailed image generation prompt for Gemini AI to create a professional "
        "parish announcement flyer (1080×1350px, portrait orientation for social media).\n\n"
        f"ANNOUNCEMENT DATA (use these exact text values in the image):\n" + "\n".join(data_lines) + "\n\n"
        f"VISUAL STYLE:\n{style_block}\n\n"
        "The attached bulletin crop is for context only — the generated image must be a fully "
        "redesigned social media flyer, not a copy of the original.\n\n"
        "Requirements:\n"
        "- Specify which text strings must appear and where (title prominent, date/time/location below)\n"
        "- Each text value must appear EXACTLY ONCE — never repeat any text\n"
        "- Describe colors, typography, background, and visual elements in detail\n"
        "- Catholic spiritual atmosphere appropriate to the event type\n"
        + (f"\nTARGETED EDIT — apply only this change, keep everything else intact:\n- {edit_request}\n" if edit_request else "")
        + "\nReturn ONLY the prompt text, no preamble or explanation."
    )

    response = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img_b64}},
                {"type": "text", "text": user_msg},
            ],
        }],
    )
    log_ai_tokens(response, task="image_build_prompt", atimo_team=atimo_team)
    return response.content[0].text.strip()


def resize_to_canvas(img: Image.Image, size: tuple) -> Image.Image:
    """Redimensiona para o tamanho final preenchendo o canvas sem barras (cover — sem corte central)."""
    cw, ch = size
    iw, ih = img.size
    scale = max(cw / iw, ch / ih)
    new_w, new_h = int(iw * scale), int(ih * scale)
    resized = img.resize((new_w, new_h), Image.LANCZOS)
    x = (new_w - cw) // 2
    y = (new_h - ch) // 2
    return resized.crop((x, y, x + cw, y + ch))


def generate_image_with_gemini(gemini_client: genai.Client, crop: Image.Image, prompt: str, atimo_team: bool = False) -> Image.Image | None:
    """Image-to-image via Gemini 3.1 Flash Image Preview."""
    buf = io.BytesIO()
    crop.save(buf, format="PNG")
    img_bytes = buf.getvalue()

    try:
        response = gemini_client.models.generate_content(
            model="gemini-3.1-flash-image-preview",
            contents=[
                types.Part.from_bytes(data=img_bytes, mime_type="image/png"),
                types.Part.from_text(text=prompt),
            ],
            config=types.GenerateContentConfig(response_modalities=["IMAGE", "TEXT"]),
        )
        log_ai_tokens(response, task="image_generate_gemini", atimo_team=atimo_team,
                      model_hint="gemini-3.1-flash-image-preview")
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


async def run(output_dir: Path, parish_id: str, prompt_mode: str = "python", atimo_team: bool = False) -> dict[str, str]:
    anthropic_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    gemini_client = genai.Client(api_key=os.environ["GOOGLE_API_KEY"])

    announcements_path = output_dir / "announcements.json"
    announcements = json.loads(announcements_path.read_text())
    pages_dir = output_dir / "pages"

    print("Localizando anúncios nas páginas (Claude)...")
    locations = locate_announcements(anthropic_client, pages_dir, announcements, atimo_team=atimo_team)

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

        print(f"  Extraindo dados da imagem...")
        ann_json = extract_announcement_json(anthropic_client, crop, atimo_team=atimo_team)
        print(f"  JSON extraído: {ann_json}")

        if prompt_mode == "claude":
            print(f"  Gerando prompt via Claude...")
            prompt = build_prompt_with_claude(anthropic_client, ann_json, crop, atimo_team=atimo_team)
        else:
            prompt = build_prompt_from_json(ann_json)

        generated = generate_image_with_gemini(gemini_client, crop, prompt, atimo_team=atimo_team)
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
