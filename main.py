from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, HTMLResponse
import httpx
import asyncio
from datetime import datetime, timedelta
from collections import defaultdict
import calendar
from typing import Optional
import os
from dotenv import load_dotenv

from tiny_auth import TinyAuth
from httpx import AsyncClient, Limits

http_client = AsyncClient(timeout=30, limits=Limits(max_connections=20))

load_dotenv()
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==================== AUTENTICAÇÃO OAUTH2 (API v3) ====================
# Cada empresa tem sua própria conta/aplicativo no Tiny.

AUTH_PROHAIR = TinyAuth(
    nome_empresa="ProHair",
    client_id=os.getenv("TINY_PROHAIR_CLIENT_ID", ""),
    client_secret=os.getenv("TINY_PROHAIR_CLIENT_SECRET", ""),
    refresh_token_inicial=os.getenv("TINY_PROHAIR_REFRESH_TOKEN", ""),
)

AUTH_PROGROWTH = TinyAuth(
    nome_empresa="ProGrowth",
    client_id=os.getenv("TINY_PROGROWTH_CLIENT_ID", ""),
    client_secret=os.getenv("TINY_PROGROWTH_CLIENT_SECRET", ""),
    refresh_token_inicial=os.getenv("TINY_PROGROWTH_REFRESH_TOKEN", ""),
)

EMPRESAS = [
    ("ProHair", AUTH_PROHAIR),
    ("ProGrowth", AUTH_PROGROWTH),
]

if not AUTH_PROHAIR.refresh_token or not AUTH_PROGROWTH.refresh_token:
    print("❌ ERRO CRÍTICO: refresh_token do Tiny não encontrado nas variáveis de ambiente!")

BASE_URL_V3 = "https://api.tiny.com.br/public-api/v3"

# ==================== CACHE DE PEDIDOS (Performance) ====================
import json
import os

CACHE_PEDIDOS_ARQUIVO = ".cache_natureza_pedidos.json"
cache_pedidos = {}

def carregar_cache_pedidos():
    global cache_pedidos
    if os.path.exists(CACHE_PEDIDOS_ARQUIVO):
        try:
            with open(CACHE_PEDIDOS_ARQUIVO, "r") as f:
                cache_pedidos = json.load(f)
        except Exception:
            cache_pedidos = {}

def salvar_cache_pedidos():
    try:
        with open(CACHE_PEDIDOS_ARQUIVO, "w") as f:
            json.dump(cache_pedidos, f)
    except Exception:
        pass

# Carrega o cache para a memória assim que a API inicia
carregar_cache_pedidos()

# Mapa para exibição amigável da situação no frontend
SITUACOES_NOME = {
    0: "Aberta",
    1: "Faturada",
    2: "Cancelada",
    3: "Aprovado",
    4: "Preparando envio",
    5: "Enviado",
    6: "Entregue",
    7: "Pronto para envio",
    8: "Dados Incompletos",
    9: "Não Entregue",
}

# ==================== METAS DIÁRIAS (R$/dia por vendedora) ====================

METAS_MENSAIS = {
    "PADRAO": 0,
    "Maria Clara David Pais": 90000.00,
    "Livia Quirino Santos": 45000.00,
    "Stephany Carolliny Soares Cândido Moreira": 35000.00,
    "Jenifer Mikaele Santos de Oliveira": 60000.00,
    "Marina David de Souza": 35000.00,
    # ProGrowth — adicione as vendedoras e metas aqui:
    # "Nome Completo ProGrowth": 1500.00,
}

# ==================== FILTROS ====================

TERMOS_BLOQUEADOS = {"BONIFICA", "BRINDE", "TROCA", "GARANTIA", "REMESSA"}
VENDEDORES_BLOQUEADOS = {"ANAMELIA", "LUIZ", "ANA CLARA", "ANDREZA", "NÍVIA"}

# Códigos numéricos de situação na API v3 (confirmados via documentação oficial):
# 0=Aberta, 1=Faturada, 2=Cancelada, 3=Aprovada, 4=Preparando Envio,
# 5=Enviada, 6=Entregue, 7=Pronto Envio, 8=Dados Incompletos, 9=Não Entregue
SITUACOES_VALIDAS = {1, 3, 4, 5, 6, 7}  # Faturada, Aprovada, Preparando, Enviada, Entregue, Pronto

# ==================== DATAS ====================

def parse_data_front(data_str: str) -> datetime:
    """Transforma a data que vem do frontend em um objeto datetime seguro."""
    try:
        if "/" in data_str:
            # Se o frontend mandou DD/MM/YYYY
            return datetime.strptime(data_str[:10], "%d/%m/%Y")
        else:
            # Se o frontend mandou YYYY-MM-DD ou formato ISO longo (ex: 2026-07-01T00:00:00.000Z)
            return datetime.strptime(data_str[:10], "%Y-%m-%d")
    except Exception:
        # Em caso de lixo na string, usa a data atual por segurança
        return datetime.now()


def calcular_datas(periodo: str, data_ini: Optional[str], data_fim: Optional[str]):
    """Retorna datas estritamente no formato YYYY-MM-DD, que é o exigido pela API v3."""
    hoje = datetime.now()

    if periodo == "personalizado" and data_ini and data_fim:
        inicio = parse_data_front(data_ini)
        fim = parse_data_front(data_fim)
    elif periodo == "semana":
        dias_para_domingo = (hoje.weekday() + 1) % 7
        inicio = hoje - timedelta(days=dias_para_domingo)
        fim = inicio + timedelta(days=6)
    elif periodo == "mes":
        inicio = hoje.replace(day=1)
        ultimo = calendar.monthrange(hoje.year, hoje.month)[1]
        fim = hoje.replace(day=ultimo)
    else:  # hoje
        inicio = hoje
        fim = hoje

    # Retorna EXATAMENTE o formato %Y-%m-%d (Ano-Mês-Dia) para a API do Tiny não recusar (Erro 400)
    return inicio.strftime("%Y-%m-%d"), fim.strftime("%Y-%m-%d")


def contar_dias(data_ini: str, data_fim: str) -> int:
    try:
        # data_ini e data_fim chegam aqui já formatados como YYYY-MM-DD
        d1 = datetime.strptime(data_ini, "%Y-%m-%d")
        d2 = datetime.strptime(data_fim, "%Y-%m-%d")
        return abs((d2 - d1).days) + 1
    except Exception:
        return 1


def data_para_exibicao(data_iso: str) -> str:
    """Converte YYYY-MM-DD de volta para DD/MM/YYYY para exibição em português no frontend."""
    if not data_iso:
        return ""
    try:
        return datetime.strptime(data_iso[:10], "%Y-%m-%d").strftime("%d/%m/%Y")
    except Exception:
        return data_iso

# ==================== FETCH ASYNC (API v3) ====================

async def buscar_pagina_v3(
    client: httpx.AsyncClient,
    auth: TinyAuth,
    data_ini: str,
    data_fim: str,
    offset: int,
    limit: int = 100,
    tentativas: int = 3,
) -> tuple[list[dict], int]:
    """Busca uma página de pedidos (offset/limit) via API v3."""
    url = f"{BASE_URL_V3}/pedidos"
    params = {
        "dataInicial": data_ini,
        "dataFinal": data_fim,
        "limit": limit,
        "offset": offset,
    }

    for tentativa in range(1, tentativas + 1):
        token = await auth.obter_token(client)
        if not token:
            print(f"DEBUG buscar_pagina_v3[{auth.nome_empresa}] | sem access_token válido, abortando")
            return [], 0

        headers = {"Authorization": f"Bearer {token}"}
        print(f"DEBUG buscar_pagina_v3[{auth.nome_empresa}] | usando token ...{token[-8:]}")
        
        async with auth.semaforo:
            # =========================================================
            # FREIO DE REQUISIÇÕES: Pausa 1 segundo antes de bater na API
            # Isso impede o erro 429 (Too Many Requests)
            # =========================================================
            await asyncio.sleep(1)

            try:
                r = await client.get(url, params=params, headers=headers, timeout=15)
                if r.status_code == 200:
                    dados = r.json()
                    itens = dados.get("itens", [])
                    total = dados.get("paginacao", {}).get("total", len(itens))
                    return itens, total
                elif r.status_code == 401:
                    print(f"DEBUG buscar_pagina_v3[{auth.nome_empresa}] | Token rejeitado (401). Invalidando token...")
                    auth.invalidar_token()
                # ---------------------------
                else:
                    corpo = r.text[:300] if r.text else "(vazio)"
                    headers_relevantes = {k: v for k, v in r.headers.items() if k.lower() in ("www-authenticate", "x-error", "content-type")}
                    print(f"DEBUG buscar_pagina_v3[{auth.nome_empresa}] | tentativa {tentativa}/{tentativas} "
                          f"status={r.status_code} corpo='{corpo}' headers={headers_relevantes}")
            except Exception as e:
                print(f"DEBUG buscar_pagina_v3[{auth.nome_empresa}] | ERRO DETALHADO: {type(e).__name__} - {e}")

        if tentativa < tentativas:
            espera = 3 * tentativa  # backoff mais forte para 429
            await asyncio.sleep(espera)

    print(f"DEBUG buscar_pagina_v3[{auth.nome_empresa}] | DESISTIU após {tentativas} tentativas")
    return [], 0


async def buscar_por_empresa_v3(
    client: httpx.AsyncClient,
    auth: TinyAuth,
    data_ini: str,
    data_fim: str,
    empresa: str,
) -> list[dict]:
    limit = 100
    primeira_pagina, total = await buscar_pagina_v3(client, auth, data_ini, data_fim, offset=0, limit=limit)

    for item in primeira_pagina:
        item["_empresa"] = empresa
        item["_auth"] = auth

    todos = list(primeira_pagina)

    if total > limit:
        offsets_restantes = list(range(limit, total, limit))
        tarefas = [
            buscar_pagina_v3(client, auth, data_ini, data_fim, offset=off, limit=limit)
            for off in offsets_restantes
        ]
        resultados = await asyncio.gather(*tarefas)
        for itens, _ in resultados:
            for item in itens:
                item["_empresa"] = empresa
                item["_auth"] = auth
            todos.extend(itens)

    return todos


async def buscar_todos_v3(data_ini: str, data_fim: str) -> list[dict]:
        resultados = await asyncio.gather(*[
            buscar_por_empresa_v3(http_client, auth, data_ini, data_fim, nome)
            for nome, auth in EMPRESAS
        ])
        todos = []
        for lista in resultados:
            todos.extend(lista)
        return todos

async def buscar_detalhe_v3(
    client: httpx.AsyncClient,
    auth: TinyAuth,
    id_pedido: str,
    tentativas: int = 3,
) -> dict:
    """Busca os detalhes completos do pedido para lermos a Natureza da Operação verdadeira."""
    if not id_pedido:
        return {}
        
    url = f"{BASE_URL_V3}/pedidos/{id_pedido}"
    
    for tentativa in range(1, tentativas + 1):
        token = await auth.obter_token(client)
        if not token:
            return {}

        headers = {"Authorization": f"Bearer {token}"}
        
        async with auth.semaforo:
            try:
                r = await client.get(url, headers=headers, timeout=15)
                if r.status_code == 200:
                    return r.json()
                elif r.status_code == 401:
                    auth.invalidar_token()
                elif r.status_code == 429:
                    await asyncio.sleep(2 * tentativa) # Se o Tiny reclamar de limite, espera um pouco
            except Exception:
                pass
                
        if tentativa < tentativas:
            await asyncio.sleep(1 * tentativa)

    return {}

# ==================== HELPERS ====================

def nome_vendedor(item: dict) -> Optional[str]:
    raw = ((item.get("vendedor") or {}).get("nome") or "").strip()
    if not raw:
        return None
    up = raw.upper()
    if "ECOMMERCE" in up:
        return None
    if any(b in up for b in VENDEDORES_BLOQUEADOS):
        return None
    return raw.replace("Pós Vendas - ", "")


def extrair_valor(item: dict) -> float:
    try:
        return float(item.get("valor") or 0)
    except Exception:
        return 0.0

# ==================== ENDPOINTS ====================

@app.get("/api/dashboard")
async def dashboard(
    periodo: str = "mes",
    data_inicio: Optional[str] = None,
    data_fim: Optional[str] = None,
    vendedora: Optional[str] = None,
):
    data_ini, data_fim_calc = calcular_datas(periodo, data_inicio, data_fim)
    dias = contar_dias(data_ini, data_fim_calc)

    todos = await buscar_todos_v3(data_ini, data_fim_calc)

    # 1. Filtro Básico: Apenas situações válidas
    pre_candidatos = [item for item in todos if item.get("situacao") in SITUACOES_VALIDAS]

    # 2. Filtro Profundo com CACHE e Lotes (Muito mais estável)
    candidatos = []
    pedidos_para_buscar = []
    TAMANHO_LOTE = 10  # Processa de 10 em 10 para não estourar o limite da API

    # Separa quem já conhecemos de quem é novo
    for item in pre_candidatos:
        id_pedido = str(item.get("id"))
        if id_pedido in cache_pedidos:
            if not cache_pedidos[id_pedido]: 
                candidatos.append(item)
        else:
            pedidos_para_buscar.append(item)

    # Só busca na API os pedidos que o script ainda não conhece, em lotes
    if pedidos_para_buscar:
        async with httpx.AsyncClient() as client:
            # Divide a lista em blocos (batches)
            for i in range(0, len(pedidos_para_buscar), TAMANHO_LOTE):
                lote = pedidos_para_buscar[i : i + TAMANHO_LOTE]
                
                # Faz as requisições deste lote
                tarefas = [buscar_detalhe_v3(client, p["_auth"], p.get("id")) for p in lote]
                detalhes = await asyncio.gather(*tarefas)
                
                # Processa os resultados do lote
                for p, detalhe in zip(lote, detalhes):
                    texto_completo = str(detalhe).upper()
                    eh_bonificacao = any(termo in texto_completo for termo in TERMOS_BLOQUEADOS)
                    
                    cache_pedidos[str(p.get("id"))] = eh_bonificacao
                    
                    if not eh_bonificacao:
                        candidatos.append(p)
                
                # Opcional: Pausa mínima entre lotes para garantir paz com a API
                await asyncio.sleep(0.5) 
                    
        salvar_cache_pedidos()

    # ==========================================================
    # Daqui para baixo é exatamente a sua lógica original
    # ==========================================================

    ranking: dict = defaultdict(lambda: {"total": 0.0, "qtd": 0})
    faturamento = 0.0
    fat_prohair = 0.0
    fat_progrowth = 0.0

    for item in candidatos:
        empresa = item.get("_empresa", "")
        nome = nome_vendedor(item)
        if not nome:
            continue

        if vendedora and vendedora.lower() not in nome.lower():
            continue

        valor = extrair_valor(item)
        ranking[nome]["total"] += valor
        ranking[nome]["qtd"] += 1
        faturamento += valor

        if empresa == "ProHair":
            fat_prohair += valor
        else:
            fat_progrowth += valor

    lista = []
    meta_total = 0.0

    try:
        data_obj = datetime.strptime(data_ini, "%Y-%m-%d")
        dias_no_mes = calendar.monthrange(data_obj.year, data_obj.month)[1]
    except:
        dias_no_mes = 30

    for nome, dados in ranking.items():
        meta_mensal = METAS_MENSAIS.get(nome, METAS_MENSAIS["PADRAO"])
        
        # Se a busca for o mês inteiro, mostra a meta redonda. 
        # Se for fração (ex: 1 dia), calcula o proporcional.
        if periodo == "mes":
            meta = meta_mensal
        else:
            meta = (meta_mensal / dias_no_mes) * dias

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
        "periodo": {
            "inicio": data_para_exibicao(data_ini),
            "fim": data_para_exibicao(data_fim_calc),
            "dias": dias,
        },
        "ultima_atualizacao": datetime.now().strftime("%d/%m/%Y %H:%M:%S")
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
    todos = await buscar_todos_v3(data_ini, data_fim_calc)

    # 1. Filtro Básico
    pre_candidatos = [item for item in todos if item.get("situacao") in SITUACOES_VALIDAS]

    # 2. Filtro Profundo (Remove Bonificações)
    candidatos = []
    async with httpx.AsyncClient() as client:
        tarefas = [buscar_detalhe_v3(client, item["_auth"], item.get("id")) for item in pre_candidatos]
        detalhes = await asyncio.gather(*tarefas)
        
        for item, detalhe in zip(pre_candidatos, detalhes):
            texto_completo = str(detalhe).upper()
            eh_bonificacao = any(termo in texto_completo for termo in TERMOS_BLOQUEADOS)
            
            if not eh_bonificacao:
                candidatos.append(item)

    resultado = []

    for item in candidatos:
        emp = item.get("_empresa", "")
        nome = nome_vendedor(item)
        if not nome:
            continue

        if vendedora and vendedora.lower() not in nome.lower():
            continue
        if empresa and empresa.lower() != emp.lower():
            continue

        valor = extrair_valor(item)

        resultado.append({
            "numero": str(item.get("numeroPedido") or item.get("id") or ""),
            "data": data_para_exibicao(item.get("dataCriacao") or ""),
            "vendedora": nome,
            "empresa": emp,
            "valor": round(valor, 2),
            "situacao": SITUACOES_NOME.get(item.get("situacao"), "Desconhecida"),
            "cliente": (item.get("cliente") or {}).get("nome", ""),
        })

    resultado.sort(key=lambda x: x["data"], reverse=True)

    return {
        "pedidos": resultado,
        "total": len(resultado),
        "valor_total": round(sum(p["valor"] for p in resultado), 2),
        "periodo": {
            "inicio": data_para_exibicao(data_ini),
            "fim": data_para_exibicao(data_fim_calc),
        },
    }

@app.get("/api/auth/login/{empresa}")
def auth_login(empresa: str):
    """Rota que te joga direto para a tela de autorização do Tiny."""
    empresa_nome = empresa.lower()
    if empresa_nome not in ["prohair", "progrowth"]:
        return {"error": "Empresa inválida. Use prohair ou progrowth"}
        
    client_id = os.getenv(f"TINY_{empresa_nome.upper()}_CLIENT_ID")
    redirect_uri = "https://dashboard-prohair-api.onrender.com/api/auth/callback"
    
    # O parâmetro 'state' serve para sabermos qual empresa está logando quando o Tiny devolver o acesso
    url = (
        f"https://accounts.tiny.com.br/realms/tiny/protocol/openid-connect/auth"
        f"?client_id={client_id}"
        f"&redirect_uri={redirect_uri}"
        f"&response_type=code"
        f"&state={empresa_nome}"
    )
    return RedirectResponse(url)


@app.get("/api/auth/callback")
async def auth_callback(code: str = None, state: str = None, error: str = None):
    """O Tiny bate aqui enviando o código. O servidor resolve tudo sozinho."""
    if error:
        return HTMLResponse(content=f"<h1>❌ Erro na autorização: {error}</h1>", status_code=400)
    if not code or not state:
        return HTMLResponse(content="<h1>❌ Parâmetros inválidos.</h1>", status_code=400)
        
    empresa_nome = state.lower()
    client_id = os.getenv(f"TINY_{empresa_nome.upper()}_CLIENT_ID")
    client_secret = os.getenv(f"TINY_{empresa_nome.upper()}_CLIENT_SECRET")
    redirect_uri = "https://dashboard-prohair-api.onrender.com/api/auth/callback"
    
    url_tiny = "https://accounts.tiny.com.br/realms/tiny/protocol/openid-connect/token"
    dados = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "redirect_uri": redirect_uri
    }
    
    async with httpx.AsyncClient() as client:
        # 1. Troca o código recebido pelos tokens de acesso oficiais
        resposta = await client.post(url_tiny, data=dados)
        if resposta.status_code != 200:
            return HTMLResponse(content=f"<h1>❌ Erro ao trocar código no Tiny: {resposta.text}</h1>", status_code=400)
            
        json_resp = resposta.json()
        access_token = json_resp.get("access_token")
        refresh_token = json_resp.get("refresh_token")
        
        import time
        tempo_expiracao = int(time.time()) + json_resp.get("expires_in", 14400) - 60
        
        # 2. Envia os tokens direto para o seu Google Sheets
        apps_script_url = os.getenv("APPS_SCRIPT_URL")
        if apps_script_url:
            payload = {
                "action": "save",
                "empresa": "ProHair" if empresa_nome == "prohair" else "ProGrowth",
                "access_token": access_token,
                "refresh_token": refresh_token,
                "expira_em": tempo_expiracao
            }
            try:
                await client.post(apps_script_url, json=payload, follow_redirects=True, timeout=15)
            except Exception as e:
                return HTMLResponse(content=f"<h1>⚠️ Tokens gerados, mas falhou ao salvar na planilha: {e}</h1>")

    # Nome formatado para exibição bonita na tela
    empresa_final = "ProHair" if empresa_nome == "prohair" else "ProGrowth"
    
    return HTMLResponse(content=f"""
        <div style="font-family: sans-serif; text-align: center; padding: 50px; background: #07080f; color: #fff; height: 100vh;">
            <h1 style="color: #dfb15b;">✅ Integração {empresa_final} Atualizada!</h1>
            <p style="font-size: 18px; color: #a4a6b1;">O servidor recebeu o código, gerou os novos tokens e já salvou tudo no seu Google Sheets.</p>
            <p style="font-weight: bold; margin-top: 30px; color: #dfb15b;">Você não precisa fazer mais nada. Pode fechar esta aba e atualizar o seu Dashboard!</p>
        </div>
    """)