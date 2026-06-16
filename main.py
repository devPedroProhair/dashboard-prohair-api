from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import httpx
import asyncio
from datetime import datetime, timedelta
from collections import defaultdict
import calendar
from typing import Optional
import os
from dotenv import load_dotenv

load_dotenv()
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==================== TOKENS ====================

TOKENS_PROHAIR = [
    os.getenv("TOKEN_PROHAIR")
]

TOKENS_PROGROWTH = [
    os.getenv("TOKEN_PROGROWTH")
]

if not TOKENS_PROHAIR[0] or not TOKENS_PROGROWTH[0]:
    print("❌ ERRO CRÍTICO: Os tokens do Tiny ERP não foram encontrados nas variáveis de ambiente!")

# ==================== METAS DIÁRIAS (R$/dia por vendedora) ====================

METAS_DIARIAS = {
    "PADRAO": 0,
    # ProHair
    "Maria Clara David Pais": 3000.00,
    "Stephany Carolliny Soares Cândido Moreira": 1166.00,
    "Jenifer Mikaele Santos de Oliveira": 1166.00,
    "Marina David de Souza": 1166.00,
    "Livia Quirino Santos": 1500.00,
    # ProGrowth — adicione as vendedoras e metas aqui:
    # "Nome Completo ProGrowth": 1500.00,
}

# ==================== FILTROS ====================

TERMOS_BLOQUEADOS = {"BONIFICA", "BRINDE", "TROCA", "GARANTIA", "REMESSA"}
VENDEDORES_BLOQUEADOS = {"ANAMELIA", "LUIZ", "ANA CLARA", "ANDREZA", "NÍVIA"}

# Únicos status aceitos nos dois endpoints: /api/dashboard e /api/pedidos
# Qualquer outro status (em aberto, aguardando aprovação, etc.) é ignorado.
STATUS_VALIDOS = {
    "aprovado",
    "preparando envio",
    "faturado",
    "pronto para envio",
    "enviado",
    "entregue",
}

# ==================== DATAS ====================

def calcular_datas(periodo: str, data_ini: Optional[str], data_fim: Optional[str]):
    hoje = datetime.now()

    if periodo == "personalizado" and data_ini and data_fim:
        return data_ini, data_fim

    if periodo == "semana":
        dias_para_domingo = (hoje.weekday() + 1) % 7
        inicio = hoje - timedelta(days=dias_para_domingo)
        fim = inicio + timedelta(days=6)
    elif periodo == "mes":
        inicio = hoje.replace(day=1)
        ultimo = calendar.monthrange(hoje.year, hoje.month)[1]
        fim = hoje.replace(day=ultimo)
    else:  # hoje
        inicio = fim = hoje

    return inicio.strftime("%d/%m/%Y"), fim.strftime("%d/%m/%Y")


def contar_dias(data_ini: str, data_fim: str) -> int:
    try:
        d1 = datetime.strptime(data_ini, "%d/%m/%Y")
        d2 = datetime.strptime(data_fim, "%d/%m/%Y")
        return abs((d2 - d1).days) + 1
    except Exception:
        return 1

# ==================== FETCH ASYNC ====================

async def buscar_pagina(client: httpx.AsyncClient, token: str, data_ini: str, data_fim: str, pagina: int):
    url = "https://api.tiny.com.br/api2/pedidos.pesquisa.php"
    params = {
        "token": token,
        "formato": "json",
        "dataInicial": data_ini,
        "dataFinal": data_fim,
        "pagina": pagina,
    }
    try:
        r = await client.get(url, params=params, timeout=12)
        dados = r.json().get("retorno", {})
        if dados.get("status") == "Erro":
            return [], 0
        return dados.get("pedidos", []), int(dados.get("numero_paginas", 1))
    except Exception:
        return [], 0


async def buscar_por_empresa(
    client: httpx.AsyncClient,
    tokens: list[str],
    data_ini: str,
    data_fim: str,
    empresa: str,
) -> list[dict]:
    # Primeira página de cada token em paralelo
    primeiras = await asyncio.gather(
        *[buscar_pagina(client, t, data_ini, data_fim, 1) for t in tokens]
    )

    todos: list[dict] = []
    proximas_tarefas: list[tuple] = []  # (coroutine, empresa)

    for i, (pedidos, total_paginas) in enumerate(primeiras):
        token = tokens[i]
        for item in pedidos:
            item["_empresa"] = empresa
        todos.extend(pedidos)
        for p in range(2, total_paginas + 1):
            proximas_tarefas.append(
                (buscar_pagina(client, token, data_ini, data_fim, p), empresa)
            )

    if proximas_tarefas:
        extras = await asyncio.gather(*[t[0] for t in proximas_tarefas])
        for idx, (pedidos, _) in enumerate(extras):
            emp = proximas_tarefas[idx][1]
            for item in pedidos:
                item["_empresa"] = emp
            todos.extend(pedidos)

    return todos


async def buscar_todos(data_ini: str, data_fim: str) -> list[dict]:
    async with httpx.AsyncClient() as client:
        ph, pg = await asyncio.gather(
            buscar_por_empresa(client, TOKENS_PROHAIR, data_ini, data_fim, "ProHair"),
            buscar_por_empresa(client, TOKENS_PROGROWTH, data_ini, data_fim, "ProGrowth"),
        )
        return ph + pg

# ==================== HELPERS ====================

def extrair_valor(p: dict) -> float:
    try:
        itens = p.get("itens", [])
        if itens:
            v = sum(float(i.get("item", {}).get("valor_total") or 0) for i in itens)
            if v > 0:
                return v
        return float(p.get("valor_produtos") or p.get("valor") or 0)
    except Exception:
        return 0.0


def nome_vendedor(p: dict) -> Optional[str]:
    raw = p.get("nome_vendedor", "").strip()
    if not raw:
        return None
    up = raw.upper()
    if "ECOMMERCE" in up:
        return None
    if any(b in up for b in VENDEDORES_BLOQUEADOS):
        return None
    return raw.replace("Pós Vendas - ", "")


def natureza_ok(p: dict) -> bool:
    nat = p.get("natureza_operacao", "").upper()
    return not any(t in nat for t in TERMOS_BLOQUEADOS)

# ==================== ENDPOINTS ====================

@app.get("/api/dashboard")
async def dashboard(
    periodo: str = "mes",
    data_inicio: Optional[str] = None,
    data_fim: Optional[str] = None,
):
    data_ini, data_fim_calc = calcular_datas(periodo, data_inicio, data_fim)
    dias = contar_dias(data_ini, data_fim_calc)

    todos = await buscar_todos(data_ini, data_fim_calc)

    ranking: dict = defaultdict(lambda: {"total": 0.0, "qtd": 0})
    faturamento = 0.0
    fat_prohair = 0.0
    fat_progrowth = 0.0

    for item in todos:
        p = item.get("pedido", item)
        print(f"DEBUG → situacao: '{p.get('situacao')}' | natureza: '{p.get('natureza_operacao', '')[:40]}'")
        empresa = item.get("_empresa", "")

        if not isinstance(p, dict):
            continue

        # ← Bloqueia qualquer status fora da lista válida (em aberto, aguardando, etc.)
        situacao = (p.get("situacao") or "").strip()
        if situacao.lower() not in STATUS_VALIDOS:
            continue

        if not natureza_ok(p):
            continue

        nome = nome_vendedor(p)
        if not nome:
            continue

        valor = extrair_valor(p)
        ranking[nome]["total"] += valor
        ranking[nome]["qtd"] += 1
        faturamento += valor

        if empresa == "ProHair":
            fat_prohair += valor
        else:
            fat_progrowth += valor

    lista = []
    meta_total = 0.0

    for nome, dados in ranking.items():
        meta = METAS_DIARIAS.get(nome, METAS_DIARIAS["PADRAO"]) * dias
        percentual = (dados["total"] / meta * 100) if meta else 0
        lista.append({
            "nome": nome,
            "total": round(dados["total"], 2),
            "meta": round(meta, 2),
            "percentual": round(percentual, 1),
            "qtd": dados["qtd"],
            "status": "Atingida" if percentual >= 100 else "Em andamento",
        })
        if meta:
            meta_total += meta

    lista.sort(key=lambda x: x["total"], reverse=True)

    return {
        "faturamento_geral": round(faturamento, 2),
        "faturamento_prohair": round(fat_prohair, 2),
        "faturamento_progrowth": round(fat_progrowth, 2),
        "meta_empresa": round(meta_total, 2),
        "melhor_vendedora": lista[0]["nome"] if lista else "-",
        "ranking": lista,
        "periodo": {"inicio": data_ini, "fim": data_fim_calc, "dias": dias},
    }


@app.get("/api/pedidos")
async def listar_pedidos(
    periodo: str = "mes",
    data_inicio: Optional[str] = None,
    data_fim: Optional[str] = None,
    vendedora: Optional[str] = None,
    empresa: Optional[str] = None,
):
    data_ini, data_fim_calc = calcular_datas(periodo, data_inicio, data_fim)
    todos = await buscar_todos(data_ini, data_fim_calc)

    resultado = []

    for item in todos:
        p = item.get("pedido", item)
        emp = item.get("_empresa", "")

        if not isinstance(p, dict):
            continue

        situacao = (p.get("situacao") or "").strip()
        if situacao.lower() not in STATUS_VALIDOS:
            continue
        if not natureza_ok(p):
            continue

        nome = nome_vendedor(p)
        if not nome:
            continue

        # Filtros opcionais
        if vendedora and vendedora.lower() not in nome.lower():
            continue
        if empresa and empresa.lower() != emp.lower():
            continue

        valor = extrair_valor(p)

        resultado.append({
            "numero": str(p.get("numero") or p.get("id") or ""),
            "data": p.get("data_pedido") or p.get("data") or "",
            "vendedora": nome,
            "empresa": emp,
            "valor": round(valor, 2),
            "situacao": situacao,
            "cliente": (
                p.get("nome_contato")
                or (p.get("cliente") or {}).get("nome", "")
                or ""
            ),
        })

    resultado.sort(key=lambda x: x["data"], reverse=True)

    return {
        "pedidos": resultado,
        "total": len(resultado),
        "valor_total": round(sum(p["valor"] for p in resultado), 2),
        "periodo": {"inicio": data_ini, "fim": data_fim_calc},
    }