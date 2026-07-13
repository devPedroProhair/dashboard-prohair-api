"""Gera o hash pbkdf2 de uma senha para colar em USUARIOS_JSON / usuarios.json.
Uso: python gerar_hash.py "minha-senha"
"""
import sys, os, base64, hashlib

def _b64e(b): return base64.urlsafe_b64encode(b).decode().rstrip("=")

def hash_senha(senha: str, iteracoes: int = 200_000) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", senha.encode(), salt, iteracoes)
    return f"pbkdf2${iteracoes}${_b64e(salt)}${_b64e(dk)}"

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print('Uso: python gerar_hash.py "minha-senha"'); sys.exit(1)
    print(hash_senha(sys.argv[1]))