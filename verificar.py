#!/usr/bin/env python3
"""Verificador do catálogo Schunck TV.

Lê sinais.m3u, testa cada endereço e grava:
  catalogo.json  - o que o aplicativo baixa (só canais com sinal respondendo)
  estado.json    - histórico de falhas de cada endereço
  RELATORIO.md   - resumo legível da última verificação

Só usa a biblioteca padrão do Python 3.8+.
Uso:  python verificar.py            (testa tudo; é o que o GitHub roda)
      python verificar.py --casa     (testa da sua rede e grava confirmados-em-casa.txt)
      python verificar.py --sem-teste (só converte a lista, sem testar)
"""
import argparse
import concurrent.futures
import datetime
import hashlib
import json
import re
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
LIMITE_FALHAS = 3      # verificações seguidas com falha antes de tirar o endereço
TEMPO_LIMITE = 12      # segundos por endereço
SIMULTANEOS = 24
VALIDADE_CASA = 30     # dias em que a confirmação feita em casa protege o canal
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


def baixar(endereco, limite=65536, conferir_certificado=True):
    pedido = urllib.request.Request(endereco, headers={"User-Agent": AGENTE, "Accept": "*/*"})
    contexto = ssl.create_default_context()
    if not conferir_certificado:  # só para sondar o sinal; nenhum dado sensível é enviado
        contexto.check_hostname = False
        contexto.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(pedido, timeout=TEMPO_LIMITE, context=contexto) as resposta:
        return resposta.geturl(), resposta.headers.get("Content-Type", "").lower(), resposta.read(limite)


def motivo(erro):
    """Traduz o erro de rede para um texto curto."""
    causa = getattr(erro, "reason", erro)
    if isinstance(causa, ssl.SSLCertVerificationError):
        return "certificado inválido"
    if isinstance(causa, (socket.timeout, TimeoutError)) or "timed out" in str(causa):
        return "sem resposta (tempo esgotado)"
    if isinstance(causa, socket.gaierror):
        return "endereço não existe mais (DNS)"
    if isinstance(causa, ConnectionRefusedError):
        return "conexão recusada"
    if isinstance(causa, ssl.SSLError):
        return "falha na conexão segura"
    if isinstance(causa, OSError) and getattr(causa, "errno", None) in (101, 113):
        return "servidor inalcançável"
    return type(causa).__name__


def sondar(endereco, conferir_certificado):
    final, tipo, corpo = baixar(endereco, conferir_certificado=conferir_certificado)
    texto = corpo[:2048].decode("utf-8", "replace").lstrip("\ufeff \r\n")
    if texto.startswith("#EXTM3U"):
        linhas = [l.strip() for l in corpo.decode("utf-8", "replace").splitlines()]
        if any(l.startswith("#EXTINF") for l in linhas):
            return True, "HLS"
        proxima = next((l for l in linhas if l and not l.startswith("#")), None)
        if not proxima:
            return False, "lista HLS vazia"
        _, _, interno = baixar(urllib.parse.urljoin(final, proxima), conferir_certificado=conferir_certificado)
        if b"#EXTINF" in interno:
            return True, "HLS"
        return False, "lista HLS sem segmentos"
    if corpo[:1] == b"G" and (len(corpo) < 189 or corpo[188:189] == b"G"):
        return True, "MPEG-TS"
    if tipo.startswith(("video/", "audio/")) or "mpegurl" in tipo or "octet-stream" in tipo:
        return (True, tipo.split(";")[0]) if corpo else (False, "resposta vazia")
    return False, "resposta não é vídeo (" + (tipo.split(";")[0] or "sem tipo") + ")"


def testar(endereco):
    """Devolve (True, formato) ou (False, motivo). Repete uma vez antes de dar como falha."""
    resultado = (False, "")
    conferir = True
    for _ in range(2):
        try:
            ok, detalhe = sondar(endereco, conferir)
            if ok and not conferir:
                detalhe += " (certificado inválido)"
            resultado = (ok, detalhe)
        except urllib.error.HTTPError as erro:
            resultado = (False, "HTTP %d" % erro.code)
        except Exception as erro:  # rede, tempo esgotado, certificado...
            resultado = (False, motivo(erro))
            if resultado[1] == "certificado inválido":
                conferir = False
        if resultado[0]:
            break
    return resultado


def main():
    opcoes = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    opcoes.add_argument("--casa", action="store_true",
                        help="testa da sua rede e grava confirmados-em-casa.txt, sem mexer no catálogo")
    opcoes.add_argument("--sem-teste", action="store_true", help="gera o catálogo sem testar os endereços")
    args = opcoes.parse_args()

    canais = ler_lista(RAIZ / "sinais.m3u")
    arquivo_estado = RAIZ / "estado.json"
    antigo = json.loads(arquivo_estado.read_text(encoding="utf-8")) if arquivo_estado.exists() else {}
    arquivo_manter = RAIZ / "manter.txt"
    manter = {l.strip() for l in arquivo_manter.read_text(encoding="utf-8").splitlines()
              if l.strip() and not l.startswith("#")} if arquivo_manter.exists() else set()
    agora = datetime.datetime.now(datetime.timezone.utc)
    hoje = agora.strftime("%Y-%m-%d %H:%M UTC")

    # Canais confirmados na rede de casa ficam protegidos por VALIDADE_CASA dias.
    arquivo_casa = RAIZ / "confirmados-em-casa.txt"
    confirmados = set()
    if arquivo_casa.exists() and not args.casa:
        texto = arquivo_casa.read_text(encoding="utf-8").splitlines()
        data = re.search(r"\d{4}-\d{2}-\d{2}", texto[0] if texto else "")
        if data and (agora.date() - datetime.date.fromisoformat(data[0])).days <= VALIDADE_CASA:
            confirmados = {l.strip() for l in texto[1:] if l.strip() and not l.startswith("#")}

    enderecos = [v["url"] for c in canais for v in c["variants"]]
    if args.sem_teste:
        resultados = {e: (True, "não testado") for e in enderecos}
    else:
        with concurrent.futures.ThreadPoolExecutor(SIMULTANEOS) as grupo:
            resultados = dict(zip(enderecos, grupo.map(testar, enderecos)))

    if args.casa:
        bons = [c for c in canais if any(resultados[v["url"]][0] for v in c["variants"])]
        ruins = [c for c in canais if c not in bons]
        arquivo_casa.write_text(
            "# Confirmados em casa em %s\n" % agora.strftime("%Y-%m-%d")
            + "".join(c["key"] + "\n" for c in bons), encoding="utf-8")
        linhas = ["# Verificação feita em casa", "", "Data: " + hoje, "",
                  "Responderam: %d de %d canais." % (len(bons), len(canais)), "",
                  "## Não responderam", "", "| Canal | Motivo |", "| --- | --- |"]
        linhas += ["| %s | %s |" % (c["name"], resultados[c["variants"][0]["url"]][1]) for c in ruins]
        (RAIZ / "RELATORIO-CASA.md").write_text("\n".join(linhas) + "\n", encoding="utf-8")
        print("Em casa: %d de %d canais responderam. Envie confirmados-em-casa.txt ao GitHub."
              % (len(bons), len(canais)))
        return

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
        protegido = (canal["tvgId"] in manter or canal["name"] in manter
                     or canal["key"] in confirmados)
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
