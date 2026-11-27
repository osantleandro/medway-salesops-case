"""Trata B2C (CSV do Google Sheets) e B2B (API), consolida e gera dashboard/data.json.

Regras de tratamento estão documentadas em docs/REGRAS.md. Tudo o que é descartado
ou corrigido vai para o bloco "qualidade" do JSON, que o dashboard mostra.
"""
import csv
import io
import json
import os
import re
from collections import Counter, defaultdict
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from statistics import median
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
OUT = ROOT / "dashboard" / "data.json"
CLEAN = ROOT / "data" / "tratado"

# Fonte B2C: se houver URL de CSV publicado do Google Sheets, lê de lá; senão, o arquivo do repo
B2C_URL = os.environ.get("B2C_SHEET_CSV_URL", "").strip()
B2C_FILE = RAW / "b2c_vendas.csv"
B2B_FILE = RAW / "b2b_contratos.json"
VIGENCIA_MESES = 12  # contrato B2B sem data de fim: vigência assumida de 12 meses


def ym(d):
    return f"{d.year}-{d.month:02d}"


def add_months(d, n):
    m = d.month - 1 + n
    return date(d.year + m // 12, m % 12 + 1, 1)


def month_range(a, b):
    out, d = [], date(a.year, a.month, 1)
    while d <= b:
        out.append(ym(d))
        d = add_months(d, 1)
    return out


# ---------------------------------------------------------------- B2C
def ler_b2c():
    if B2C_URL:
        with urlopen(B2C_URL, timeout=60) as r:
            texto = r.read().decode("utf-8")
        origem = "Google Sheets (CSV publicado)"
    else:
        texto = B2C_FILE.read_text(encoding="utf-8")
        origem = "data/raw/b2c_vendas.csv (export do Google Sheets)"
    return list(csv.DictReader(io.StringIO(texto))), origem


def parse_data(s):
    s = s.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return datetime.strptime(s, "%Y-%m-%d").date(), "ISO"
    if re.fullmatch(r"\d{2}/\d{2}/\d{4}", s):
        return datetime.strptime(s, "%d/%m/%Y").date(), "BR"
    raise ValueError(f"data fora do padrão: {s}")


def parse_valor(s):
    s = s.strip()
    neg = s.startswith("-")
    t = s.replace("-", "").replace("R$", "").strip()
    if re.fullmatch(r"\d+\.\d{1,2}", t):  # 1234.56 (padrão americano)
        v, fmt = float(t), "decimal com ponto"
    else:  # 1.234,56 ou 1234,56 (padrão BR)
        v, fmt = float(t.replace(".", "").replace(",", ".")), ("R$ BR" if "R$" in s else "BR sem R$")
    return (-v if neg else v), fmt


def tratar_b2c(linhas):
    q = {"linhas_lidas": len(linhas), "formatos_data": Counter(), "formatos_valor": Counter(),
         "duplicadas": 0, "valor_invalido": [], "regiao_padronizada": 0,
         "embaixador_sem_nome": 0, "evento_sem_tipo": 0}
    vistos, vendas = set(), []
    for i, r in enumerate(linhas, start=2):
        chave = tuple((k, (r.get(k) or "").strip()) for k in sorted(r))
        if chave in vistos:
            q["duplicadas"] += 1
            continue
        vistos.add(chave)
        d, fd = parse_data(r["data_venda"])
        v, fv = parse_valor(r["valor_pago"])
        q["formatos_data"][fd] += 1
        q["formatos_valor"][fv] += 1
        if v <= 0:
            q["valor_invalido"].append({"linha": i, "data": d.isoformat(), "produto": r["produto"], "valor": v})
            continue
        reg = (r.get("regiao_evento") or "").strip()
        if reg and reg != reg.upper():
            q["regiao_padronizada"] += 1
        origem = r["origem"].strip()
        emb = (r.get("embaixador") or "").strip()
        tipo_ev = (r.get("tipo_evento") or "").strip()
        if origem == "Embaixador" and not emb:
            q["embaixador_sem_nome"] += 1
            emb = "Não informado"
        if origem == "Evento Presencial" and not tipo_ev:
            q["evento_sem_tipo"] += 1
            tipo_ev = "Não informado"
        cupom = r["cupom"].strip()
        familia = re.sub(r"[-_]?\d+$", "", cupom)
        familia = {"SEM_CUPOM": "Sem cupom", "NEGOCIAÇÃO-VENDA": "Negociação", "RETENCAO": "Retenção",
                   "EMBAIXADOR": "Embaixador"}.get(familia, "Evento" if familia.startswith("EVENTO") else familia)
        vendas.append({
            "data": d.isoformat(), "mes": ym(d), "ano": d.year, "tri": f"{d.year}-T{(d.month - 1) // 3 + 1}",
            "origem": origem, "embaixador": emb, "tipo_evento": tipo_ev, "regiao_evento": reg.upper(),
            "produto": r["produto"].strip(), "icp": r["icp_comprador"].strip(), "cupom": cupom,
            "cupom_familia": familia, "valor": round(v, 2),
        })
    q["formatos_data"] = dict(q["formatos_data"])
    q["formatos_valor"] = dict(q["formatos_valor"])
    q["linhas_validas"] = len(vendas)
    return vendas, q


# ---------------------------------------------------------------- B2B
SUFIXO = re.compile(r"\s*-\s*(Renovação( \d{4})?|Turma \d{4}|Nova Turma)\s*$", re.I)


def tratar_b2b(raw):
    contratos = [c for p in raw["paginas"] for c in p.get("contratos", [])]
    q = {"contratos_lidos": len(contratos), "status_desatualizado": 0, "status_brutos": dict(Counter(c["status"] for c in contratos))}
    for c in contratos:
        c["conta"] = SUFIXO.sub("", c["instituicao"]).strip()
        c["inicio"] = datetime.strptime(c["data_inicio"], "%Y-%m-%d").date()
        c["preco_licenca"] = round(c["valor_mensal"] / c["num_licencas"], 2) if c["num_licencas"] else None
    por_conta = defaultdict(list)
    for c in contratos:
        por_conta[c["conta"]].append(c)
    for conta, cs in por_conta.items():
        cs.sort(key=lambda c: c["inicio"])
        for i, c in enumerate(cs):
            fim_vig = add_months(c["inicio"], VIGENCIA_MESES)
            prox = cs[i + 1]["inicio"] if i + 1 < len(cs) else None
            c["fim"] = min(fim_vig, prox) if prox else fim_vig
            c["tipo_venda"] = "Novo logo" if i == 0 else "Renovação"
            c["anterior"] = cs[i - 1]["id_contrato"] if i else None
            # API marca 'ativo' contratos já substituídos pela renovação: corrige para não somar MRR em dobro
            if prox and c["status"] == "ativo":
                q["status_desatualizado"] += 1
                c["status_tratado"] = "renovado"
            else:
                c["status_tratado"] = c["status"]
    q["contas"] = len(por_conta)
    return contratos, q


# ---------------------------------------------------------------- Agregações
def soma_por(itens, chave, valor="valor"):
    acc = defaultdict(lambda: {"n": 0, "valor": 0.0})
    for it in itens:
        k = it[chave]
        acc[k]["n"] += 1
        acc[k]["valor"] += it[valor]
    return {k: {"n": v["n"], "valor": round(v["valor"], 2), "ticket": round(v["valor"] / v["n"], 2)} for k, v in acc.items()}


def brl(v, casas=1):
    if abs(v) >= 1e6:
        return f"R$ {v / 1e6:,.{casas}f} mi".replace(",", "X").replace(".", ",").replace("X", ".")
    if abs(v) >= 1e3:
        return f"R$ {v / 1e3:,.{casas}f} mil".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {v:,.0f}".replace(",", ".")


MESES_PT = {1: "jan", 2: "fev", 3: "mar", 4: "abr", 5: "mai", 6: "jun", 7: "jul", 8: "ago", 9: "set", 10: "out", 11: "nov", 12: "dez"}


def num(v, casas=0):
    return f"{v:,.{casas}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def pct(v, casas=0):
    return f"{v:,.{casas}f}%".replace(",", "X").replace(".", ",").replace("X", ".")


def gerar_insights(d, vendas, contratos, ativos_ref):
    """Insights recalculados a cada atualização. Cada um traz o achado, o número e a decisão sugerida."""
    out = []
    cons = d["consolidado"]
    b2c, b2b = d["b2c"], d["b2b"]
    ult = d["b2c"]["ytd_ate_mes"]
    anos = sorted(b2c["ytd"])
    if len(anos) >= 2:
        a0, a1 = b2c["ytd"][anos[-2]], b2c["ytd"][anos[-1]]
        cres = (a1["valor"] / a0["valor"] - 1) * 100
        out.append({"area": "B2C", "titulo": f"B2C cresceu {pct(cres)} no acumulado jan-{MESES_PT[ult]}",
                    "achado": f"{brl(a1['valor'])} em {num(a1['n'])} vendas contra {brl(a0['valor'])} em {num(a0['n'])} no mesmo período de {anos[-2]}. O crescimento veio de volume; o ticket ficou estável.",
                    "decisao": "Manter o investimento nos canais que escalaram e acompanhar o ticket: crescer só por volume com desconto corrói margem."})
    pico = max(cons, key=lambda r: r["b2c_valor"])
    media_resto = median([r["b2c_valor"] for r in cons if r["mes"] != pico["mes"]])
    ano_pico = [r for r in cons if r["mes"][:4] == pico["mes"][:4]]
    share_h2 = 100 * sum(r["b2c_valor"] for r in ano_pico if int(r["mes"][5:]) >= 8) / sum(r["b2c_valor"] for r in ano_pico)
    out.append({"area": "B2C", "titulo": f"{MESES_PT[int(pico['mes'][5:])].capitalize()}/{pico['mes'][2:4]} concentrou {pct(100 * pico['b2c_valor'] / sum(r['b2c_valor'] for r in cons if r['mes'][:4] == pico['mes'][:4]))} do ano",
                "achado": f"O mês de pico vendeu {brl(pico['b2c_valor'])}, cerca de {num(pico['b2c_valor'] / media_resto, 1)}x a mediana dos outros meses. Ago-dez de {pico['mes'][:4]} somou {pct(share_h2)} das vendas do ano.",
                "decisao": "Planejar capacidade de vendas, mídia e estoque de cupons para ago-out com antecedência; a base termina em julho, então o pico de 2026 ainda está por vir."})
    desc = b2c["desconto_por_cupom"]
    if "Embaixador" in desc:
        emb = b2c["por_origem"].get("Embaixador", {})
        out.append({"area": "B2C", "titulo": f"Cupom de embaixador custa {pct(desc['Embaixador']['desconto_medio_pct'], 1)} de desconto médio",
                    "achado": f"Embaixador tem o menor ticket entre as origens ({brl(emb.get('ticket', 0))}) e o top 10 de embaixadores faz {pct(b2c['embaixadores_top10_pct'])} da receita do canal.",
                    "decisao": "Rever a faixa de 20% (a mais cara) e premiar volume por embaixador em vez de desconto por aluno; concentrar o programa nos que entregam."})
    if "Retenção" in desc:
        n_ret = desc["Retenção"]["n"]
        out.append({"area": "B2C", "titulo": f"{pct(100 * n_ret / len(vendas))} das vendas usaram cupom de retenção",
                    "achado": f"{n_ret} vendas com cupom RETENCAO, desconto médio de {pct(desc['Retenção']['desconto_medio_pct'], 1)}, aparecendo em vendas orgânicas e de indicação.",
                    "decisao": "Confirmar se cupom de retenção está sendo usado em aquisição. Se sim, é desconto sem dono: criar regra de uso e medir quanto dele volta em conversão."})
    out.append({"area": "B2B", "titulo": f"Nenhum logo novo no B2B em {max(c['inicio'].year for c in contratos)}",
                "achado": f"As {len([r for r in b2b['renovacoes']])} vendas B2B do ano são renovações das {b2b['contas_total']} contas de {min(c['inicio'].year for c in contratos)}. O MRR cresceu só por expansão (NRR {pct(b2b['nrr_renovacao_pct'], 1)}, licenças +{pct(b2b['licencas_var_pct'], 1)}).",
                "decisao": "A base renova e expande bem, mas a aquisição parou. Abrir meta de novos logos por região e tipo de instituição (faculdades privadas, ligas fora do Sudeste)."})
    ent = b2b["por_plano"].get("Institucional Enterprise", {})
    if ent:
        out.append({"area": "B2B", "titulo": f"{ent['n']} contratos Enterprise são {pct(100 * ent['valor'] / b2b['mrr_atual'])} do MRR B2B",
                    "achado": f"MRR atual de {brl(b2b['mrr_atual'])} (ARR {brl(b2b['arr_atual'])}) em {num(b2b['licencas_atuais'])} licenças. A perda de uma conta Enterprise tira cerca de {pct(100 * ent['ticket'] / b2b['mrr_atual'])} da receita recorrente.",
                    "decisao": "Plano de sucesso dedicado para as contas Enterprise e renovação antecipada (90 dias antes do aniversário)."})
    tk_r1 = b2c["por_produto"].get("Extensivo R1", {}).get("ticket")
    if tk_r1:
        anual_pro = 79 * 12
        out.append({"area": "Consolidado", "titulo": f"Um aluno B2B rende {pct(100 * anual_pro / tk_r1)} de um aluno B2C",
                    "achado": f"No plano Pro, a licença custa R$ 79/mês (R$ {num(anual_pro, 0)}/ano) contra ticket médio de {brl(tk_r1)} do Extensivo R1 no B2C. O B2B é {pct(100 * b2b['mrr_atual'] / cons[-1]['receita_mes'], 1)} da receita do último mês.",
                    "decisao": "Tratar B2B como canal de alcance e marca dentro das faculdades, e medir se ele alimenta o B2C (ex.: aluno de licença que compra R+ depois de formado) ou se canibaliza."})
    mrr_status_api = sum(c["valor_mensal"] for c in contratos if c["status"] == "ativo")
    out.append({"area": "Dados", "titulo": f"{d['qualidade']['b2b']['status_desatualizado']} contratos B2B constam como 'ativo' mas já foram renovados",
                "achado": f"Somar o MRR pelo status da API daria {brl(mrr_status_api)}, contando a mesma conta duas vezes; o MRR real é {brl(b2b['mrr_atual'])}. A base B2C chega com 2 formatos de data, 3 de valor, duplicadas e um estorno.",
                "decisao": "Definir fim de vigência e status 'renovado' na origem, e padronizar a exportação do B2C. O pipeline corrige isso hoje, mas a regra tem que nascer no sistema."})
    return out


def main():
    linhas, origem_b2c = ler_b2c()
    vendas, q_b2c = tratar_b2c(linhas)
    raw_b2b = json.loads(B2B_FILE.read_text(encoding="utf-8"))
    contratos, q_b2b = tratar_b2b(raw_b2b)

    d_min = min(date.fromisoformat(v["data"]) for v in vendas)
    d_max_b2c = max(date.fromisoformat(v["data"]) for v in vendas)
    d_max_b2b = max(c["inicio"] for c in contratos)
    ref = max(d_max_b2c, d_max_b2b)
    meses = month_range(min(d_min, min(c["inicio"] for c in contratos)), ref)

    # B2C por mês
    b2c_mes = {m: {"n": 0, "valor": 0.0} for m in meses}
    for v in vendas:
        b2c_mes[v["mes"]]["n"] += 1
        b2c_mes[v["mes"]]["valor"] += v["valor"]

    # B2B: MRR ativo por mês e bookings (ACV = mensal x 12) no mês de início
    b2b_mes = {m: {"mrr": 0.0, "licencas": 0, "contratos": 0, "bookings": 0.0, "novos": 0, "renov": 0,
                   "mrr_novo": 0.0, "mrr_expansao": 0.0} for m in meses}
    for c in contratos:
        for m in month_range(c["inicio"], add_months(c["fim"], -1)):
            if m in b2b_mes:
                b2b_mes[m]["mrr"] += c["valor_mensal"]
                b2b_mes[m]["licencas"] += c["num_licencas"]
                b2b_mes[m]["contratos"] += 1
        mi = ym(c["inicio"])
        b2b_mes[mi]["bookings"] += c["valor_mensal"] * VIGENCIA_MESES
        if c["tipo_venda"] == "Novo logo":
            b2b_mes[mi]["novos"] += 1
            b2b_mes[mi]["mrr_novo"] += c["valor_mensal"]
        else:
            b2b_mes[mi]["renov"] += 1
            ant = next(x for x in contratos if x["id_contrato"] == c["anterior"])
            b2b_mes[mi]["mrr_expansao"] += c["valor_mensal"] - ant["valor_mensal"]

    # Renovações: comparação conta a conta
    renov = []
    for c in contratos:
        if c["anterior"]:
            a = next(x for x in contratos if x["id_contrato"] == c["anterior"])
            renov.append({"conta": c["conta"], "tipo": c["tipo"], "plano": c["plano"], "regiao": c["regiao"],
                          "status_anterior": a["status"], "mrr_antes": a["valor_mensal"], "mrr_depois": c["valor_mensal"],
                          "lic_antes": a["num_licencas"], "lic_depois": c["num_licencas"],
                          "var_pct": round((c["valor_mensal"] / a["valor_mensal"] - 1) * 100, 1),
                          "inicio": c["data_inicio"]})
    mrr_antes = sum(r["mrr_antes"] for r in renov)
    mrr_depois = sum(r["mrr_depois"] for r in renov)

    ativos_ref = [c for c in contratos if c["inicio"] <= ref < c["fim"]]
    mrr_ref = sum(c["valor_mensal"] for c in ativos_ref)
    lic_ref = sum(c["num_licencas"] for c in ativos_ref)

    # Comparação justa: ticket B2C x valor anual por aluno no B2B
    preco_plano = {}
    for c in contratos:
        preco_plano.setdefault(c["plano"], set()).add(c["preco_licenca"])
    ticket_prod = soma_por(vendas, "produto")

    # YTD comparável (mesmos meses nos dois anos)
    ult_mes = d_max_b2c.month
    ytd = {}
    for ano in sorted({v["ano"] for v in vendas}):
        sel = [v for v in vendas if v["ano"] == ano and int(v["mes"][5:]) <= ult_mes]
        ytd[ano] = {"n": len(sel), "valor": round(sum(v["valor"] for v in sel), 2)}

    # Desconto implícito: ticket por família de cupom contra 'Sem cupom', dentro do mesmo produto
    base = {p: median([v["valor"] for v in vendas if v["produto"] == p and v["cupom_familia"] == "Sem cupom"]) for p in ticket_prod}
    desc = defaultdict(list)
    for v in vendas:
        desc[v["cupom_familia"]].append(1 - v["valor"] / base[v["produto"]])
    desconto = {k: {"n": len(x), "desconto_medio_pct": round(100 * sum(x) / len(x), 1)} for k, x in desc.items()}

    # Embaixadores
    emb = soma_por([v for v in vendas if v["origem"] == "Embaixador"], "embaixador")
    emb_rank = sorted(emb.items(), key=lambda kv: -kv[1]["valor"])
    top10 = sum(x["valor"] for _, x in emb_rank[:10])
    tot_emb = sum(x["valor"] for _, x in emb_rank)

    # Eventos
    ev_reg = soma_por([v for v in vendas if v["origem"] == "Evento Presencial"], "regiao_evento")
    ev_tipo = soma_por([v for v in vendas if v["origem"] == "Evento Presencial"], "tipo_evento")

    # Séries cruzadas para o dashboard
    def serie(itens, dim):
        cats = sorted({i[dim] for i in itens})
        s = {c: [0.0] * len(meses) for c in cats}
        idx = {m: i for i, m in enumerate(meses)}
        for i in itens:
            s[i[dim]][idx[i["mes"]]] += i["valor"]
        return {c: [round(x, 2) for x in v] for c, v in s.items()}

    consolidado = []
    for m in meses:
        b2c_v = b2c_mes[m]["valor"]
        b = b2b_mes[m]
        consolidado.append({
            "mes": m,
            "b2c_vendas": b2c_mes[m]["n"], "b2c_valor": round(b2c_v, 2),
            "b2b_mrr": round(b["mrr"], 2), "b2b_licencas": b["licencas"], "b2b_contratos": b["contratos"],
            "b2b_bookings": round(b["bookings"], 2), "b2b_novos": b["novos"], "b2b_renov": b["renov"],
            "b2b_mrr_novo": round(b["mrr_novo"], 2), "b2b_mrr_expansao": round(b["mrr_expansao"], 2),
            "bookings_total": round(b2c_v + b["bookings"], 2),
            "receita_mes": round(b2c_v + b["mrr"], 2),
        })

    tot_b2c = sum(v["valor"] for v in vendas)
    tot_b2b_book = sum(c["valor_mensal"] * VIGENCIA_MESES for c in contratos)
    anos = sorted({v["ano"] for v in vendas})

    data = {
        "gerado_em": datetime.now(timezone(timedelta(hours=-3))).strftime("%d/%m/%Y %H:%M"),
        "referencia": ref.isoformat(),
        "periodo": {"inicio": meses[0], "fim": meses[-1], "ultimo_mes_b2c": ym(d_max_b2c)},
        "fontes": {"b2c": origem_b2c, "b2b": raw_b2b.get("fonte", "/contratos"), "b2b_coletado_em": raw_b2b.get("coletado_em")},
        "premissas": {"vigencia_b2b_meses": VIGENCIA_MESES,
                      "bookings_b2b": "valor mensal x 12 no mês de início do contrato",
                      "receita_mes": "vendas B2C do mês + MRR B2B ativo no mês"},
        "meses": meses,
        "consolidado": consolidado,
        "b2c": {
            "total": {"n": len(vendas), "valor": round(tot_b2c, 2), "ticket": round(tot_b2c / len(vendas), 2)},
            "ytd": ytd, "ytd_ate_mes": ult_mes,
            "por_produto": ticket_prod, "por_origem": soma_por(vendas, "origem"), "por_icp": soma_por(vendas, "icp"),
            "por_cupom": soma_por(vendas, "cupom_familia"), "desconto_por_cupom": desconto,
            "por_ano_origem": {a: soma_por([v for v in vendas if v["ano"] == a], "origem") for a in anos},
            "por_ano_produto": {a: soma_por([v for v in vendas if v["ano"] == a], "produto") for a in anos},
            "evento_regiao": ev_reg, "evento_tipo": ev_tipo,
            "embaixadores": [{"nome": k, **x} for k, x in emb_rank],
            "embaixadores_top10_pct": round(100 * top10 / tot_emb, 1) if tot_emb else 0,
            "serie_produto": serie(vendas, "produto"), "serie_origem": serie(vendas, "origem"),
        },
        "b2b": {
            "contratos": [{k: (v.isoformat() if isinstance(v, date) else v) for k, v in c.items()} for c in contratos],
            "mrr_atual": round(mrr_ref, 2), "arr_atual": round(mrr_ref * 12, 2), "licencas_atuais": lic_ref,
            "contas_ativas": len({c["conta"] for c in ativos_ref}), "contas_total": q_b2b["contas"],
            "renovacoes": renov, "nrr_renovacao_pct": round(100 * mrr_depois / mrr_antes, 1) if mrr_antes else None,
            "licencas_var_pct": round(100 * (sum(r["lic_depois"] for r in renov) / sum(r["lic_antes"] for r in renov) - 1), 1) if renov else None,
            "novos_logos_por_ano": dict(Counter(c["inicio"].year for c in contratos if c["tipo_venda"] == "Novo logo")),
            "preco_licenca_plano": {k: sorted(v) for k, v in preco_plano.items()},
            "por_plano": soma_por([{**c, "valor": c["valor_mensal"]} for c in ativos_ref], "plano"),
            "por_tipo": soma_por([{**c, "valor": c["valor_mensal"]} for c in ativos_ref], "tipo"),
            "por_regiao": soma_por([{**c, "valor": c["valor_mensal"]} for c in ativos_ref], "regiao"),
            "bookings_total": round(tot_b2b_book, 2),
        },
        "qualidade": {"b2c": q_b2c, "b2b": q_b2b},
    }

    data["insights"] = gerar_insights(data, vendas, contratos, ativos_ref)
    data["vendas"] = {
        "cols": ["data", "origem", "produto", "icp", "cupom_familia", "regiao_evento", "tipo_evento", "embaixador", "valor"],
        "rows": [[v["data"], v["origem"], v["produto"], v["icp"], v["cupom_familia"], v["regiao_evento"],
                  v["tipo_evento"], v["embaixador"], v["valor"]] for v in vendas],
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    # Base tratada em CSV (camada consolidada, legível fora do dashboard)
    CLEAN.mkdir(parents=True, exist_ok=True)
    with open(CLEAN / "b2c_vendas_tratado.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(vendas[0].keys()))
        w.writeheader()
        w.writerows(vendas)
    with open(CLEAN / "b2b_contratos_tratado.csv", "w", newline="", encoding="utf-8") as f:
        cols = ["id_contrato", "conta", "instituicao", "tipo", "regiao", "plano", "produto", "num_licencas", "valor_mensal",
                "preco_licenca", "data_inicio", "fim", "tipo_venda", "anterior", "status", "status_tratado"]
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for c in contratos:
            w.writerow({k: (c[k].isoformat() if isinstance(c[k], date) else c[k]) for k in cols})
    with open(CLEAN / "consolidado_mensal.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(consolidado[0].keys()))
        w.writeheader()
        w.writerows(consolidado)

    print(f"B2C: {len(vendas)} vendas válidas de {len(linhas)} | B2B: {len(contratos)} contratos, MRR atual R$ {mrr_ref:,.0f}")


if __name__ == "__main__":
    main()
