"""Coleta os contratos B2B da API (Bearer Token) e grava o retorno bruto.

O token vem da variável de ambiente MEDWAY_B2B_TOKEN (secret do GitHub Actions).
Grava data/raw/b2b_contratos.json com todas as páginas, se a API paginar.
"""
import json
import os
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen

BASE_URL = os.environ.get(
    "MEDWAY_B2B_URL", "http://yeh3066gfbw2sw3ywghufr90.187.77.56.193.sslip.io"
).rstrip("/")
TOKEN = os.environ.get("MEDWAY_B2B_TOKEN", "")
OUT = Path(__file__).resolve().parent.parent / "data" / "raw" / "b2b_contratos.json"


def get(path, params=None, auth=True, tries=4):
    url = urljoin(BASE_URL + "/", path.lstrip("/"))
    if params:
        url += ("&" if "?" in url else "?") + urlencode(params)
    headers = {"Accept": "application/json", "User-Agent": "medway-salesops-case"}
    if auth:
        headers["Authorization"] = f"Bearer {TOKEN}"
    for i in range(tries):
        try:
            with urlopen(Request(url, headers=headers), timeout=60) as r:
                return json.loads(r.read().decode("utf-8"))
        except HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and i < tries - 1:
                time.sleep(2 ** i * 2)
                continue
            raise
        except URLError:
            if i < tries - 1:
                time.sleep(2 ** i * 2)
                continue
            raise


def main():
    if not TOKEN:
        sys.exit("MEDWAY_B2B_TOKEN não definido")
    print("health:", get("/health", auth=False))

    # A API devolve {total, limit, offset, contratos}: pagina por offset até cobrir o total
    limit = 100
    pages = [get("/contratos", {"limit": limit, "offset": 0})]
    total = int(pages[0].get("total", 0))
    offset = len(pages[0].get("contratos", []))
    while offset < total:
        page = get("/contratos", {"limit": limit, "offset": offset})
        got = len(page.get("contratos", []))
        if got == 0:
            break
        pages.append(page)
        offset += got

    n = sum(len(p.get("contratos", [])) for p in pages)
    if n != total:
        sys.exit(f"Coleta incompleta: {n} de {total} contratos")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "coletado_em": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "fonte": BASE_URL + "/contratos",
        "paginas": pages,
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"ok: {len(pages)} página(s) gravadas em {OUT}")


if __name__ == "__main__":
    main()
