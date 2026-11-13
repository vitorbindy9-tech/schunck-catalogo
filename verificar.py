#!/usr/bin/env python3
"""Verificador do catálogo Schunck TV.

Lê sinais.m3u, testa cada endereço e grava:
  catalogo.json  - o que o aplicativo baixa (só canais com sinal respondendo)
  estado.json    - histórico de falhas de cada endereço
  RELATORIO.md   - resumo legível da última verificação

Só usa a biblioteca padrão do Python 3.8+.
Uso:  python verificar.py            (testa tudo)
      python verificar.py --sem-teste (só converte a lista, sem testar)
"""
import argparse
import concurrent.futures
import datetime
import hashlib
import json
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
LIMITE_FALHAS = 3      # verificações seguidas com falha antes de tirar o endereço
TEMPO_LIMITE = 12      # segundos por endereço
SIMULTANEOS = 24
AGENTE = "VLC/3.0.20 LibVLC/3.0.20"
ATRIBUTO = re.compile(r'([\w-]+)="([^"]*)"')


def ler_lista(caminho):
    """Agrupa as entradas do M3U por canal (tvg-id), na ordem do arquivo."""
    canais, meta = {}, None
    for linha in caminho.read_text(encoding="utf-8-sig").splitlines():
        linha = linha.strip()
        if linha.startswith("#EXTINF:"):
            meta = (dict(ATRIBUTO.findall(linha)), ATRIBUTO.sub("", linha).partition(",")[2].strip())
        elif meta and linha and not linha.startswith("#"):
            atributos, nome = meta
            meta = None
            chave = atributos.get("tvg-id") or hashlib.sha256(linha.encode()).hexdigest()[:20]
            canal = canais.setdefault(chave, {
                "key": chave,
                "tvgId": atributos.get("tvg-id", ""),
                "name": nome,
                "group": atributos.get("group-title", "Variedades"),
                "logo": atributos.get("tvg-logo", ""),
                "country": atributos.get("tvg-country", ""),
                "sp": atributos.get("tvg-region", "") == "BR-SP",
                "type": "radio" if atributos.get("radio") == "true" else "live",
                "variants": [],
            })
            if all(v["url"] != linha for v in canal["variants"]):
                canal["variants"].append({"url": linha, "quality": atributos.get("tvg-quality", "")})
    return list(canais.values())


def baixar(endereco, limite=65536):
    pedido = urllib.request.Request(endereco, headers={"User-Agent": AGENTE, "Accept": "*/*"})
    contexto = ssl.create_default_context()
    with urllib.request.urlopen(pedido, timeout=TEMPO_LIMITE, context=contexto) as resposta:
        return resposta.geturl(), resposta.headers.get("Content-Type", "").lower(), resposta.read(limite)


def testar(endereco):
    """Devolve (True, formato) ou (False, motivo)."""
    try:
        final, tipo, corpo = baixar(endereco)
        texto = corpo[:2048].decode("utf-8", "replace").lstrip("﻿ \r\n")
        if texto.startswith("#EXTM3U"):
            linhas = [l.strip() for l in corpo.decode("utf-8", "replace").splitlines()]
            if any(l.startswith("#EXTINF") for l in linhas):
                return True, "HLS"
            proxima = next((l for l in linhas if l and not l.startswith("#")), None)
            if not proxima:
                return False, "lista HLS vazia"
            _, _, interno = baixar(urllib.parse.urljoin(final, proxima))
            if b"#EXTINF" in interno:
                return True, "HLS"
            return False, "lista HLS sem segmentos"
        if corpo[:1] == b"G" and (len(corpo) < 189 or corpo[188:189] == b"G"):
            return True, "MPEG-TS"
        if tipo.startswith(("video/", "audio/")) or "mpegurl" in tipo or "octet-stream" in tipo:
            return (True, tipo.split(";")[0]) if corpo else (False, "resposta vazia")
        return False, "resposta não é vídeo (" + (tipo.split(";")[0] or "sem tipo") + ")"
    except urllib.error.HTTPError as erro:
        return False, "HTTP %d" % erro.code
    except Exception as erro:  # rede, tempo esgotado, certificado...
        return False, type(erro).__name__


def main():
    opcoes = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    opcoes.add_argument("--sem-teste", action="store_true", help="gera o catálogo sem testar os endereços")
    args = opcoes.parse_args()

    canais = ler_lista(RAIZ / "sinais.m3u")
    arquivo_estado = RAIZ / "estado.json"
    antigo = json.loads(arquivo_estado.read_text(encoding="utf-8")) if arquivo_estado.exists() else {}
    arquivo_manter = RAIZ / "manter.txt"
    manter = {l.strip() for l in arquivo_manter.read_text(encoding="utf-8").splitlines()
              if l.strip() and not l.startswith("#")} if arquivo_manter.exists() else set()
    hoje = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    enderecos = [v["url"] for c in canais for v in c["variants"]]
    if args.sem_teste:
        resultados = {e: (True, "não testado") for e in enderecos}
    else:
        with concurrent.futures.ThreadPoolExecutor(SIMULTANEOS) as grupo:
            resultados = dict(zip(enderecos, grupo.map(testar, enderecos)))

    estado = {}
    for endereco, (ok, detalhe) in resultados.items():
        anterior = antigo.get(endereco, {})
        estado[endereco] = {
            "falhas": 0 if ok else anterior.get("falhas", 0) + 1,
            "ultimo_ok": hoje if ok else anterior.get("ultimo_ok", ""),
            "detalhe": detalhe,
        }

    publicados, instaveis, retirados = [], [], []
    for canal in canais:
        protegido = canal["tvgId"] in manter or canal["name"] in manter
        vivos = [v for v in canal["variants"] if estado[v["url"]]["falhas"] == 0]
        em_espera = [v for v in canal["variants"] if 0 < estado[v["url"]]["falhas"] < LIMITE_FALHAS]
        mortos = [v for v in canal["variants"] if estado[v["url"]]["falhas"] >= LIMITE_FALHAS]
        escolhidos = vivos + em_espera + (mortos if protegido else [])
        motivo = estado[canal["variants"][0]["url"]]["detalhe"]
        if escolhidos:
            publicados.append(dict(canal, variants=escolhidos))
            if not vivos:
                instaveis.append((canal["name"], motivo, max(estado[v["url"]]["falhas"] for v in canal["variants"])))
        else:
            retirados.append((canal["name"], motivo, estado[canal["variants"][0]["url"]]["ultimo_ok"] or "nunca"))

    base = json.dumps(publicados, ensure_ascii=False, sort_keys=True)
    catalogo = {"revision": hashlib.sha256(base.encode()).hexdigest()[:16], "items": publicados}
    (RAIZ / "catalogo.json").write_text(
        json.dumps(catalogo, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    if not args.sem_teste:
        arquivo_estado.write_text(json.dumps(estado, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                                  encoding="utf-8")

    linhas = [
        "# Verificação do catálogo", "",
        "Última verificação: " + hoje, "",
        "| Resultado | Canais |", "| --- | ---: |",
        "| Na lista de origem | %d |" % len(canais),
        "| Publicados no catálogo | %d |" % len(publicados),
        "| Publicados, mas falhando (em observação) | %d |" % len(instaveis),
        "| Retirados após %d falhas seguidas | %d |" % (LIMITE_FALHAS, len(retirados)), "",
    ]
    if instaveis:
        linhas += ["## Em observação", "", "| Canal | Motivo | Falhas seguidas |", "| --- | --- | ---: |"]
        linhas += ["| %s | %s | %d |" % item for item in instaveis] + [""]
    if retirados:
        linhas += ["## Retirados do catálogo", "", "| Canal | Motivo | Último sinal |", "| --- | --- | --- |"]
        linhas += ["| %s | %s | %s |" % item for item in retirados] + [""]
    (RAIZ / "RELATORIO.md").write_text("\n".join(linhas), encoding="utf-8")
    print("origem %d | publicados %d | em observação %d | retirados %d"
          % (len(canais), len(publicados), len(instaveis), len(retirados)))


if __name__ == "__main__":
    main()
