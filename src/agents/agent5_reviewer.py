import asyncio
import base64
import io
import json
import os
import sys
from pathlib import Path

import anthropic
from PIL import Image
from dotenv import load_dotenv

load_dotenv()

MAX_RETRIES = 2

# Controla se o agente pode acionar regenerações automáticas (4A/4B).
# False = apenas revisa e reporta. True = regenera automaticamente ao reprovar.
AUTO_REGEN = False

REVIEW_SYSTEM_PROMPT = """Você é um revisor de qualidade rigoroso para conteúdo de boletins paroquiais católicos.

IDIOMA OBRIGATÓRIO: Escreva TODOS os textos dentro de "issues" EXCLUSIVAMENTE em português brasileiro (PT-BR). Nunca use inglês.

Você receberá:
1. A IMAGEM FONTE — recorte original do boletim (quando disponível), para referência de formato e conteúdo
2. A IMAGEM GERADA — flyer criado pelo agente para redes sociais
3. O HTML gerado para o anúncio
4. Os dados do anúncio (título, data, local)

═══════════════════════════════════
VERIFICAÇÕES DA IMAGEM GERADA
═══════════════════════════════════

1. CONTAMINAÇÃO CRUZADA
   A imagem contém logos, fotos, textos ou links que claramente pertencem a OUTRO anúncio
   diferente do informado nos dados? Se sim, liste cada item intruso encontrado.

2. ESTRUTURA BÁSICA
   Verifique se a imagem possui TODOS os elementos abaixo. Liste separadamente cada ausente:
   - Foto ou imagem ilustrativa (não apenas fundo sem imagem real)
   - Título do anúncio, legível e em destaque
   - Data e hora no mesmo formato visual da imagem fonte
   - 1 ou mais logotipos presentes na imagem fonte
   - Local/endereço do evento
   - Informação de contato (telefone, e-mail ou site)

3. ERROS ORTOGRÁFICOS NA IMAGEM
   Analise o texto visível na imagem gerada. Liste cada palavra com erro ortográfico.

4. QR CODE NA IMAGEM
   A imagem contém algum QR code visível? Se sim, sinalize como problema.

═══════════════════════════════════
VERIFICAÇÕES DO HTML
═══════════════════════════════════

1. ESTRUTURA BÁSICA DO HTML
   Em CADA versão de idioma (EN, ES, PT), verifique se há:
   - Data e hora
   - Descrição do evento
   - Informação de contato (telefone, e-mail ou site)
   Liste cada elemento ausente por idioma.
   IMPORTANTE: ter 3 versões separadas por "- - -" é o formato correto — NÃO é problema estrutural.
   CONSOLIDAÇÃO: se o mesmo elemento estiver ausente nos 3 idiomas, escreva UMA única entrada
   prefixada com "EN/ES/PT:" (ex.: "EN/ES/PT: ausência de informação de contato") em vez de
   repetir 3 entradas separadas. Use "EN/ES:" ou "EN/PT:" etc. quando faltar em apenas 2.

2. ERROS ORTOGRÁFICOS NO HTML
   Há palavras com erro ortográfico NÃO marcadas em <span style="color:red; font-weight: bold;">?
   Verifique cada trecho NO IDIOMA CORRETO: inglês, espanhol ou português.
   Palavras corretas em outro idioma (ex: "mayo", "parroquia" em espanhol) NÃO são erros.
   CONSOLIDAÇÃO: se o mesmo erro (mesma palavra/trecho) ocorrer nos 3 idiomas, escreva UMA única
   entrada prefixada com "EN/ES/PT:" em vez de repetir 3 entradas separadas.

3. TERMOS QR NO HTML
   O HTML contém os termos "QR", "QR code" ou "QR-CODE" como texto visível (não dentro de uma URL)?
   Se sim, sinalize como problema — o link já foi inserido e o termo deve ser removido.

4. ESTRUTURA HTML
   O HTML está bem formado? (sem tags abertas, sem caracteres corrompidos)

═══════════════════════════════════
FORMATO DA RESPOSTA
═══════════════════════════════════

Retorne um objeto JSON com esta estrutura exata:
{
  "image": {
    "approved": true/false,
    "issues": ["problemas encontrados em PT-BR, lista vazia se aprovado"]
  },
  "html": {
    "approved": true/false,
    "spelling_errors": ["palavras com erro ortográfico ainda não marcadas no HTML"],
    "issues": ["problemas estruturais, QR codes ou outros do HTML em PT-BR, lista vazia se aprovado"]
  },
  "overall_approved": true/false
}

Seja rigoroso mas justo. "overall_approved" é true apenas quando AMBOS imagem e html estão aprovados.
Regra de consolidação: quando o mesmo problema ocorrer nos 3 idiomas (EN, ES, PT), registre
UMA única entrada prefixada com "EN/ES/PT:" em vez de três entradas idênticas.
Retorne APENAS o objeto JSON, sem explicações."""


CORRECTION_SYSTEM_PROMPT = """Você é um assistente especializado em correção de textos para boletins paroquiais católicos em inglês e espanhol latino.

Receberá um HTML e uma descrição dos problemas identificados pelo revisor. Seu trabalho é localizar no HTML o trecho exato que precisa ser corrigido e sugerir o valor correto.

REGRAS:
- O campo "original" deve conter o trecho EXATO como aparece no HTML — pode ser uma palavra ou uma frase inteira.
- O campo "suggestion" deve conter apenas o texto substituto (não o HTML completo ao redor).
- O campo "context" deve mostrar até 80 caracteres do HTML ao redor para identificar onde está o trecho.
- Nunca quebre uma frase problemática em partes — trate o trecho completo como uma unidade.
- Se o problema é a presença de um texto inteiro que deve ser removido, use "" como suggestion.

Retorne um JSON array com objetos no formato:
[
  {"original": "trecho exato no HTML", "suggestion": "texto corrigido", "context": "trecho ao redor no HTML (até 80 caracteres)"},
  ...
]

Retorne APENAS o JSON array, sem explicação."""


def encode_image_file(img_path: Path) -> str:
    return base64.standard_b64encode(img_path.read_bytes()).decode("utf-8")


def check_image_size(img_path: Path) -> tuple[bool, str]:
    """Verifica se a imagem tem exatamente 1080x1350px."""
    img = Image.open(img_path)
    w, h = img.size
    if (w, h) == (1080, 1350):
        return True, ""
    return False, f"Tamanho incorreto: {w}x{h}px (esperado 1080x1350px)"


def _pil_to_b64(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.standard_b64encode(buf.getvalue()).decode("utf-8")


def review_announcement(
    client: anthropic.Anthropic,
    ann: dict,
    img_path: Path,
    html_path: Path,
    source_crop: "Image.Image | None" = None,
) -> dict:
    """Envia imagem fonte + imagem gerada + HTML ao Claude para revisão."""
    html_content = html_path.read_text(encoding="utf-8") if html_path.exists() else ""
    img_exists = img_path.exists()

    size_ok, size_error = check_image_size(img_path) if img_exists else (False, "Imagem não encontrada")

    content = []

    content.append({
        "type": "text",
        "text": (
            f"DADOS DO ANÚNCIO:\n"
            f"Título: {ann.get('title')}\n"
            f"Data: {ann.get('event_date') or 'não informada'}\n"
            f"Local: {ann.get('location') or 'não informado'}\n"
            f"Categoria: {ann.get('category')}\n"
        )
    })

    if source_crop:
        content.append({"type": "text", "text": "IMAGEM FONTE (recorte original do boletim — use como referência de formato e conteúdo):"})
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": _pil_to_b64(source_crop)}
        })

    if img_exists:
        content.append({"type": "text", "text": "IMAGEM GERADA pelo agente (avalie esta):"})
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": encode_image_file(img_path),
            }
        })
    else:
        content.append({
            "type": "text",
            "text": "IMAGEM GERADA: arquivo não encontrado. Marque imagem como reprovada."
        })

    content.append({
        "type": "text",
        "text": f"HTML GERADO:\n```html\n{html_content}\n```"
    })

    response = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=1024,
        system=REVIEW_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": content}],
    )

    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    result = json.loads(raw)

    # Injeta erro de tamanho se houver
    if not size_ok:
        result["image"]["approved"] = False
        result["image"]["issues"].insert(0, size_error)
        result["overall_approved"] = False

    return result


async def trigger_agent_4a(output_dir: Path, parish_id: str, ann_id: str):
    """Aciona o Agente 4A para regenerar a imagem de um anúncio específico."""
    sys.path.insert(0, str(Path(__file__).parent))
    from agent4a_image import (
        locate_announcements, crop_announcement, generate_image_with_gemini,
        resize_to_canvas, OUTPUT_SIZE
    )
    import google.genai as genai_mod

    announcements_path = output_dir / "announcements.json"
    announcements = json.loads(announcements_path.read_text())
    ann = next((a for a in announcements if str(a.get("id")) == ann_id), None)
    if not ann:
        return

    gemini_client = genai_mod.Client(api_key=os.environ["GOOGLE_API_KEY"])
    pages_dir = output_dir / "pages"

    # Use cached locations if available; otherwise locate only this announcement
    locations_cache_path = output_dir / "locations.json"
    if locations_cache_path.exists():
        locations = json.loads(locations_cache_path.read_text())
        location = locations.get(ann_id)
    else:
        anthropic_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        locations = locate_announcements(anthropic_client, pages_dir, [ann])
        location = locations.get(ann_id)

    if not location:
        return

    crop = crop_announcement(pages_dir, location)

    generated = generate_image_with_gemini(gemini_client, crop, ann)
    if generated is None:
        generated = crop

    final_img = resize_to_canvas(generated, OUTPUT_SIZE)
    out_path = output_dir / "images" / f"announcement_{ann_id.zfill(2)}.png"
    final_img.save(str(out_path), "PNG")
    print(f"    Imagem regenerada: {out_path}")


async def trigger_agent_4b(output_dir: Path, parish_id: str, ann_id: str, spelling_errors: list[str]):
    """Aciona o Agente 4B para regenerar o HTML de um anúncio específico."""
    sys.path.insert(0, str(Path(__file__).parent))
    from agent4b_html import load_system_prompt, generate_html_for_announcement, detect_qr_codes

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    system_prompt = load_system_prompt(parish_id)

    announcements_path = output_dir / "announcements.json"
    announcements = json.loads(announcements_path.read_text())
    ann = next((a for a in announcements if str(a.get("id")) == ann_id), None)
    if not ann:
        return

    qr_urls = detect_qr_codes(output_dir / "pages")

    if spelling_errors:
        system_prompt += (
            f"\n\nCORREÇÃO OBRIGATÓRIA: As seguintes palavras foram identificadas com erro ortográfico "
            f"e ainda NÃO estão marcadas em vermelho. Envolva cada uma delas em "
            f'<span style="color:red; font-weight: bold;">: {", ".join(spelling_errors)}'
        )

    html = await generate_html_for_announcement(client, ann, system_prompt, qr_urls)
    out_path = output_dir / "html" / f"announcement_{ann_id.zfill(2)}.html"
    out_path.write_text(html, encoding="utf-8")
    print(f"    HTML regenerado: {out_path}")


async def run(output_dir: Path, parish_id: str) -> dict:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    announcements_path = output_dir / "announcements.json"
    announcements = json.loads(announcements_path.read_text())

    images_dir = output_dir / "images"
    html_dir = output_dir / "html"
    pages_dir = output_dir / "pages"

    # Load cached locations for source crop comparison
    locations_cache = output_dir / "locations.json"
    locations = json.loads(locations_cache.read_text()) if locations_cache.exists() else {}

    sys.path.insert(0, str(Path(__file__).parent))
    try:
        from agent4a_image import crop_announcement as _crop_ann
    except Exception:
        _crop_ann = None

    report = {}
    approved_ids = []
    rejected_ids = []

    for ann in announcements:
        ann_id = str(ann.get("id", ann.get("order")))
        title = ann.get("title", "")
        print(f"\nRevisando [{ann_id}] {title}...")

        img_path = images_dir / f"announcement_{ann_id.zfill(2)}.png"
        html_path = html_dir / f"announcement_{ann_id.zfill(2)}.html"

        # Load source crop for visual comparison
        source_crop = None
        if _crop_ann and pages_dir.exists():
            location = locations.get(ann_id)
            if location:
                try:
                    source_crop = _crop_ann(pages_dir, location)
                except Exception:
                    pass

        attempt = 0
        review = None

        while attempt <= MAX_RETRIES:
            attempt += 1
            print(f"  Tentativa {attempt}/{MAX_RETRIES + 1}...")

            review = review_announcement(client, ann, img_path, html_path, source_crop)

            if review["overall_approved"]:
                print(f"  APROVADO.")
                break

            img_issues = review["image"].get("issues", [])
            html_issues = review["html"].get("issues", [])
            spelling = review["html"].get("spelling_errors", [])

            print(f"  Reprovado — imagem: {img_issues} | html: {html_issues + spelling}")

            if not AUTO_REGEN or attempt > MAX_RETRIES:
                if not AUTO_REGEN:
                    print(f"  Regeneração automática desativada. Marcado para revisão humana.")
                else:
                    print(f"  Limite de tentativas atingido. Marcado para revisão humana.")
                break

            # Regenera o que for necessário (só executa se AUTO_REGEN = True)
            if not review["image"]["approved"]:
                print(f"  Acionando Agente 4A para regenerar imagem...")
                await trigger_agent_4a(output_dir, parish_id, ann_id)

            if not review["html"]["approved"]:
                print(f"  Acionando Agente 4B para regenerar HTML...")
                await trigger_agent_4b(output_dir, parish_id, ann_id, spelling)

        final_status = "approved" if review and review["overall_approved"] else "needs_review"
        report[ann_id] = {
            "title": title,
            "status": final_status,
            "attempts": attempt,
            "review": review,
        }

        if final_status == "approved":
            approved_ids.append(ann_id)
        else:
            rejected_ids.append(ann_id)

    # Salva relatório
    report_path = output_dir / "review_report.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False))

    # Salva lista de aprovados para o dashboard
    approved_path = output_dir / "approved.json"
    approved_path.write_text(json.dumps(approved_ids, indent=2))

    print(f"\n--- REVISÃO CONCLUÍDA ---")
    print(f"Aprovados ({len(approved_ids)}): {approved_ids}")
    print(f"Revisão humana necessária ({len(rejected_ids)}): {rejected_ids}")
    print(f"Relatório salvo em: {report_path}")

    return report


if __name__ == "__main__":
    output_dir = Path("output/skdrexel/2026-05-06")
    asyncio.run(run(output_dir=output_dir, parish_id="skdrexel"))
