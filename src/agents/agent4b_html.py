import asyncio
import json
import os
from pathlib import Path

import anthropic
from PIL import Image
from pyzbar.pyzbar import decode as qr_decode
from dotenv import load_dotenv

load_dotenv()

PROMPTS_DIR = Path(__file__).parent.parent / "prompts"


def detect_qr_codes(pages_dir: Path) -> list[str]:
    """Lê QR codes de todas as imagens de página salvas."""
    urls = []
    if not pages_dir.exists():
        return urls
    for img_path in sorted(pages_dir.glob("page_*.png")):
        try:
            img = Image.open(img_path)
            decoded = qr_decode(img)
            for obj in decoded:
                url = obj.data.decode("utf-8")
                if url not in urls:
                    urls.append(url)
        except Exception:
            pass
    if urls:
        print(f"QR codes detectados: {urls}")
    return urls


def load_parish_config(parish_id: str) -> dict:
    import yaml  # type: ignore
    config_path = (
        Path(__file__).parent.parent.parent / "config" / "parishes" / f"{parish_id}.yaml"
    )
    if config_path.exists():
        try:
            return yaml.safe_load(config_path.read_text()) or {}
        except Exception:
            pass
    return {}


def load_html_feedback(parish_id: str) -> str:
    path = (
        Path(__file__).parent.parent.parent
        / "config" / "parishes" / parish_id / "agent_feedback" / "html_feedback.md"
    )
    if not path.exists():
        return ""
    lines = [l for l in path.read_text().splitlines() if l.startswith("-")]
    return "\n".join(lines)


def load_system_prompt(parish_id: str) -> str:
    config = load_parish_config(parish_id)
    internal_domain = config.get("parish", {}).get("internal_domain", "")
    template_path = (
        Path(__file__).parent.parent.parent
        / "config" / "parishes" / parish_id / "html_template.txt"
    )
    template = template_path.read_text() if template_path.exists() else ""
    raw = (PROMPTS_DIR / "html_system_prompt.txt").read_text()
    prompt = raw.replace("{template}", template).replace("{internal_domain}", internal_domain)

    feedback = load_html_feedback(parish_id)
    if feedback:
        prompt += f"\n\n---\n\nFEEDBACK DE RUNS ANTERIORES (aplique estas lições):\n{feedback}"

    return prompt


def build_user_message(announcement: dict, qr_urls: list[str]) -> str:
    lines = [
        f"Title: {announcement.get('title', '')}",
        f"Category: {announcement.get('category', '')}",
        f"Event date: {announcement.get('event_date') or 'not specified'}",
        f"Location: {announcement.get('location') or 'not specified'}",
        "",
        "Body:",
        announcement.get("body", ""),
    ]
    if qr_urls:
        lines.append("")
        lines.append("QR code URL(s) found in the bulletin page:")
        for url in qr_urls:
            lines.append(f"- {url}")
    return "\n".join(lines)


async def generate_html_for_announcement(
    client: anthropic.Anthropic,
    announcement: dict,
    system_prompt: str,
    qr_urls: list[str],
) -> str:
    user_message = build_user_message(announcement, qr_urls)
    response = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=4096,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
    )
    html = response.content[0].text.strip()
    # Remove possível markdown code block
    if html.startswith("```"):
        html = html.split("```")[1]
        if html.startswith("html"):
            html = html[4:]
        html = html.strip()
    return html


async def run(output_dir: Path, parish_id: str) -> dict[str, str]:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    system_prompt = load_system_prompt(parish_id)

    announcements_path = output_dir / "announcements.json"
    announcements = json.loads(announcements_path.read_text())

    qr_urls = detect_qr_codes(output_dir / "pages")

    html_dir = output_dir / "html"
    html_dir.mkdir(exist_ok=True)

    results = {}
    for ann in announcements:
        ann_id = ann.get("id", str(ann.get("order", "unknown")))
        print(f"Gerando HTML: [{ann_id}] {ann.get('title', '')}...")

        html = await generate_html_for_announcement(client, ann, system_prompt, qr_urls)

        filename = f"announcement_{ann_id:0>2}.html" if str(ann_id).isdigit() else f"announcement_{ann_id}.html"
        file_path = html_dir / filename
        file_path.write_text(html, encoding="utf-8")

        results[ann_id] = str(file_path)

    print(f"\n{len(results)} arquivo(s) HTML gerado(s) em {html_dir}")
    return results


if __name__ == "__main__":
    output_dir = Path("output/skdrexel/2026-05-06")
    results = asyncio.run(run(output_dir=output_dir, parish_id="skdrexel"))

    print("\n--- ARQUIVOS HTML GERADOS ---")
    for ann_id, path in results.items():
        print(f"  [{ann_id}] {path}")
