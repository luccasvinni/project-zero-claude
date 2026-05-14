import asyncio
import io
import json
import os
import shutil
import sys
import uuid
import webbrowser
from pathlib import Path

import anthropic
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).parent.parent.parent
OUTPUT_BASE = ROOT / "output"
CONFIG_BASE = ROOT / "config" / "parishes"
STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI()
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# --- Workflow state ---
_workflow_jobs: dict[str, dict] = {}

# --- Image edit jobs ---
_image_jobs: dict[str, dict] = {}

# --- Regen jobs (individual announcement regeneration) ---
_regen_jobs: dict[str, dict] = {}


def _get_prompt_mode(parish_id: str) -> str:
    config = _load_parish_yaml(parish_id)
    return config.get("image_generation", {}).get("prompt_mode", "python")


async def _run_image_edit(job_id: str, parish_id: str, date: str, ann_id: str, prompt: str):
    job = _image_jobs[job_id]

    def step(name: str, detail: str):
        job["step"] = name
        job["detail"] = detail

    try:
        sys.path.insert(0, str(Path(__file__).parent.parent / "agents"))
        from agent4a_image import (
            locate_announcements, crop_announcement,
            extract_announcement_json, build_prompt_from_json, build_prompt_with_claude,
            generate_image_with_gemini, resize_to_canvas, OUTPUT_SIZE,
            load_parish_logo, composite_logo,
        )
        from google import genai as _genai

        run_dir = OUTPUT_BASE / parish_id / date
        announcements = json.loads((run_dir / "announcements.json").read_text())
        ann = next((a for a in announcements if str(a.get("id")) == ann_id), None)
        if not ann:
            raise ValueError("Anúncio não encontrado")

        anthropic_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        gemini_client = _genai.Client(api_key=os.environ["GOOGLE_API_KEY"])
        pages_dir = run_dir / "pages"

        # Step 1 — locate (use cache if available)
        step("locating", "Localizando anúncio na página...")
        locations_cache = run_dir / "locations.json"
        if locations_cache.exists():
            locations = json.loads(locations_cache.read_text())
            location = locations.get(ann_id)
        else:
            locations = locate_announcements(anthropic_client, pages_dir, [ann])
            location = locations.get(ann_id)

        if not location:
            raise ValueError("Não foi possível localizar o anúncio na página")

        # Step 2 — extract JSON + build prompt + generate
        is_manual = location.get("manual", False)
        step("generating", "Extraindo dados da imagem...")
        crop = crop_announcement(pages_dir, location, padding=0 if is_manual else 12)
        ann_json = extract_announcement_json(anthropic_client, crop)
        ann_json["_edit_request"] = prompt
        if is_manual:
            ann_json["_strict_crop"] = True
        _pmode = _get_prompt_mode(parish_id)
        if _pmode == "claude":
            final_prompt = build_prompt_with_claude(anthropic_client, ann_json, crop)
        else:
            final_prompt = build_prompt_from_json(ann_json)

        step("generating", "Gerando nova imagem com IA...")
        generated = generate_image_with_gemini(gemini_client, crop, final_prompt)
        if generated is None:
            generated = crop

        # Step 3 — save
        step("saving", "Salvando imagem...")
        final_img = resize_to_canvas(generated, OUTPUT_SIZE)
        parish_logo = load_parish_logo(parish_id)
        if parish_logo:
            final_img = composite_logo(final_img, parish_logo)
        out_path = run_dir / "images" / f"announcement_{ann_id.zfill(2)}.png"
        final_img.save(str(out_path), "PNG")

        job["status"] = "done"
        job["step"] = "done"
        job["detail"] = "Imagem gerada com sucesso."

    except Exception as exc:
        job["status"] = "error"
        job["detail"] = str(exc)


async def _run_regen_image(job_id: str, parish_id: str, date: str, ann_id: str, instruction: str = ""):
    job = _regen_jobs[job_id]

    def step(name: str, detail: str):
        job["step"] = name
        job["detail"] = detail

    try:
        sys.path.insert(0, str(Path(__file__).parent.parent / "agents"))
        from agent4a_image import (
            locate_announcements, crop_announcement,
            extract_announcement_json, build_prompt_from_json, build_prompt_with_claude,
            generate_image_with_gemini, resize_to_canvas, OUTPUT_SIZE,
            load_parish_logo, composite_logo,
        )
        from google import genai as _genai

        run_dir = OUTPUT_BASE / parish_id / date
        announcements = json.loads((run_dir / "announcements.json").read_text())
        ann = next((a for a in announcements if str(a.get("id")) == ann_id), None)
        if not ann:
            raise ValueError("Anúncio não encontrado")

        anthropic_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        gemini_client = _genai.Client(api_key=os.environ["GOOGLE_API_KEY"])
        pages_dir = run_dir / "pages"

        step("locating", "Localizando anúncio na página...")
        locations_cache = run_dir / "locations.json"
        if locations_cache.exists():
            locations = json.loads(locations_cache.read_text())
            location = locations.get(ann_id)
        else:
            locations = locate_announcements(anthropic_client, pages_dir, [ann])
            location = locations.get(ann_id)

        if not location:
            raise ValueError("Não foi possível localizar o anúncio")

        is_manual = location.get("manual", False)
        step("generating", "Extraindo dados da imagem...")
        crop = crop_announcement(pages_dir, location, padding=0 if is_manual else 12)
        ann_json = extract_announcement_json(anthropic_client, crop)
        if instruction:
            ann_json["_edit_request"] = instruction
        if is_manual:
            ann_json["_strict_crop"] = True
        _pmode = _get_prompt_mode(parish_id)
        if _pmode == "claude":
            final_prompt = build_prompt_with_claude(anthropic_client, ann_json, crop)
        else:
            final_prompt = build_prompt_from_json(ann_json)

        step("generating", "Gerando nova imagem com IA...")
        generated = generate_image_with_gemini(gemini_client, crop, final_prompt)
        if generated is None:
            generated = crop

        step("saving", "Salvando imagem...")
        final_img = resize_to_canvas(generated, OUTPUT_SIZE)
        parish_logo = load_parish_logo(parish_id)
        if parish_logo:
            final_img = composite_logo(final_img, parish_logo)
        out_path = run_dir / "images" / f"announcement_{ann_id.zfill(2)}.png"
        backup_path = run_dir / "images" / f"announcement_{ann_id.zfill(2)}_backup.png"
        had_existing = out_path.exists()
        if had_existing:
            shutil.copy2(str(out_path), str(backup_path))
        final_img.save(str(out_path), "PNG")

        job["status"] = "done"
        job["step"] = "done"
        job["detail"] = "Imagem regenerada com sucesso."
        job["has_backup"] = had_existing

    except Exception as exc:
        job["status"] = "error"
        job["detail"] = str(exc)


async def _run_regen_content(job_id: str, parish_id: str, date: str, ann_id: str, instruction: str = "", use_crop: bool = False):
    job = _regen_jobs[job_id]

    def step(name: str, detail: str):
        job["step"] = name
        job["detail"] = detail

    try:
        sys.path.insert(0, str(Path(__file__).parent.parent / "agents"))
        import agent4b_html as _a4b

        run_dir = OUTPUT_BASE / parish_id / date
        announcements = json.loads((run_dir / "announcements.json").read_text())
        ann = next((a for a in announcements if str(a.get("id")) == ann_id), None)
        if not ann:
            raise ValueError("Anúncio não encontrado")

        step("generating", "Gerando conteúdo HTML...")
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        system_prompt = _a4b.load_system_prompt(parish_id)
        if instruction:
            system_prompt += f"\n\n---\n\nINSTRUÇÃO PONTUAL PARA ESTE ANÚNCIO (aplicar apenas nesta geração):\n- {instruction}"
        qr_urls = _a4b.detect_qr_codes(run_dir / "pages")

        crop_image = None
        if use_crop:
            try:
                from agent4a_image import crop_announcement as _crop_ann
                locations_path = run_dir / "locations.json"
                if locations_path.exists():
                    locations = json.loads(locations_path.read_text())
                    location = locations.get(ann_id)
                    if location:
                        crop_image = _crop_ann(run_dir / "pages", location)
            except Exception as _e:
                print(f"  Warning: could not load crop image for HTML agent: {_e}")

        html = await _a4b.generate_html_for_announcement(client, ann, system_prompt, qr_urls, crop_image=crop_image)

        step("saving", "Salvando conteúdo...")
        html_dir = run_dir / "html"
        html_dir.mkdir(exist_ok=True)
        filename = f"announcement_{ann_id.zfill(2)}.html"
        html_path = html_dir / filename
        had_existing = html_path.exists()
        if had_existing and use_crop:
            backup_path = html_dir / f"announcement_{ann_id.zfill(2)}_backup.html"
            shutil.copy2(str(html_path), str(backup_path))
        html_path.write_text(html, encoding="utf-8")

        job["status"] = "done"
        job["step"] = "done"
        job["detail"] = "Conteúdo regenerado com sucesso."
        job["html"] = html
        job["has_html_backup"] = had_existing and use_crop

    except Exception as exc:
        job["status"] = "error"
        job["detail"] = str(exc)


def _load_parish_yaml(parish_id: str) -> dict:
    import yaml
    config_path = CONFIG_BASE / f"{parish_id}.yaml"
    if config_path.exists():
        return yaml.safe_load(config_path.read_text()) or {}
    return {}


def _run_in_thread(coro_factory):
    """Run an async coroutine in a fresh thread with its own event loop."""
    import asyncio as _aio
    loop = _aio.new_event_loop()
    try:
        return loop.run_until_complete(coro_factory())
    finally:
        loop.close()


async def _run_workflow(job_id: str, parish_id: str, mode: str = "complete", reader_instruction: str = "", bulletin_url: str = ""):
    """mode: 'complete' | 'images' | 'content'"""
    job = _workflow_jobs[job_id]

    def step(name: str, detail: str = ""):
        job["step"] = name
        job["detail"] = detail

    try:
        sys.path.insert(0, str(Path(__file__).parent.parent / "agents"))
        _pid = parish_id

        if mode == "complete":
            # Step 1 — scraper
            step("scraper", "Baixando boletim...")
            config = _load_parish_yaml(parish_id)
            direct_pdf = config.get("scraper", {}).get("direct_pdf", False)

            import agent1_scraper as _a1

            if direct_pdf:
                if not bulletin_url:
                    raise ValueError("Esta paróquia requer o link direto do boletim PDF.")
                _burl = bulletin_url
                pdf_path = await asyncio.to_thread(
                    _run_in_thread, lambda: _a1.download_pdf(_burl, _pid)
                )
            else:
                page_url = config.get("parish", {}).get("bulletin_archive_url", "")
                if not page_url:
                    raise ValueError(f"bulletin_archive_url não configurado para {parish_id}")
                _purl = page_url
                pdf_path = await asyncio.to_thread(
                    _run_in_thread, lambda: _a1.run(page_url=_purl, parish_id=_pid)
                )

            if not pdf_path:
                raise RuntimeError("Falha ao baixar o boletim PDF")
            output_dir = pdf_path.parent
            job["output_dir"] = str(output_dir)
            job["date"] = output_dir.name

            # Step 2 — reader
            step("reader", "Lendo boletim e extraindo informações...")
            import agent2_reader as _a2
            _rinstr = reader_instruction
            await asyncio.to_thread(
                _run_in_thread, lambda: _a2.run(pdf_path=pdf_path, parish_id=_pid, instruction=_rinstr)
            )
        else:
            # Usa o run mais recente já existente para a paróquia
            parish_dir = OUTPUT_BASE / parish_id
            existing_runs = sorted(
                [d for d in parish_dir.iterdir() if d.is_dir() and (d / "announcements.json").exists()],
                reverse=True,
            )
            if not existing_runs:
                raise RuntimeError(f"Nenhum run encontrado para {parish_id}. Execute o modo completo primeiro.")
            output_dir = existing_runs[0]
            job["output_dir"] = str(output_dir)
            job["date"] = output_dir.name

        # Step 3 — preparing
        step("preparing", "Preparando geração de conteúdo...")

        # Zera revisões e ratings antes de gerar novo conteúdo (seletivo por modo)
        if mode == "complete":
            for _fname in ("review_report.json", "approved.json", "ratings.json"):
                _fpath = output_dir / _fname
                if _fpath.exists():
                    _fpath.unlink()
        else:
            _reset_image = (mode == "images")
            _reset_html  = (mode == "content")

            # Ratings — zera apenas o campo correspondente ao modo
            _rp = output_dir / "ratings.json"
            if _rp.exists():
                _rt = json.loads(_rp.read_text())
                for _k in _rt:
                    if _reset_image: _rt[_k]["image"] = 0
                    if _reset_html:  _rt[_k]["html"]  = 0
                _rp.write_text(json.dumps(_rt, indent=2, ensure_ascii=False))

            # Review report — zera apenas a seção correspondente
            _rep_path = output_dir / "review_report.json"
            if _rep_path.exists():
                _rep = json.loads(_rep_path.read_text())
                for _k in _rep:
                    _rv = _rep[_k].get("review") or {}
                    if _reset_image:
                        _rv["image"] = {"approved": None, "issues": []}
                    if _reset_html:
                        _rv["html"] = {"approved": None, "issues": [], "spelling_errors": []}
                    _rv["overall_approved"] = False
                    _rep[_k]["review"] = _rv
                    _rep[_k]["status"] = "needs_review"
                _rep_path.write_text(json.dumps(_rep, indent=2, ensure_ascii=False))

            # Approved — limpa pois overall_approved foi resetado
            _ap = output_dir / "approved.json"
            _ap.write_text(json.dumps([], indent=2, ensure_ascii=False))

        # Step 4 — generation (varies by mode)
        _odir = output_dir
        _pmode = _get_prompt_mode(parish_id)
        import agent4a_image as _a4a, agent4b_html as _a4b

        if mode == "images":
            step("generation", "Gerando imagens...")
            await asyncio.to_thread(_run_in_thread, lambda: _a4a.run(output_dir=_odir, parish_id=_pid, prompt_mode=_pmode))
        elif mode == "content":
            step("generation", "Gerando conteúdo HTML...")
            await asyncio.to_thread(_run_in_thread, lambda: _a4b.run(output_dir=_odir, parish_id=_pid))
        else:  # complete
            step("generation", "Gerando imagens e conteúdo...")
            await asyncio.gather(
                asyncio.to_thread(_run_in_thread, lambda: _a4a.run(output_dir=_odir, parish_id=_pid, prompt_mode=_pmode)),
                asyncio.to_thread(_run_in_thread, lambda: _a4b.run(output_dir=_odir, parish_id=_pid)),
            )

        # Step 5 — reviewer (complete only)
        if mode == "complete":
            step("reviewer", "Revisando qualidade...")
            import agent5_reviewer as _a5
            await asyncio.to_thread(
                _run_in_thread, lambda: _a5.run(output_dir=_odir, parish_id=_pid)
            )

        job["status"] = "done"
        job["step"] = "done"
        job["detail"] = "Workflow concluído com sucesso."

    except Exception as exc:
        job["status"] = "error"
        job["detail"] = str(exc)


# --- Helpers ---

def get_run_dir(parish_id: str, date: str) -> Path:
    p = OUTPUT_BASE / parish_id / date
    if not p.exists():
        raise HTTPException(status_code=404, detail="Run not found")
    return p


def load_parish_css(parish_id: str) -> str:
    css_path = CONFIG_BASE / parish_id / "newspage_rules.css"
    return css_path.read_text() if css_path.exists() else ""


# --- API routes ---

@app.get("/api/runs")
def list_runs():
    runs = []
    for parish_dir in sorted(OUTPUT_BASE.iterdir()):
        if not parish_dir.is_dir():
            continue
        for date_dir in sorted(parish_dir.iterdir(), reverse=True):
            if (date_dir / "announcements.json").exists():
                runs.append({"parish_id": parish_dir.name, "date": date_dir.name})
    return runs


@app.get("/api/run/{parish_id}/{date}")
def get_run(parish_id: str, date: str):
    run_dir = get_run_dir(parish_id, date)
    announcements = json.loads((run_dir / "announcements.json").read_text())
    report = {}
    if (run_dir / "review_report.json").exists():
        report = json.loads((run_dir / "review_report.json").read_text())
    approved = json.loads((run_dir / "approved.json").read_text()) if (run_dir / "approved.json").exists() else []

    ratings = {}
    if (run_dir / "ratings.json").exists():
        ratings = json.loads((run_dir / "ratings.json").read_text())

    locations = {}
    if (run_dir / "locations.json").exists():
        locations = json.loads((run_dir / "locations.json").read_text())

    for ann in announcements:
        ann_id = str(ann.get("id"))
        img_path = run_dir / "images" / f"announcement_{ann_id.zfill(2)}.png"
        html_path = run_dir / "html" / f"announcement_{ann_id.zfill(2)}.html"
        ann["has_image"] = img_path.exists()
        ann["has_html"] = html_path.exists()
        ann["has_source"] = ann_id in locations
        ann["html_content"] = html_path.read_text(encoding="utf-8") if html_path.exists() else ""
        ann["review"] = report.get(ann_id, {})
        ann["status"] = report.get(ann_id, {}).get("status", "pending")
        ann["rating"] = ratings.get(ann_id, {"image": 0, "html": 0})

    return {"announcements": announcements, "approved": approved, "parish_id": parish_id, "date": date}


@app.get("/api/image/{parish_id}/{date}/{ann_id}")
def get_image(parish_id: str, date: str, ann_id: str):
    run_dir = get_run_dir(parish_id, date)
    img_path = run_dir / "images" / f"announcement_{ann_id.zfill(2)}.png"
    if not img_path.exists():
        raise HTTPException(status_code=404, detail="Image not found")
    return FileResponse(str(img_path), media_type="image/png")


@app.get("/api/image-backup/{parish_id}/{date}/{ann_id}")
def get_image_backup(parish_id: str, date: str, ann_id: str):
    run_dir = get_run_dir(parish_id, date)
    backup_path = run_dir / "images" / f"announcement_{ann_id.zfill(2)}_backup.png"
    if not backup_path.exists():
        raise HTTPException(status_code=404, detail="No backup")
    return FileResponse(str(backup_path), media_type="image/png")


@app.post("/api/restore-image/{parish_id}/{date}/{ann_id}")
def restore_image(parish_id: str, date: str, ann_id: str):
    run_dir = get_run_dir(parish_id, date)
    backup_path = run_dir / "images" / f"announcement_{ann_id.zfill(2)}_backup.png"
    out_path = run_dir / "images" / f"announcement_{ann_id.zfill(2)}.png"
    if not backup_path.exists():
        raise HTTPException(status_code=404, detail="No backup to restore")
    shutil.copy2(str(backup_path), str(out_path))
    backup_path.unlink()
    return {"ok": True}


@app.delete("/api/image-backup/{parish_id}/{date}/{ann_id}")
def delete_image_backup(parish_id: str, date: str, ann_id: str):
    run_dir = get_run_dir(parish_id, date)
    backup_path = run_dir / "images" / f"announcement_{ann_id.zfill(2)}_backup.png"
    if backup_path.exists():
        backup_path.unlink()
    return {"ok": True}


@app.get("/api/html-backup/{parish_id}/{date}/{ann_id}")
def get_html_backup(parish_id: str, date: str, ann_id: str):
    run_dir = get_run_dir(parish_id, date)
    backup_path = run_dir / "html" / f"announcement_{ann_id.zfill(2)}_backup.html"
    if not backup_path.exists():
        raise HTTPException(status_code=404, detail="No HTML backup")
    return {"html": backup_path.read_text(encoding="utf-8")}


@app.post("/api/restore-html/{parish_id}/{date}/{ann_id}")
def restore_html(parish_id: str, date: str, ann_id: str):
    run_dir = get_run_dir(parish_id, date)
    backup_path = run_dir / "html" / f"announcement_{ann_id.zfill(2)}_backup.html"
    out_path = run_dir / "html" / f"announcement_{ann_id.zfill(2)}.html"
    if not backup_path.exists():
        raise HTTPException(status_code=404, detail="No HTML backup to restore")
    shutil.copy2(str(backup_path), str(out_path))
    backup_path.unlink()
    return {"html": out_path.read_text(encoding="utf-8")}


@app.delete("/api/html-backup/{parish_id}/{date}/{ann_id}")
def delete_html_backup(parish_id: str, date: str, ann_id: str):
    run_dir = get_run_dir(parish_id, date)
    backup_path = run_dir / "html" / f"announcement_{ann_id.zfill(2)}_backup.html"
    if backup_path.exists():
        backup_path.unlink()
    return {"ok": True}


@app.get("/api/source-crop/{parish_id}/{date}/{ann_id}")
def get_source_crop(parish_id: str, date: str, ann_id: str):
    run_dir = get_run_dir(parish_id, date)
    locations_path = run_dir / "locations.json"
    if not locations_path.exists():
        raise HTTPException(status_code=404, detail="No source locations available")
    locations = json.loads(locations_path.read_text())
    location = locations.get(ann_id)
    if not location:
        raise HTTPException(status_code=404, detail="Source location not found for this announcement")

    sys.path.insert(0, str(Path(__file__).parent.parent / "agents"))
    from agent4a_image import crop_announcement

    crop = crop_announcement(run_dir / "pages", location)
    buf = io.BytesIO()
    crop.save(buf, format="PNG")
    buf.seek(0)
    return StreamingResponse(buf, media_type="image/png", headers={"Cache-Control": "no-cache"})


@app.get("/api/bulletin-page/{parish_id}/{date}/{ann_id}")
def get_bulletin_page(parish_id: str, date: str, ann_id: str):
    run_dir = get_run_dir(parish_id, date)
    locations_path = run_dir / "locations.json"
    if not locations_path.exists():
        raise HTTPException(status_code=404, detail="No locations available")
    locations = json.loads(locations_path.read_text())
    location = locations.get(ann_id)
    if not location:
        raise HTTPException(status_code=404, detail="Location not found")

    page_num = location["page"]
    page_path = run_dir / "pages" / f"page_{page_num:02d}.png"
    if not page_path.exists():
        raise HTTPException(status_code=404, detail="Page image not found")
    return FileResponse(str(page_path), media_type="image/png")


@app.get("/api/location/{parish_id}/{date}/{ann_id}")
def get_location(parish_id: str, date: str, ann_id: str):
    run_dir = get_run_dir(parish_id, date)
    locations_path = run_dir / "locations.json"
    if not locations_path.exists():
        raise HTTPException(status_code=404, detail="No locations")
    locations = json.loads(locations_path.read_text())
    loc = locations.get(ann_id)
    if not loc:
        raise HTTPException(status_code=404, detail="Not found")
    return {"page": loc["page"], "top": loc["top"], "left": loc["left"], "bottom": loc["bottom"], "right": loc["right"]}


class ManualCropRequest(BaseModel):
    page: int
    top: float
    left: float
    bottom: float
    right: float


@app.post("/api/manual-crop/{parish_id}/{date}/{ann_id}")
def save_manual_crop(parish_id: str, date: str, ann_id: str, body: ManualCropRequest):
    run_dir = get_run_dir(parish_id, date)
    locations_path = run_dir / "locations.json"
    locations = json.loads(locations_path.read_text()) if locations_path.exists() else {}
    locations[ann_id] = {
        "id": ann_id,
        "page": body.page,
        "top": round(body.top, 4),
        "left": round(body.left, 4),
        "bottom": round(body.bottom, 4),
        "right": round(body.right, 4),
        "manual": True,
    }
    locations_path.write_text(json.dumps(locations, indent=2, ensure_ascii=False))
    return {"ok": True}


@app.delete("/api/manual-crop/{parish_id}/{date}/{ann_id}")
def delete_manual_crop(parish_id: str, date: str, ann_id: str):
    run_dir = get_run_dir(parish_id, date)
    locations_path = run_dir / "locations.json"
    if not locations_path.exists():
        return {"ok": True}
    locations = json.loads(locations_path.read_text())
    if ann_id in locations and locations[ann_id].get("manual"):
        del locations[ann_id]
        locations_path.write_text(json.dumps(locations, indent=2, ensure_ascii=False))
    return {"ok": True}


@app.get("/api/css/{parish_id}")
def get_css(parish_id: str):
    css = load_parish_css(parish_id)
    return {"css": css}


class HtmlUpdateRequest(BaseModel):
    html: str


@app.post("/api/edit/html/{parish_id}/{date}/{ann_id}")
def save_html(parish_id: str, date: str, ann_id: str, body: HtmlUpdateRequest):
    run_dir = get_run_dir(parish_id, date)
    html_path = run_dir / "html" / f"announcement_{ann_id.zfill(2)}.html"
    html_path.write_text(body.html, encoding="utf-8")
    return {"ok": True}


class SuggestCorrectionsRequest(BaseModel):
    html: str
    spelling_errors: list[str]
    html_issues: list[str] = []


@app.post("/api/suggest-corrections/{parish_id}/{date}/{ann_id}")
async def suggest_corrections(parish_id: str, date: str, ann_id: str, body: SuggestCorrectionsRequest):
    sys.path.insert(0, str(Path(__file__).parent.parent / "agents"))
    from agent5_reviewer import CORRECTION_SYSTEM_PROMPT

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    if not body.spelling_errors and not body.html_issues:
        return {"suggestions": []}

    if body.spelling_errors:
        issues_text = "\n".join(f"- {i}" for i in body.html_issues) if body.html_issues else ""
        user_msg = (
            f"Erros ortográficos identificados (palavras ou trechos exatos): {', '.join(body.spelling_errors)}\n"
            + (f"\nProblemas adicionais:\n{issues_text}\n" if issues_text else "")
            + f"\nLocalize cada item acima no HTML abaixo e sugira a correção. "
            f"Use o trecho EXATO como aparece no HTML no campo 'original'.\n\n"
            f"HTML:\n{body.html[:4000]}"
        )
    else:
        issues_text = "\n".join(f"- {i}" for i in body.html_issues)
        user_msg = (
            f"O revisor identificou os seguintes problemas no HTML:\n{issues_text}\n\n"
            f"Para cada problema, localize no HTML abaixo o trecho EXATO que precisa ser corrigido "
            f"(pode ser uma frase inteira, não apenas uma palavra). "
            f"Sugira o texto substituto correto. Não quebre frases em palavras isoladas.\n\n"
            f"HTML:\n{body.html[:4000]}"
        )

    response = await asyncio.to_thread(
        client.messages.create,
        model="claude-opus-4-5",
        max_tokens=1024,
        system=CORRECTION_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    )
    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    return {"suggestions": json.loads(raw)}


class ImageEditRequest(BaseModel):
    prompt: str


@app.post("/api/edit/image/{parish_id}/{date}/{ann_id}")
async def edit_image(parish_id: str, date: str, ann_id: str, body: ImageEditRequest):
    get_run_dir(parish_id, date)  # validates run exists
    job_id = str(uuid.uuid4())
    _image_jobs[job_id] = {
        "status": "running", "step": "locating",
        "detail": "Iniciando...", "ann_id": ann_id,
    }
    asyncio.create_task(_run_image_edit(job_id, parish_id, date, ann_id, body.prompt))
    return {"job_id": job_id}


@app.get("/api/edit/image/status/{job_id}")
def image_edit_status(job_id: str):
    job = _image_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


class FeedbackRequest(BaseModel):
    type: str        # "image" | "content"
    rating: int
    instruction: str


@app.post("/api/feedback/{parish_id}/{date}/{ann_id}")
async def save_feedback(parish_id: str, date: str, ann_id: str, body: FeedbackRequest):
    from datetime import datetime as _dt
    run_dir = get_run_dir(parish_id, date)

    ratings_path = run_dir / "ratings.json"
    ratings = json.loads(ratings_path.read_text()) if ratings_path.exists() else {}
    existing = ratings.get(ann_id, {"image": 0, "html": 0})
    if body.type == "image":
        existing["image"] = body.rating
    else:
        existing["html"] = body.rating
    ratings[ann_id] = existing
    ratings_path.write_text(json.dumps(ratings, indent=2, ensure_ascii=False))

    if body.instruction.strip():
        filename = "image_feedback.md" if body.type == "image" else "html_feedback.md"
        feedback_dir = CONFIG_BASE / parish_id / "agent_feedback"
        feedback_dir.mkdir(exist_ok=True)
        path = feedback_dir / filename
        timestamp = _dt.now().strftime("%Y-%m-%d %H:%M")
        header = "# Feedback acumulado\n" if not path.exists() else path.read_text()
        entry = f"\n\n## Instrução manual — {timestamp} (anúncio #{ann_id})\n\n- {body.instruction.strip()}"
        path.write_text(header + entry)

    return {"ok": True}


class RegenRequest(BaseModel):
    instruction: str = ""
    use_crop: bool = False


@app.post("/api/regen/image/{parish_id}/{date}/{ann_id}")
async def regen_image_endpoint(parish_id: str, date: str, ann_id: str, body: RegenRequest = RegenRequest()):
    get_run_dir(parish_id, date)
    job_id = str(uuid.uuid4())
    _regen_jobs[job_id] = {"status": "running", "step": "locating", "detail": "Iniciando..."}
    asyncio.create_task(_run_regen_image(job_id, parish_id, date, ann_id, body.instruction))
    return {"job_id": job_id}


@app.post("/api/regen/content/{parish_id}/{date}/{ann_id}")
async def regen_content_endpoint(parish_id: str, date: str, ann_id: str, body: RegenRequest = RegenRequest()):
    get_run_dir(parish_id, date)
    job_id = str(uuid.uuid4())
    _regen_jobs[job_id] = {"status": "running", "step": "generating", "detail": "Iniciando..."}
    asyncio.create_task(_run_regen_content(job_id, parish_id, date, ann_id, body.instruction, body.use_crop))
    return {"job_id": job_id}


@app.get("/api/regen/status/{job_id}")
def regen_status(job_id: str):
    job = _regen_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.post("/api/finalize/{parish_id}/{date}")
async def finalize_run(parish_id: str, date: str):
    from datetime import datetime as _dt
    run_dir = get_run_dir(parish_id, date)

    announcements = json.loads((run_dir / "announcements.json").read_text())
    ratings = json.loads((run_dir / "ratings.json").read_text()) if (run_dir / "ratings.json").exists() else {}
    report = json.loads((run_dir / "review_report.json").read_text()) if (run_dir / "review_report.json").exists() else {}

    summaries = []
    for ann in announcements:
        ann_id = str(ann.get("id"))
        r = ratings.get(ann_id, {"image": 0, "html": 0})
        rev = report.get(ann_id, {}).get("review", {})
        img_issues  = rev.get("image", {}).get("issues", [])
        html_issues = rev.get("html", {}).get("issues", []) + rev.get("html", {}).get("spelling_errors", [])
        summaries.append(
            f'- [{ann_id}] "{ann.get("title","")}" | '
            f'Imagem {r.get("image",0)}/5 | HTML {r.get("html",0)}/5 | '
            f'Problemas imagem: {img_issues or "nenhum"} | '
            f'Problemas HTML: {html_issues or "nenhum"}'
        )
    context = "\n".join(summaries)

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    def ask(focus: str) -> str:
        prompt = (
            f"Você é um sistema de aprendizado para agentes de IA que geram {focus} de anúncios paroquiais católicos.\n\n"
            f"Analise as avaliações abaixo (0–5 estrelas) e os problemas encontrados pelo revisor:\n\n"
            f"{context}\n\n"
            f"Escreva de 3 a 6 instruções ESPECÍFICAS e ACIONÁVEIS para o agente de {focus} melhorar nos próximos runs. "
            f"Baseie-se nos padrões: o que gerou notas altas vs notas baixas. "
            f"Escreva em português brasileiro. Formato: lista com marcadores (-)."
        )
        resp = client.messages.create(
            model="claude-opus-4-5", max_tokens=800,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text.strip()

    image_feedback = ask("imagens (flyers para redes sociais)")
    html_feedback  = ask("conteúdo HTML")

    feedback_dir = CONFIG_BASE / parish_id / "agent_feedback"
    feedback_dir.mkdir(exist_ok=True)
    timestamp = _dt.now().strftime("%Y-%m-%d %H:%M")
    separator = f"\n\n## Run {date} — {timestamp}\n\n"

    for filename, content in [("image_feedback.md", image_feedback), ("html_feedback.md", html_feedback)]:
        path = feedback_dir / filename
        header = "# Feedback acumulado\n" if not path.exists() else path.read_text()
        path.write_text(header + separator + content)

    return {"ok": True, "image_feedback": image_feedback, "html_feedback": html_feedback}


class RatingRequest(BaseModel):
    image_rating: int
    html_rating: int


@app.post("/api/rate/{parish_id}/{date}/{ann_id}")
def save_rating(parish_id: str, date: str, ann_id: str, body: RatingRequest):
    run_dir = get_run_dir(parish_id, date)
    ratings_path = run_dir / "ratings.json"
    ratings = json.loads(ratings_path.read_text()) if ratings_path.exists() else {}
    ratings[ann_id] = {"image": body.image_rating, "html": body.html_rating}
    ratings_path.write_text(json.dumps(ratings, indent=2, ensure_ascii=False))
    return {"ok": True}


class WorkflowStartRequest(BaseModel):
    parish_id: str
    mode: str = "complete"  # "complete" | "images" | "content"
    reader_instruction: str = ""
    bulletin_url: str = ""


@app.post("/api/workflow/start")
async def workflow_start(body: WorkflowStartRequest):
    job_id = str(uuid.uuid4())
    _workflow_jobs[job_id] = {
        "status": "running",
        "step": "starting",
        "detail": "Iniciando...",
        "parish_id": body.parish_id,
        "mode": body.mode,
        "output_dir": None,
        "date": None,
    }
    asyncio.create_task(_run_workflow(job_id, body.parish_id, body.mode, body.reader_instruction, body.bulletin_url))
    return {"job_id": job_id}


@app.get("/api/workflow/status/{job_id}")
def workflow_status(job_id: str):
    job = _workflow_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.post("/api/review/{parish_id}/{date}/{ann_id}")
async def review_single(parish_id: str, date: str, ann_id: str):
    sys.path.insert(0, str(Path(__file__).parent.parent / "agents"))

    run_dir = get_run_dir(parish_id, date)
    announcements = json.loads((run_dir / "announcements.json").read_text())
    ann = next((a for a in announcements if str(a.get("id")) == ann_id), None)
    if not ann:
        raise HTTPException(status_code=404, detail="Anúncio não encontrado")

    try:
        from agent5_reviewer import review_announcement

        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        img_path = run_dir / "images" / f"announcement_{ann_id.zfill(2)}.png"
        html_path = run_dir / "html" / f"announcement_{ann_id.zfill(2)}.html"

        source_crop = None
        locations_cache = run_dir / "locations.json"
        if locations_cache.exists():
            try:
                from agent4a_image import crop_announcement as _crop_ann
                locations = json.loads(locations_cache.read_text())
                location = locations.get(ann_id)
                if location and (run_dir / "pages").exists():
                    source_crop = _crop_ann(run_dir / "pages", location)
            except Exception:
                pass

        review = await asyncio.to_thread(
            review_announcement, client, ann, img_path, html_path, source_crop
        )
        status = "approved" if review["overall_approved"] else "needs_review"

        report_path = run_dir / "review_report.json"
        report = json.loads(report_path.read_text()) if report_path.exists() else {}
        existing = report.get(ann_id, {})
        report[ann_id] = {
            "title": ann.get("title", ""),
            "status": status,
            "attempts": existing.get("attempts", 0) + 1,
            "review": review,
        }
        report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False))

        return {"review": review, "status": status}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/parishes")
def list_parishes():
    return sorted(p.stem for p in CONFIG_BASE.glob("*.yaml"))


@app.get("/api/parish-config/{parish_id}")
def get_parish_config(parish_id: str):
    config = _load_parish_yaml(parish_id)
    return {
        "direct_pdf": config.get("scraper", {}).get("direct_pdf", False),
        "name": config.get("parish", {}).get("name", parish_id),
    }


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return STATIC_DIR.joinpath("index.html").read_text(encoding="utf-8")


if __name__ == "__main__":
    webbrowser.open("http://localhost:8502")
    uvicorn.run("app:app", host="0.0.0.0", port=8502, reload=False)
