"""Modal deployment: `modal deploy modal_app.py`.

Needs Modal secrets: `supabase` (SUPABASE_URL, SUPABASE_KEY).
"""
import modal

image = (
    modal.Image.debian_slim()
    .pip_install("fastapi[standard]", "httpx", "supabase")
    .add_local_python_source("scraper", "storage", "web", "fetch", "htmlparse", "signals", "jobs", "eventpage")
)
app = modal.App("signals", image=image)
supabase = modal.Secret.from_name("supabase")


@app.function(secrets=[supabase], timeout=300)
def scrape_and_save(url: str, batch_id: str | None = None, row_id: int | None = None) -> dict:
    from jobs import run_job
    from scraper import scrape
    from storage import save_to_supabase

    return run_job(scrape, save_to_supabase, url, batch_id, row_id)


@app.function(secrets=[supabase])
@modal.asgi_app(label="signals")
def web():
    from scraper import scrape
    from storage import get_batch, queue_batch, save_to_supabase
    from web import create_app

    def spawn(batch_id, jobs):
        for row_id, url in jobs:
            scrape_and_save.spawn(url, batch_id, row_id)

    return create_app(
        scrape,
        save_to_supabase,
        spawn=spawn,
        queue_batch=queue_batch,
        get_batch=get_batch,
    )
