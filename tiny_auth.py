import httpx
import os
import asyncio
import time
import logging
from typing import Optional

logger = logging.getLogger("tiny.auth")

TOKEN_URL = "https://accounts.tiny.com.br/realms/tiny/protocol/openid-connect/token"

# Renovamos com folga: o access token do Tiny dura ~4h. Renovar 5 min antes
# evita que um request "morra no limite" enquanto a renovação acontece.
MARGEM_SEGURANCA_SEGUNDOS = 300


class TinyAuth:
    """
    Gerencia o ciclo de vida do access_token / refresh_token de UMA empresa.

    Pontos-chave desta versão:
      - _lock (asyncio.Lock): garante que só existe UMA renovação em andamento.
        Sem isto, dezenas de corrotinas paralelas renovam ao mesmo tempo e a
        rotação do refresh_token do Tiny invalida umas às outras.
      - carregar_cache/salvar_cache são ASSÍNCRONOS e usam o client compartilhado
        (não bloqueiam o event loop como o httpx.Client síncrono da versão antiga).
      - Nunca gravamos refresh_token vazio por cima de um válido.
      - invalidar_token() só zera o ACCESS token; o refresh nunca é apagado.
    """

    def __init__(self, nome_empresa: str, client_id: str, client_secret: str,
                 refresh_token_inicial: str):
        self.nome_empresa = nome_empresa
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token_inicial
        self.access_token: Optional[str] = None
        self.expira_em: float = 0.0
        self._lock = asyncio.Lock()

        self.script_url = os.getenv("APPS_SCRIPT_URL")
        if not self.script_url:
            logger.error("TinyAuth[%s]: APPS_SCRIPT_URL não configurada no ambiente",
                         self.nome_empresa)

    # ------------------------------------------------------------------ cache
    async def carregar_cache(self, client: httpx.AsyncClient) -> None:
        """Lê os tokens mais recentes da planilha (fonte de verdade compartilhada)."""
        if not self.script_url:
            return
        try:
            r = await client.get(
                f"{self.script_url}?empresa={self.nome_empresa}",
                timeout=15, follow_redirects=True,
            )
            dados = r.json()
            if isinstance(dados, dict) and "error" not in dados:
                rt = dados.get("refresh_token")
                if rt:  # nunca sobrescreve um refresh bom com vazio
                    self.refresh_token = rt
                self.access_token = dados.get("access_token") or self.access_token
                self.expira_em = float(dados.get("expira_em", 0) or 0)
                logger.info("TinyAuth[%s]: tokens carregados da nuvem", self.nome_empresa)
            else:
                logger.info("TinyAuth[%s]: sem tokens salvos ainda (%s)",
                            self.nome_empresa, dados)
        except Exception as e:
            logger.warning("TinyAuth[%s]: falha ao carregar cache: %s",
                           self.nome_empresa, e)

    async def salvar_cache(self, client: httpx.AsyncClient) -> None:
        """Grava os tokens atualizados na planilha, com verificação de sucesso."""
        if not self.script_url:
            return
        if not self.refresh_token:
            logger.error("TinyAuth[%s]: recusando salvar refresh_token VAZIO",
                         self.nome_empresa)
            return
        try:
            payload = {
                "action": "save",
                "empresa": self.nome_empresa,
                "access_token": self.access_token or "",
                "refresh_token": self.refresh_token,
                "expira_em": self.expira_em,
            }
            r = await client.post(self.script_url, json=payload, timeout=15,
                                  follow_redirects=True)
            if r.status_code >= 300:
                logger.error("TinyAuth[%s]: Apps Script retornou %s ao salvar",
                             self.nome_empresa, r.status_code)
            else:
                logger.info("TinyAuth[%s]: tokens salvos na nuvem", self.nome_empresa)
        except Exception as e:
            logger.error("TinyAuth[%s]: falha ao salvar cache: %s",
                         self.nome_empresa, e)

    # ------------------------------------------------------------------ estado
    def _token_valido(self) -> bool:
        return bool(self.access_token) and time.time() < (self.expira_em - MARGEM_SEGURANCA_SEGUNDOS)

    def invalidar_token(self) -> None:
        """Força renovação no próximo obter_token(). NÃO apaga o refresh_token."""
        logger.info("TinyAuth[%s]: invalidando access_token", self.nome_empresa)
        self.access_token = None
        self.expira_em = 0.0

    # ------------------------------------------------------------------ obtenção
    async def obter_token(self, client: httpx.AsyncClient) -> Optional[str]:
        # 1) caminho rápido: token em memória ainda válido
        if self._token_valido():
            return self.access_token

        # 2) só UMA corrotina entra aqui por vez
        async with self._lock:
            # outra corrotina pode ter renovado enquanto eu esperava a trava
            if self._token_valido():
                return self.access_token
            # pega o refresh_token MAIS RECENTE (outra instância pode ter rotacionado)
            await self.carregar_cache(client)
            if self._token_valido():
                return self.access_token
            return await self._renovar(client)

    async def renovar_forcado(self, client: httpx.AsyncClient) -> Optional[str]:
        """Usado pela sincronização em background para manter o token sempre quente."""
        async with self._lock:
            await self.carregar_cache(client)
            if self._token_valido():
                return self.access_token
            return await self._renovar(client)

    async def _renovar(self, client: httpx.AsyncClient) -> Optional[str]:
        """Troca o refresh_token por um novo par (access + refresh). Assume lock preso."""
        if not self.refresh_token:
            logger.error("TinyAuth[%s]: sem refresh_token para renovar",
                         self.nome_empresa)
            return None
        try:
            r = await client.post(
                TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": self.refresh_token,
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=20,
            )
            if r.status_code != 200:
                logger.error("TinyAuth[%s]: refresh falhou (%s): %s",
                             self.nome_empresa, r.status_code, r.text[:200])
                return None

            dados = r.json()
            self.access_token = dados["access_token"]
            self.expira_em = time.time() + int(dados.get("expires_in", 14400))
            novo_rt = dados.get("refresh_token")
            if novo_rt:                       # ROTAÇÃO: guarda imediatamente o novo
                self.refresh_token = novo_rt
            await self.salvar_cache(client)   # persiste antes de liberar a trava
            logger.info("TinyAuth[%s]: token renovado (expira_in=%ss)",
                        self.nome_empresa, dados.get("expires_in"))
            return self.access_token
        except Exception as e:
            logger.error("TinyAuth[%s]: exceção ao renovar: %s", self.nome_empresa, e)
            return None