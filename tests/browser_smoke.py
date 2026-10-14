"""Optional local browser test. No real accounts, credentials or Codex calls."""
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from codex_tasks.store import Store
from codex_tasks.server import Coordinator
from playwright.sync_api import sync_playwright


def main():
    with tempfile.TemporaryDirectory() as temporary:
        server = Coordinator(("127.0.0.1", 0), Store(Path(temporary) / "state.sqlite"), "test-administrator-token-01234567890123456789")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(executable_path="/usr/bin/chromium", headless=True)
                page = browser.new_page(viewport={"width": 1280, "height": 1000})
                errors = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.goto(f"http://127.0.0.1:{server.server_port}")
                page.fill("#admin-token", "test-administrator-token-01234567890123456789")
                page.click("#login-form button")
                page.wait_for_selector("#dashboard", state="visible")
                page.locator("details.card > summary").click()
                page.fill("#project-id", "example")
                page.fill("#project-remote", "https://github.com/example/private-project.git")
                page.click("#project-form button")
                page.wait_for_selector("#task-project option[value=example]", state="attached")
                page.locator("summary", has_text="Cadastrar um agente").click()
                page.fill("#worker-id", "pro-1")
                page.fill("#worker-label", "Agente de demonstração")
                page.check("#worker-projects input[value=example]")
                page.click("#worker-form button")
                page.wait_for_selector("#worker-secret", state="visible")
                assert '"worker_id": "pro-1"' in page.input_value("#worker-config")
                page.evaluate("document.getElementById('worker-secret').hidden=true;document.getElementById('worker-config').value='';")
                page.fill("#task-title", "Organizar a próxima atualização")
                page.fill("#task-prompt", "<script>window.UNSAFE=true</script> Implementar e verificar a melhoria.")
                page.select_option("#task-kind", "code")
                page.click("#task-submit")
                page.wait_for_selector("#tasks article")
                assert page.evaluate("window.UNSAFE") is None
                assert len(server.store.snapshot()["tasks"]) == 1
                assert not server.store.snapshot()["workers"][0]["online"]
                for width in [1280, 390, 320]:
                    page.set_viewport_size({"width": width, "height": 950})
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"), width
                page.set_viewport_size({"width": 1280, "height": 1000})
                page.locator("details.card > summary").click()
                page.locator("summary", has_text="Cadastrar um agente").click()
                screenshot = Path(__file__).resolve().parents[1] / "docs" / "preview.png"
                screenshot.parent.mkdir(exist_ok=True)
                page.screenshot(path=str(screenshot), full_page=True)
                assert not errors, errors
                page.click("#logout")
                assert page.locator("#login").is_visible()
                browser.close()
        finally:
            server.shutdown(); server.server_close(); thread.join()
    print("Browser smoke passed: login, projects, workers, queue, XSS escaping, logout; widths 320/390/1280.")


if __name__ == "__main__": main()
