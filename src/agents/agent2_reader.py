import asyncio
import base64
import json
import os
from pathlib import Path

import anthropic
import pdfplumber
from pdf2image import convert_from_path
from dotenv import load_dotenv

load_dotenv()

PROMPTS_DIR = Path(__file__).parent.parent / "prompts"
EXAMPLES_DIR = Path(__file__).parent.parent.parent / "config" / "parishes"
CONFIG_DIR = Path(__file__).parent.parent.parent / "config" / "parishes"

DEFAULT_BULLETIN_PAGES = [7, 8, 9, 10]


def _load_bulletin_pages(parish_id: str) -> list[int]:
    import yaml
    config_path = CONFIG_DIR / f"{parish_id}.yaml"
    if config_path.exists():
        cfg = yaml.safe_load(config_path.read_text()) or {}
        pages = cfg.get("reader", {}).get("bulletin_pages")
        if pages:
            return list(pages)
    return DEFAULT_BULLETIN_PAGES


def extract_pages_as_images(pdf_path: Path, pages: list[int], output_dir: Path) -> list[bytes]:
    """Converte páginas específicas do PDF em imagens PNG e salva no disco."""
    print(f"Convertendo páginas {pages} do PDF para imagens...")
    images = convert_from_path(
        str(pdf_path),
        dpi=150,
        first_page=min(pages),
        last_page=max(pages),
        fmt="png",
    )
    pages_dir = output_dir / "pages"
    pages_dir.mkdir(exist_ok=True)

    result = []
    for i, page_num in enumerate(pages):
        img = images[page_num - min(pages)]
        page_path = pages_dir / f"page_{page_num:02d}.png"
        img.save(str(page_path), format="PNG")

        import io
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        result.append(buf.getvalue())

    print(f"{len(result)} páginas convertidas e salvas em {pages_dir}")
    return result


def load_examples(parish_id: str) -> list[dict]:
    """Carrega imagens de exemplo da pasta examples da paróquia."""
    examples_path = EXAMPLES_DIR / parish_id / "examples"
    if not examples_path.exists():
        return []

    examples = []
    for img_file in sorted(examples_path.glob("*.png")) or sorted(examples_path.glob("*.jpg")):
        img_bytes = img_file.read_bytes()
        examples.append({
            "filename": img_file.name,
            "data": base64.standard_b64encode(img_bytes).decode("utf-8"),
            "media_type": "image/png" if img_file.suffix == ".png" else "image/jpeg",
        })

    if examples:
        print(f"{len(examples)} exemplo(s) de referência carregado(s).")
    return examples


def build_messages(page_images: list[bytes], examples: list[dict], bulletin_pages: list[int]) -> list[dict]:
    """Monta o payload de mensagens para a API do Claude."""
    content = []

    today = __import__("datetime").date.today().isoformat()

    if examples:
        content.append({
            "type": "text",
            "text": "Here are reference examples showing how announcements appear in this parish's bulletins:"
        })
        for ex in examples:
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": ex["media_type"],
                    "data": ex["data"],
                }
            })
        content.append({
            "type": "text",
            "text": f"Today's date is {today}. Now analyze the following bulletin pages and extract all announcements following the same pattern. Ignore any event whose date has already passed."
        })
    else:
        content.append({
            "type": "text",
            "text": f"Today's date is {today}. Analyze the following {len(page_images)} bulletin pages (pages {bulletin_pages}) and extract all announcements. Ignore any event whose date has already passed."
        })

    for i, img_bytes in enumerate(page_images):
        content.append({
            "type": "text",
            "text": f"Page {bulletin_pages[i]}:"
        })
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": base64.standard_b64encode(img_bytes).decode("utf-8"),
            }
        })

    return [{"role": "user", "content": content}]


def parse_announcements(response_text: str) -> list[dict]:
    """Extrai e valida o JSON retornado pelo Claude."""
    text = response_text.strip()
    # Remove possível markdown code block
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    text = text.strip()

    announcements = json.loads(text)
    if not isinstance(announcements, list):
        raise ValueError("Resposta do Claude não é uma lista JSON.")
    return announcements


async def run(pdf_path: Path, parish_id: str, instruction: str = "") -> list[dict]:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    system_prompt = (PROMPTS_DIR / "system_prompt.txt").read_text()
    if instruction.strip():
        system_prompt += f"\n\nADDITIONAL INSTRUCTION FOR THIS RUN:\n{instruction.strip()}"

    bulletin_pages = _load_bulletin_pages(parish_id)
    output_dir = pdf_path.parent
    page_images = extract_pages_as_images(pdf_path, bulletin_pages, output_dir)
    examples = load_examples(parish_id)
    messages = build_messages(page_images, examples, bulletin_pages)

    print("Enviando para Claude API...")
    response = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=4096,
        system=system_prompt,
        messages=messages,
    )

    raw = response.content[0].text
    announcements = parse_announcements(raw)

    # Salva resultado na pasta de output
    output_dir = pdf_path.parent
    output_path = output_dir / "announcements.json"
    output_path.write_text(json.dumps(announcements, indent=2, ensure_ascii=False))

    print(f"{len(announcements)} anúncio(s) identificado(s).")
    print(f"Resultado salvo em: {output_path}")
    return announcements


if __name__ == "__main__":
    pdf_path = Path("output/skdrexel/2026-05-06/20260503.pdf")
    announcements = asyncio.run(run(pdf_path=pdf_path, parish_id="skdrexel"))

    print("\n--- ANÚNCIOS ENCONTRADOS ---")
    for ann in announcements:
        print(f"\n[{ann['order']}] {ann['title']}")
        print(f"    Categoria: {ann['category']}")
        if ann.get('event_date'):
            print(f"    Data: {ann['event_date']}")
        print(f"    Corpo: {ann['body'][:80]}...")
