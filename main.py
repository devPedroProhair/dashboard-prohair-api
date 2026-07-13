import os
import json
import time
import hmac
import base64
import hashlib
import asyncio
import logging
import calendar
from typing import Optional
from datetime import datetime, timedelta
from collections import defaultdict

from fastapi import FastAPI, Depends, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import httpx
from httpx import AsyncClient, Limits
from dotenv import load_dotenv

from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from tiny_auth import TinyAuth

# ==================== LOGGING ====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("dashboard")

load_dotenv()

# ==================== HTTP CLIENT COMPARTILHADO ====================
http_client = AsyncClient(timeout=30, limits=Limits(max_connections=20))

# Throttle contra o Tiny (429). Detalhes são o ponto sensível: o Tiny recusa
# rápido se muitos /pedidos/{id} saem em paralelo — por isso ficam serializados.
_sem_tiny = asyncio.Semaphore(2)      # páginas de listagem (pouca concorrência)
_sem_detalhe = asyncio.Semaphore(1)   # detalhes: um de cada vez

# ==================== APP + RATE LIMIT ====================
limiter = Limiter(key_func=get_remote_address, default_limits=["120/minute"])
app = FastAPI(title="Dashboard ProHair/ProGrowth")
app.state.limiter = limiter
app.add_middleware(SlowAPIMiddleware)


@app.exception_handler(RateLimitExceeded)
async def _rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return HTTPException(status_code=429, detail="Muitas requisições. Aguarde um instante.")

# ==================== CORS (travado por env) ====================
# CORS_ORIGINS no .env: "https://dashboard-prohair-api-front-one.vercel.app,http://localhost:5173"
_origins = [o.strip() for o in os.getenv("CORS_ORIGINS", "http://localhost:5173").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

# ==================== SEGREDOS ====================
SESSION_SECRET = os.getenv("SESSION_SECRET", "")
CRON_SECRET = os.getenv("CRON_SECRET", "")
if not SESSION_SECRET:
    logger.error("SESSION_SECRET não configurada — logins não serão seguros!")

BASE_URL_V3 = "https://api.tiny.com.br/public-api/v3"

# ==================== EMPRESAS / AUTH ====================
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
EMPRESAS = [("ProHair", AUTH_PROHAIR), ("ProGrowth", AUTH_PROGROWTH)]

if not AUTH_PROHAIR.refresh_token or not AUTH_PROGROWTH.refresh_token:
    logger.error("refresh_token do Tiny ausente nas variáveis de ambiente!")

# ==================== USUÁRIOS (hash pbkdf2 — SEM senha em texto puro) ====================
# Gere os hashes com o script gerar_hash.py e cole aqui. NUNCA vai para o frontend.
def _carregar_usuarios() -> list:
    bruto = os.getenv("USUARIOS_JSON")
    if bruto:
        try:
            return json.loads(bruto)
        except Exception as e:
            logger.error("USUARIOS_JSON inválido: %s", e)
    # Fallback opcional: arquivo usuarios.json ao lado do main.py
    caminho = os.path.join(os.path.dirname(__file__), "usuarios.json")
    if os.path.exists(caminho):
        try:
            with open(caminho, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error("usuarios.json inválido: %s", e)
    logger.error("Nenhuma fonte de usuários encontrada (USUARIOS_JSON / usuarios.json)")
    return []

USUARIOS = _carregar_usuarios()

# ==================== CRIPTOGRAFIA (stdlib, sem libs externas) ====================
def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")

def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))

def verificar_senha(senha: str, armazenado: str) -> bool:
    """armazenado = 'pbkdf2$<iteracoes>$<salt_b64>$<hash_b64>'"""
    try:
        _, it, salt_b64, dk_b64 = armazenado.split("$")
        salt, dk = _b64d(salt_b64), _b64d(dk_b64)
        novo = hashlib.pbkdf2_hmac("sha256", senha.encode(), salt, int(it))
        return hmac.compare_digest(novo, dk)
    except Exception:
        return False

SESSION_TTL = 60 * 60 * 12  # 12 horas

def criar_sessao(u: dict) -> str:
    payload = {
        "u": u["usuario"], "perfil": u["perfil"], "nome": u["nome"],
        "nomeCompleto": u.get("nomeCompleto", ""),
        "exp": int(time.time()) + SESSION_TTL,
    }
    corpo = _b64e(json.dumps(payload, separators=(",", ":")).encode())
    assinatura = _b64e(hmac.new(SESSION_SECRET.encode(), corpo.encode(), hashlib.sha256).digest())
    return f"{corpo}.{assinatura}"

def validar_sessao(token: str) -> Optional[dict]:
    try:
        corpo, assinatura = token.split(".")
        esperado = _b64e(hmac.new(SESSION_SECRET.encode(), corpo.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(assinatura, esperado):
            return None
        payload = json.loads(_b64d(corpo))
        if payload.get("exp", 0) < time.time():
            return None
        return payload
    except Exception:
        return None

async def sessao_atual(authorization: str = Header(None)) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Não autenticado")
    payload = validar_sessao(authorization[7:])
    if not payload:
        raise HTTPException(status_code=401, detail="Sessão inválida ou expirada")
    return payload

# ==================== CACHE DE CLASSIFICAÇÃO (bonificação) ====================
CACHE_CLASSIF_ARQUIVO = ".cache_natureza_pedidos.json"
CLASSIFICACAO: dict = {}   # {id_pedido: True(é bonificação) | False}

def carregar_classificacao():
    global CLASSIFICACAO
    if os.path.exists(CACHE_CLASSIF_ARQUIVO):
        try:
            with open(CACHE_CLASSIF_ARQUIVO) as f:
                CLASSIFICACAO = json.load(f)
            logger.info("Classificação carregada: %d pedidos", len(CLASSIFICACAO))
        except Exception:
            CLASSIFICACAO = {}

def salvar_classificacao():
    try:
        with open(CACHE_CLASSIF_ARQUIVO, "w") as f:
            json.dump(CLASSIFICACAO, f)
    except Exception as e:
        logger.warning("Falha ao salvar classificação: %s", e)

# ==================== CACHE DE RESULTADO (em memória) ====================
CACHE_TTL      = 600   # 10 min  — semana / mês
CACHE_TTL_HOJE = 120   #  2 min  — hoje (pedidos aparecem quase em tempo real)
_cache_candidatos: dict = {}   # {"data_ini|data_fim": (timestamp, [candidatos])}

# ==================== TABELAS DE APOIO ====================
SITUACOES_NOME = {
    0: "Aberta", 1: "Faturada", 2: "Cancelada", 3: "Aprovado",
    4: "Preparando envio", 5: "Enviado", 6: "Entregue",
    7: "Pronto para envio", 8: "Dados Incompletos", 9: "Não Entregue",
}
METAS_MENSAIS = {
    "PADRAO": 0,
    "Maria Clara David Pais": 90000.00,
    "Livia Quirino Santos": 45000.00,
    "Stephany Carolliny Soares Cândido Moreira": 35000.00,
    "Jenifer Mikaele Santos de Oliveira": 60000.00,
    "Marina David de Souza": 35000.00,
}
TERMOS_BLOQUEADOS = {"BONIFICA", "BRINDE", "TROCA", "GARANTIA", "REMESSA"}
VENDEDORES_BLOQUEADOS = {"ANAMELIA", "LUIZ", "ANA CLARA", "ANDREZA", "NÍVIA"}
SITUACOES_VALIDAS = {1, 3, 4, 5, 6, 7}

# ==================== DATAS ====================
def parse_data_front(data_str: str) -> datetime:
    try:
        if "/" in data_str:
            return datetime.strptime(data_str[:10], "%d/%m/%Y")
        return datetime.strptime(data_str[:10], "%Y-%m-%d")
    except Exception:
        return datetime.now()

def calcular_datas(periodo: str, data_ini: Optional[str], data_fim: Optional[str]):
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
    return inicio.strftime("%Y-%m-%d"), fim.strftime("%Y-%m-%d")

def contar_dias(data_ini: str, data_fim: str) -> int:
    try:
        d1 = datetime.strptime(data_ini, "%Y-%m-%d")
        d2 = datetime.strptime(data_fim, "%Y-%m-%d")
        return abs((d2 - d1).days) + 1
    except Exception:
        return 1

def data_para_exibicao(data_iso: str) -> str:
    if not data_iso:
        return ""
    try:
        return datetime.strptime(data_iso[:10], "%Y-%m-%d").strftime("%d/%m/%Y")
    except Exception:
        return data_iso

# ==================== FETCH TINY (v3) ====================
async def buscar_pagina_v3(client, auth, data_ini, data_fim, offset, limit=100, tentativas=5):
    """Retorna (itens, total, ok). ok=False significa FALHA de busca (não 'sem vendas')."""
    url = f"{BASE_URL_V3}/pedidos"
    params = {"dataInicial": data_ini, "dataFinal": data_fim, "limit": limit, "offset": offset}
    for tentativa in range(1, tentativas + 1):
        token = await auth.obter_token(client)
        if token:
            headers = {"Authorization": f"Bearer {token}"}
            async with _sem_tiny:
                await asyncio.sleep(0.3)
                try:
                    r = await client.get(url, params=params, headers=headers, timeout=15)
                    if r.status_code == 200:
                        dados = r.json()
                        itens = dados.get("itens", [])
                        total = dados.get("paginacao", {}).get("total", len(itens))
                        return itens, total, True
                    elif r.status_code == 401:
                        logger.info("buscar_pagina_v3[%s]: 401, invalidando token", auth.nome_empresa)
                        auth.invalidar_token()
                    elif r.status_code == 429:
                        espera = int(r.headers.get("Retry-After", "0") or 0) or (3 * tentativa)
                        logger.warning("buscar_pagina_v3[%s]: 429, aguardando %ss",
                                       auth.nome_empresa, espera)
                        await asyncio.sleep(espera)
                    else:
                        logger.warning("buscar_pagina_v3[%s]: status=%s corpo=%s",
                                       auth.nome_empresa, r.status_code, (r.text or "")[:200])
                except Exception as e:
                    logger.warning("buscar_pagina_v3[%s]: erro %s", auth.nome_empresa, e)
        else:
            logger.warning("buscar_pagina_v3[%s]: sem token, tentativa %d", auth.nome_empresa, tentativa)
        if tentativa < tentativas:
            await asyncio.sleep(1 * tentativa)
    logger.error("buscar_pagina_v3[%s]: DESISTIU (offset=%s) após %d tentativas",
                 auth.nome_empresa, offset, tentativas)
    return [], 0, False

async def buscar_por_empresa_v3(client, auth, data_ini, data_fim, empresa):
    """Retorna (pedidos, ok). ok=False se qualquer página falhou — dados incompletos."""
    limit = 100
    primeira, total, ok = await buscar_pagina_v3(client, auth, data_ini, data_fim, 0, limit)
    if not ok:
        # NÃO tratamos como 'zero vendas' — a empresa falhou na busca.
        return [], False
    for item in primeira:
        item["_empresa"] = empresa
        item["_auth"] = auth
    todos = list(primeira)
    completo = True
    if total > limit:
        offsets = list(range(limit, total, limit))
        tarefas = [buscar_pagina_v3(client, auth, data_ini, data_fim, off, limit) for off in offsets]
        for itens, _, ok_p in await asyncio.gather(*tarefas):
            if not ok_p:
                completo = False
                continue
            for item in itens:
                item["_empresa"] = empresa
                item["_auth"] = auth
            todos.extend(itens)
    return todos, completo

async def buscar_todos_v3(data_ini, data_fim):
    """Retorna (pedidos, completo). completo=False se ALGUMA empresa falhou."""
    resultados = await asyncio.gather(*[
        buscar_por_empresa_v3(http_client, auth, data_ini, data_fim, nome)
        for nome, auth in EMPRESAS
    ])
    todos = []
    completo = True
    for lista, ok in resultados:
        if not ok:
            completo = False
        todos.extend(lista)
    return todos, completo

async def buscar_detalhe_v3(client, auth, id_pedido, tentativas=3):
    if not id_pedido:
        return {}
    url = f"{BASE_URL_V3}/pedidos/{id_pedido}"
    for tentativa in range(1, tentativas + 1):
        token = await auth.obter_token(client)
        if not token:
            return {}
        headers = {"Authorization": f"Bearer {token}"}
        async with _sem_detalhe:
            await asyncio.sleep(0.7)  # ritmo fixo entre detalhes p/ evitar 429
            try:
                r = await client.get(url, headers=headers, timeout=15)
                if r.status_code == 200:
                    return r.json()
                elif r.status_code == 401:
                    auth.invalidar_token()
                elif r.status_code == 429:
                    espera = int(r.headers.get("Retry-After", "0") or 0) or (3 * tentativa)
                    logger.warning("buscar_detalhe_v3[%s]: 429, aguardando %ss",
                                   auth.nome_empresa, espera)
                    await asyncio.sleep(espera)
            except Exception as e:
                logger.warning("buscar_detalhe_v3[%s]: erro %s", auth.nome_empresa, e)
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

# ==================== NÚCLEO: buscar + classificar ====================
_locks_periodo = defaultdict(asyncio.Lock)

def _candidatos_de(pre):
    # Pedido ainda NÃO classificado entra como candidato (é incluído).
    # O background remove as bonificações assim que as identifica — então os
    # números aparecem na hora (levemente maiores) e se ajustam em seguida.
    return [i for i in pre if not CLASSIFICACAO.get(str(i.get("id")), False)]

async def _classificar(pre):
    """N+1 SERIALIZADO e pausado — roda só para pedidos ainda não classificados."""
    novos = [i for i in pre if str(i.get("id")) not in CLASSIFICACAO]
    if not novos:
        return
    logger.info("Classificando %d pedidos novos...", len(novos))
    for k in range(0, len(novos), 5):
        lote = novos[k:k + 5]
        detalhes = await asyncio.gather(*[
            buscar_detalhe_v3(http_client, p["_auth"], p.get("id")) for p in lote
        ])
        for p, det in zip(lote, detalhes):
            texto = str(det).upper()
            CLASSIFICACAO[str(p.get("id"))] = any(t in texto for t in TERMOS_BLOQUEADOS)
        await asyncio.sleep(0.5)
    salvar_classificacao()
    logger.info("Classificação concluída (%d no total)", len(CLASSIFICACAO))

async def obter_candidatos(data_ini: str, data_fim: str, classificar: bool = False):
    """
    classificar=False (requisição do usuário): NÃO bloqueia no N+1 — devolve o que
        já se sabe (rápido) e agenda a classificação em segundo plano.
    classificar=True (sincronização/cron): faz a classificação completa, pausada.
    O lock por período garante UMA execução por vez (fim da tempestade de 429).
    """
    chave = f"{data_ini}|{data_fim}"
    agora = time.time()
    hoje_str = datetime.now().strftime("%Y-%m-%d")
    ttl = CACHE_TTL_HOJE if (data_ini == data_fim == hoje_str) else CACHE_TTL
    ent = _cache_candidatos.get(chave)
    if ent and not classificar and (agora - ent[0]) < ttl:
        return ent[1]

    async with _locks_periodo[chave]:
        ent = _cache_candidatos.get(chave)
        if ent and not classificar and (time.time() - ent[0]) < CACHE_TTL:
            return ent[1]

        pre_raw, completo = await buscar_todos_v3(data_ini, data_fim)
        pre = [i for i in pre_raw if i.get("situacao") in SITUACOES_VALIDAS]

        if classificar:
            await _classificar(pre)
        elif any(str(i.get("id")) not in CLASSIFICACAO for i in pre):
            # agenda a classificação sem travar a resposta ao usuário
            asyncio.create_task(_classificar_bg(data_ini, data_fim))

        candidatos = _candidatos_de(pre)

        if completo:
            _cache_candidatos[chave] = (time.time(), candidatos)
            return candidatos

        # Busca INCOMPLETA (alguma empresa falhou por 429/erro): não confiamos.
        # Nunca guardamos isso no cache nem sobrescrevemos um resultado bom.
        logger.warning("Busca incompleta em %s — não cacheado (evita valor errado)", chave)
        anterior = _cache_candidatos.get(chave)
        if anterior:
            return anterior[1]          # devolve o último resultado COMPLETO conhecido
        return candidatos               # 1ª vez: devolve parcial e re-tenta na próxima

async def _classificar_bg(data_ini: str, data_fim: str):
    try:
        await obter_candidatos(data_ini, data_fim, classificar=True)
    except Exception as e:
        logger.error("Classificação em background falhou: %s", e)

# ==================== AGREGAÇÕES ====================
def montar_dashboard(candidatos, data_ini, data_fim_calc, periodo, vendedora):
    dias = contar_dias(data_ini, data_fim_calc)
    ranking = defaultdict(lambda: {"total": 0.0, "qtd": 0})
    faturamento = fat_prohair = fat_progrowth = 0.0

    for item in candidatos:
        nome = nome_vendedor(item)
        if not nome:
            continue
        if vendedora and vendedora.lower() not in nome.lower():
            continue
        valor = extrair_valor(item)
        ranking[nome]["total"] += valor
        ranking[nome]["qtd"] += 1
        faturamento += valor
        if item.get("_empresa") == "ProHair":
            fat_prohair += valor
        else:
            fat_progrowth += valor

    try:
        data_obj = datetime.strptime(data_ini, "%Y-%m-%d")
        dias_no_mes = calendar.monthrange(data_obj.year, data_obj.month)[1]
    except Exception:
        dias_no_mes = 30

    lista, meta_total = [], 0.0
    for nome, d in ranking.items():
        meta_mensal = METAS_MENSAIS.get(nome, METAS_MENSAIS["PADRAO"])
        meta = meta_mensal if periodo == "mes" else (meta_mensal / dias_no_mes) * dias
        percentual = (d["total"] / meta * 100) if meta else 0
        lista.append({
            "nome": nome, "total": round(d["total"], 2), "meta": round(meta, 2),
            "percentual": round(percentual, 1), "qtd": d["qtd"],
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
        "periodo": {"inicio": data_para_exibicao(data_ini),
                    "fim": data_para_exibicao(data_fim_calc), "dias": dias},
        "ultima_atualizacao": datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
    }

def montar_pedidos(candidatos, data_ini, data_fim_calc, vendedora, empresa):
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
            "vendedora": nome, "empresa": emp, "valor": round(valor, 2),
            "situacao": SITUACOES_NOME.get(item.get("situacao"), "Desconhecida"),
            "cliente": (item.get("cliente") or {}).get("nome", ""),
        })
    resultado.sort(key=lambda x: x["data"], reverse=True)
    return {
        "pedidos": resultado, "total": len(resultado),
        "valor_total": round(sum(p["valor"] for p in resultado), 2),
        "periodo": {"inicio": data_para_exibicao(data_ini),
                    "fim": data_para_exibicao(data_fim_calc)},
    }

# ==================== VALIDAÇÃO DE ENTRADA ====================
PERIODOS_VALIDOS = {"hoje", "semana", "mes", "personalizado"}

def _validar_periodo(periodo: str, data_inicio, data_fim):
    if periodo not in PERIODOS_VALIDOS:
        raise HTTPException(status_code=422, detail="Período inválido")
    if periodo == "personalizado":
        if not data_inicio or not data_fim:
            raise HTTPException(status_code=422, detail="Datas obrigatórias no período personalizado")

def _vendedora_efetiva(sessao: dict, vendedora: Optional[str]) -> Optional[str]:
    """Vendedora só vê os próprios dados — ignora qualquer filtro vindo do cliente."""
    if sessao.get("perfil") != "admin":
        return sessao.get("nomeCompleto") or "__sem_acesso__"
    return vendedora

# ==================== ENDPOINTS ====================
class LoginBody(BaseModel):
    usuario: str
    senha: str

@app.post("/api/login")
@limiter.limit("10/minute")
async def login(request: Request, body: LoginBody):
    alvo = body.usuario.strip().lower()
    u = next((x for x in USUARIOS if x["usuario"].lower() == alvo), None)
    if not u or not verificar_senha(body.senha, u.get("hash", "")):
        raise HTTPException(status_code=401, detail="Usuário ou senha incorretos.")
    return {
        "token": criar_sessao(u), "perfil": u["perfil"],
        "nome": u["nome"], "nomeCompleto": u.get("nomeCompleto", ""),
    }

@app.get("/api/dashboard")
async def dashboard(request: Request, periodo: str = "mes",
                    data_inicio: Optional[str] = None, data_fim: Optional[str] = None,
                    vendedora: Optional[str] = None, sessao: dict = Depends(sessao_atual)):
    _validar_periodo(periodo, data_inicio, data_fim)
    vendedora = _vendedora_efetiva(sessao, vendedora)
    data_ini, data_fim_calc = calcular_datas(periodo, data_inicio, data_fim)
    candidatos = await obter_candidatos(data_ini, data_fim_calc, classificar=False)
    return montar_dashboard(candidatos, data_ini, data_fim_calc, periodo, vendedora)

@app.get("/api/pedidos")
async def listar_pedidos(request: Request, periodo: str = "mes",
                         data_inicio: Optional[str] = None, data_fim: Optional[str] = None,
                         vendedora: Optional[str] = None, empresa: Optional[str] = None,
                         sessao: dict = Depends(sessao_atual)):
    _validar_periodo(periodo, data_inicio, data_fim)
    vendedora = _vendedora_efetiva(sessao, vendedora)
    data_ini, data_fim_calc = calcular_datas(periodo, data_inicio, data_fim)
    candidatos = await obter_candidatos(data_ini, data_fim_calc, classificar=False)
    return montar_pedidos(candidatos, data_ini, data_fim_calc, vendedora, empresa)

@app.get("/health")
async def health():
    return {"ok": True, "ts": datetime.now().isoformat()}

@app.get("/cron/sync")
async def cron_sync(token: str = ""):
    """Chamado por um cron externo (free) a cada ~10 min: mantém o Render acordado,
    renova tokens e pré-aquece o cache. Protegido por segredo."""
    if not CRON_SECRET or not hmac.compare_digest(token, CRON_SECRET):
        raise HTTPException(status_code=403, detail="Proibido")
    await sincronizar()
    return {"ok": True, "ts": datetime.now().isoformat()}

# ==================== SINCRONIZAÇÃO EM BACKGROUND ====================
async def sincronizar():
    logger.info("Sincronização iniciada")
    # 1) mantém tokens quentes
    for _, auth in EMPRESAS:
        await auth.renovar_forcado(http_client)
    # 2) pré-aquece os períodos usados pela equipe
    for periodo in ("hoje", "semana", "mes"):
        ini, fim = calcular_datas(periodo, None, None)
        try:
            await obter_candidatos(ini, fim, classificar=True)
        except Exception as e:
            logger.error("Sync período %s falhou: %s", periodo, e)
    logger.info("Sincronização concluída")

async def _loop_background():
    await asyncio.sleep(2)
    ciclo = 0
    while True:
        try:
            # "hoje" sincroniza a cada ciclo (2 min)
            # "semana" e "mês" sincronizam a cada 5 ciclos (10 min)
            periodos = ["hoje"] if ciclo % 5 != 0 else ["hoje", "semana", "mes"]
            for _, auth in EMPRESAS:
                await auth.renovar_forcado(http_client)
            for periodo in periodos:
                ini, fim = calcular_datas(periodo, None, None)
                try:
                    await obter_candidatos(ini, fim, classificar=True)
                except Exception as e:
                    logger.error("Sync período %s falhou: %s", periodo, e)
            ciclo += 1
            logger.info("Sync concluído (ciclo %d — períodos: %s)", ciclo, periodos)
        except Exception as e:
            logger.error("Loop de sync erro: %s", e)
        await asyncio.sleep(CACHE_TTL_HOJE)  # 2 min

@app.on_event("startup")
async def _startup():
    carregar_classificacao()
    for _, auth in EMPRESAS:
        await auth.carregar_cache(http_client)
    asyncio.create_task(_loop_background())