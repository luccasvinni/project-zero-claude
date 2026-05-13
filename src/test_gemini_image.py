"""
Teste do Gemini 3.1 Flash Image para recriar anúncios do boletim.
"""
import base64
import os
from pathlib import Path
from PIL import Image
from google import genai
from google.genai import types
from dotenv import load_dotenv

load_dotenv()

OUTPUT_DIR = Path("output/skdrexel/2026-05-06")
TEST_DIR = OUTPUT_DIR / "test_gemini"
TEST_DIR.mkdir(exist_ok=True)

ANNOUNCEMENT_CROPS = {
    "cbnsfl": {
        "page": "page_08.png",
        "box": (0.0, 0.52, 1.0, 1.0),  # (left%, top%, right%, bottom%)
        "info": {
            "title": "Join Our CBNSFL Talk & Networking Event",
            "date": "Wednesday, May 27",
            "time": "6:30 PM – 9 PM",
            "location": '"Freedom" Center – 13801 NW 14th Street, Sunrise, Florida 33323',
            "contacts": "cbnsfl.org",
        }
    },
    "renovacion": {
        "page": "page_09.png",
        "box": (0.0, 0.0, 1.0, 0.32),
        "info": {
            "title": "Renovación de Votos Matrimoniales Sacramentales",
            "date": "12 de Mayo",
            "time": "7pm",
            "location": None,
            "contacts": "skdpastoral@gmail.com | (954) 531-8047",
        }
    },
}


def crop_region(page_path: Path, box: tuple) -> Image.Image:
    img = Image.open(page_path).convert("RGB")
    w, h = img.size
    left = int(box[0] * w)
    top = int(box[1] * h)
    right = int(box[2] * w)
    bottom = int(box[3] * h)
    return img.crop((left, top, right, bottom))


def image_to_bytes(img: Image.Image) -> bytes:
    import io
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def build_prompt(info: dict) -> str:
    instructions_path = Path(__file__).parent / "prompts" / "image_generation_instructions.txt"
    base = instructions_path.read_text(encoding="utf-8").strip()

    lines = [f'- Title: "{info["title"]}" (large headline, keep prominent)']
    if info.get("date"):
        lines.append(f'- Date: {info["date"]}')
    if info.get("time"):
        lines.append(f'- Time: {info["time"]}')
    if info.get("location"):
        lines.append(f'- Location: {info["location"]}')
    if info.get("contacts"):
        lines.append(f'- Contact: {info["contacts"]}')

    return base + "\n\n\n== CONTENT FOR THIS ANNOUNCEMENT ==\n\n" + "\n".join(lines)


def test_announcement(name: str, config: dict):
    print(f"\nTestando: {name}")

    pages_dir = OUTPUT_DIR / "pages"
    page_path = pages_dir / config["page"]

    crop = crop_region(page_path, config["box"])
    crop_path = TEST_DIR / f"{name}_crop.png"
    crop.save(str(crop_path))
    print(f"  Recorte salvo: {crop_path}")

    prompt = build_prompt(config["info"])

    client = genai.Client(api_key=os.environ["GOOGLE_API_KEY"])

    print(f"  Enviando para Gemini 3.1 Flash Image...")
    response = client.models.generate_content(
        model="gemini-3.1-flash-image-preview",
        contents=[
            types.Part.from_bytes(data=image_to_bytes(crop), mime_type="image/png"),
            types.Part.from_text(text=prompt),
        ],
        config=types.GenerateContentConfig(
            response_modalities=["IMAGE", "TEXT"],
        ),
    )

    for part in response.candidates[0].content.parts:
        if part.inline_data and "image" in part.inline_data.mime_type:
            img_bytes = part.inline_data.data
            out_path = TEST_DIR / f"{name}_resultado.png"
            out_path.write_bytes(img_bytes)
            print(f"  Resultado salvo: {out_path}")
            return
        elif hasattr(part, "text") and part.text:
            print(f"  Resposta texto: {part.text[:200]}")

    print(f"  Nenhuma imagem gerada para {name}.")


if __name__ == "__main__":
    for name, config in ANNOUNCEMENT_CROPS.items():
        test_announcement(name, config)

    print("\nTeste concluído. Verifique as imagens em:", TEST_DIR)
