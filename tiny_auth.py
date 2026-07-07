import httpx
import os
import asyncio
import time
from typing import Optional

TOKEN_URL = "https://accounts.tiny.com.br/realms/tiny/protocol/openid-connect/token"
MARGEM_SEGURANCA_SEGUNDOS = 60

class TinyAuth:
    """Gerencia o ciclo de vida do access_token salvando no Google Sheets via Apps Script."""

    def __init__(self, nome_empresa: str, client_id: str, client_secret: str, refresh_token_inicial: str):
        self.nome_empresa = nome_empresa
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token_inicial
        self.access_token: Optional[str] = None
        self.expira_em: float = 0  
        self.semaforo = asyncio.Semaphore(2)

        self.script_url = os.getenv("APPS_SCRIPT_URL")
        
        if not self.script_url:
            print(f"❌ ERRO CRÍTICO TinyAuth[{self.nome_empresa}]: APPS_SCRIPT_URL não configurada no .env")

        self._carregar_cache()

    def _carregar_cache(self):
        """Puxa o token mais recente que está salvo lá na planilha via GET (À prova de redirecionamentos)."""
        if not self.script_url: return
        try:
            print(f"DEBUG TinyAuth[{self.nome_empresa}] | Consultando Apps Script (Leitura)...")
            with httpx.Client(follow_redirects=True) as client:
                # Mudança crucial: Agora ele acessa a URL passando a empresa e usando GET
                r = client.get(
                    f"{self.script_url}?empresa={self.nome_empresa}",
                    timeout=15
                )
                dados = r.json()
                
                if "error" not in dados:
                    self.access_token = dados.get("access_token")
                    self.refresh_token = dados.get("refresh_token", self.refresh_token)
                    self.expira_em = float(dados.get("expira_em", 0))
                    print(f"DEBUG TinyAuth[{self.nome_empresa}] | Tokens resgatados da nuvem.")
                else:
                    print(f"DEBUG TinyAuth[{self.nome_empresa}] | Empresa ainda não existe na nuvem ou erro: {dados}")
                    
        except Exception as e:
            print(f"DEBUG TinyAuth[{self.nome_empresa}] | Erro ao carregar do Apps Script: {e}")

    def _salvar_cache(self):
        """Escreve os tokens atualizados na planilha via Apps Script."""
        if not self.script_url: return
        try:
            with httpx.Client() as client:
                payload = {
                    "action": "save",
                    "empresa": self.nome_empresa,
                    "access_token": self.access_token or "",
                    "refresh_token": self.refresh_token or "",
                    "expira_em": self.expira_em
                }
                client.post(self.script_url, json=payload, timeout=15)
                
            print(f"DEBUG TinyAuth[{self.nome_empresa}] | Tokens SALVOS na nuvem com sucesso.")
        except Exception as e:
            print(f"DEBUG TinyAuth[{self.nome_empresa}] | Erro ao salvar no Apps Script: {e}")

    def _token_valido(self) -> bool:
        return bool(self.access_token) and time.time() < (self.expira_em - MARGEM_SEGURANCA_SEGUNDOS)

    def invalidar_token(self):
        print(f"DEBUG TinyAuth[{self.nome_empresa}] | Invalidando access_token (forçando refresh).")
        self.access_token = None
        self.expira_em = 0
        self._salvar_cache()

    async def obter_token(self, client: httpx.AsyncClient) -> Optional[str]:
        if self._token_valido():
            return self.access_token

        print(f"DEBUG TinyAuth[{self.nome_empresa}] | renovando access_token via refresh_token...")
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
                timeout=15,
            )
            if r.status_code != 200:
                print(f"DEBUG TinyAuth[{self.nome_empresa}] | ERRO ao renovar ({r.status_code}): {r.text[:300]}")
                return None

            dados = r.json()
            self.access_token = dados["access_token"]
            self.expira_em = time.time() + dados.get("expires_in", 3600)
            self.refresh_token = dados.get("refresh_token", self.refresh_token)
            
            # Grava na planilha através do Apps Script!
            self._salvar_cache()

            print(f"DEBUG TinyAuth[{self.nome_empresa}] | Token renovado, expira em {dados.get('expires_in')}s")
            return self.access_token

        except Exception as e:
            print(f"DEBUG TinyAuth[{self.nome_empresa}] | EXCEÇÃO ao renovar: {e}")
            return None